from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from app.edit_ops import EditOperationError, normalize_instruction_payload, normalize_validation

HP_NS = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HS_NS = 'http://www.hancom.co.kr/hwpml/2011/section'
NS = {'hp': HP_NS, 'hs': HS_NS}
SECTION_RE = re.compile(r'^Contents/section\d+\.xml$')
NUMBERED_RE = re.compile(r'^(?:\(?\d+\)?[.)]?|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|[가-하A-Z]\.|[IVXLC]+\.)\s*')
BULLET_PREFIXES = ('■', '-', '•', '*', '○', '●', '▪', '‣')
AUTHORING_BULLET_RE = re.compile(r'^(?P<marker>[■●○▪◦※□▶▷•*\-·‣])\s+(?P<text>.+)$')
AUTHORING_NUMBER_RE = re.compile(
    r'^(?P<prefix>(?:\(?\d+\)?[.)]?|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|[가-하A-Z]\.|[IVXLC]+\.))\s+(?P<text>.+)$'
)
SECTION_HEADING_NUMBER_RE = re.compile(r'^\d+(?:\.\d+)+(?:\s+|$)')
NATIVE_BULLET_LEVEL_LEFT_MARGINS = {
    1: 0.0,
    2: 14.0,
    3: 28.0,
}
LIST_TO_PARAGRAPH_PREV_SPACING = 6.0
NATIVE_FIRST_LIST_POLICY = True
HEADING_KEYWORDS = ('개요', '목표', '배경', '필요성', '전략', '방법', '계획', '활용', '기대효과', '시장', '검증', '사업', '성과', '추진')
GENERIC_TARGET_HINTS = {
    '본문',
    '내용',
    '설명',
    '항목',
    '문단',
    '텍스트',
    'body',
    'content',
    'paragraph',
    'section',
}
BODY_SENTENCE_ENDINGS = (
    '합니다.',
    '합니다',
    '됩니다.',
    '됩니다',
    '입니다.',
    '입니다',
    '하였다.',
    '하였다',
    '한다.',
    '한다',
    '됨.',
    '됨',
)
TABLEISH_LABEL_TOKENS = ('성명', '소속', '직위', '학력', '전공', '역할', '구분', '항목', '용도')
PLACEHOLDER_RE = re.compile(r'\[\[BR::[A-Za-z0-9_:\-]+\]\]')
PLACEHOLDER_SEGMENT_RE = re.compile(r'[^A-Za-z0-9]+')
TABLE_CELL_KEY_RE = re.compile(r'^R?(\d+)\s*[_\- ]?C?(\d+)$', re.IGNORECASE)
TABLE_CELL_ADDR_RE = re.compile(r'^([A-Z]+)\s*([1-9]\d*)$', re.IGNORECASE)
PARAGRAPH_RANGE_RE = re.compile(r'^(?P<start>Contents/section\d+\.xml#p\d+)\s*(?:\.\.|~|->)\s*(?P<end>Contents/section\d+\.xml#p\d+)$')
def _normalize_section_key_list(
    section_keys: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
) -> list[str]:
    if not section_keys:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_key in section_keys:
        key = str(raw_key or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def _raise_if_section_scope_widened(
    *,
    stage: str,
    observed_section_keys: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
    allowed_section_keys: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
) -> None:
    """Fail closed before emit whenever a frozen compile lane widens beyond its explicit section scope."""
    normalized_allowed = _normalize_section_key_list(allowed_section_keys)
    if not normalized_allowed:
        return
    normalized_observed = _normalize_section_key_list(observed_section_keys)
    allowed_set = set(normalized_allowed)
    offending_section_keys = [key for key in normalized_observed if key not in allowed_set]
    if offending_section_keys:
        raise TemplateEngineError(
            'allowed_section_scope_violation: '
            f'stage={stage!r}; '
            f'allowed_section_keys={normalized_allowed!r}; '
            f'offending_section_keys={offending_section_keys!r}'
        )


class TemplateEngineError(ValueError):
    pass


def _parse_paragraph_id(paragraph_id: str) -> tuple[str, int]:
    matched = re.fullmatch(r'(Contents/section\d+\.xml)#p(\d+)', paragraph_id)
    if not matched:
        raise TemplateEngineError(f'unsupported paragraph id: {paragraph_id!r}')
    return matched.group(1), int(matched.group(2))


def _extract_visible_paragraph_texts_for_range(
    section_root_map: dict[str, ET.Element],
    paragraph_range: str,
) -> list[str]:
    matched = PARAGRAPH_RANGE_RE.fullmatch(str(paragraph_range or '').strip())
    if not matched:
        raise TemplateEngineError(f'unsupported paragraph range: {paragraph_range!r}')
    section_name, start_index = _parse_paragraph_id(matched.group('start'))
    end_section_name, end_index = _parse_paragraph_id(matched.group('end'))
    if section_name != end_section_name:
        raise TemplateEngineError(f'paragraph range must stay within one section: {paragraph_range!r}')

    root = section_root_map.get(section_name)
    if root is None:
        raise TemplateEngineError(f'section root not found for {section_name!r}')

    paragraphs = root.findall('.//hp:p', NS)
    if start_index < 1 or end_index < start_index or end_index > len(paragraphs):
        raise TemplateEngineError(f'paragraph range is out of bounds: {paragraph_range!r}')

    visible_texts: list[str] = []
    for para in paragraphs[start_index - 1:end_index]:
        text = ' '.join(''.join(para.itertext()).split()).strip()
        if text:
            visible_texts.append(text)
    return visible_texts


def _build_full_section_paragraph_range(
    section_root_map: dict[str, ET.Element],
    section_name: str,
) -> str:
    root = section_root_map.get(section_name)
    if root is None:
        raise TemplateEngineError(f'section root not found for {section_name!r}')
    paragraph_count = len(root.findall('.//hp:p', NS))
    if paragraph_count <= 0:
        raise TemplateEngineError(f'section {section_name!r} has no paragraphs')
    return f'{section_name}#p1..{section_name}#p{paragraph_count}'


def _merge_instruction_proof_metadata(
    instruction_metadata: dict[str, Any],
    emitted_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(emitted_metadata, dict):
        return instruction_metadata

    merged = dict(instruction_metadata)
    emitted_proofs: list[dict[str, Any]] = []
    single_proof = emitted_metadata.get('post_serialization_proof')
    if isinstance(single_proof, dict):
        emitted_proofs.append(single_proof)
    multiple_proofs = emitted_metadata.get('post_serialization_proofs')
    if isinstance(multiple_proofs, list):
        emitted_proofs.extend(item for item in multiple_proofs if isinstance(item, dict))

    if emitted_proofs:
        existing = list(merged.get('post_serialization_proofs') or [])
        seen = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in existing
            if isinstance(item, dict)
        }
        for proof in emitted_proofs:
            marker = json.dumps(proof, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                continue
            existing.append(proof)
            seen.add(marker)
        preserve_proof_list = bool(emitted_metadata.get('preserve_post_serialization_proofs_list'))
        if len(existing) == 1 and not preserve_proof_list:
            merged['post_serialization_proof'] = existing[0]
            merged.pop('post_serialization_proofs', None)
        else:
            merged['post_serialization_proofs'] = existing
            merged.pop('post_serialization_proof', None)

    proof_emission = emitted_metadata.get('proof_emission')
    if isinstance(proof_emission, dict):
        merged['proof_emission'] = {
            **(merged.get('proof_emission') or {}),
            **proof_emission,
        }

    return merged


def _assess_structural_paragraph_range(
    section_root_map: dict[str, ET.Element],
    start_paragraph_id: str,
    end_paragraph_id: str,
) -> dict[str, Any]:
    section_name, start_index = _parse_paragraph_id(start_paragraph_id)
    end_section_name, end_index = _parse_paragraph_id(end_paragraph_id)
    if section_name != end_section_name:
        raise TemplateEngineError('structural paragraph range assessment requires start/end within the same section')

    root = section_root_map.get(section_name)
    if root is None:
        raise TemplateEngineError(f'section root not found for {section_name!r}')

    paragraphs = root.findall('.//hp:p', NS)
    if start_index < 1 or end_index < start_index or end_index > len(paragraphs):
        raise TemplateEngineError(
            'structural paragraph range assessment received an out-of-bounds paragraph range: '
            f'{start_paragraph_id!r}..{end_paragraph_id!r}'
        )

    selected = paragraphs[start_index - 1:end_index]
    table_count = sum(len(para.findall('.//hp:tbl', NS)) for para in selected)
    picture_count = sum(len(para.findall('.//hp:pic', NS)) for para in selected)
    control_count = sum(len(para.findall('.//hp:ctrl', NS)) for para in selected)
    field_count = sum(len(para.findall('.//hp:fieldBegin', NS)) for para in selected)
    page_break_count = sum(1 for para in selected if para.attrib.get('pageBreak') == '1')

    table_paragraph_ids: list[str] = []
    picture_paragraph_ids: list[str] = []
    control_paragraph_ids: list[str] = []
    field_paragraph_ids: list[str] = []
    page_break_paragraph_ids: list[str] = []
    for offset, para in enumerate(selected, start=start_index):
        paragraph_id = f'{section_name}#p{offset}'
        if para.findall('.//hp:tbl', NS):
            table_paragraph_ids.append(paragraph_id)
        if para.findall('.//hp:pic', NS):
            picture_paragraph_ids.append(paragraph_id)
        if para.findall('.//hp:ctrl', NS):
            control_paragraph_ids.append(paragraph_id)
        if para.findall('.//hp:fieldBegin', NS):
            field_paragraph_ids.append(paragraph_id)
        if para.attrib.get('pageBreak') == '1':
            page_break_paragraph_ids.append(paragraph_id)

    return {
        'paragraph_count': len(selected),
        'table_count': table_count,
        'picture_count': picture_count,
        'control_count': control_count,
        'field_count': field_count,
        'page_break_count': page_break_count,
        'table_paragraph_ids': table_paragraph_ids,
        'picture_paragraph_ids': picture_paragraph_ids,
        'control_paragraph_ids': control_paragraph_ids,
        'field_paragraph_ids': field_paragraph_ids,
        'page_break_paragraph_ids': page_break_paragraph_ids,
        'is_complex': any((table_count, picture_count, control_count, field_count, page_break_count)),
    }


class TemplateParagraph(BaseModel):
    model_config = ConfigDict(extra='forbid')

    paragraph_id: str
    index: int
    section: str
    section_paragraph_index: int
    text: str
    normalized_text: str
    paragraph_class: str
    is_heading_candidate: bool = False
    prev_text: str = ''
    next_text: str = ''
    table_count: int = 0
    picture_count: int = 0
    control_count: int = 0
    field_count: int = 0
    page_break_count: int = 0


class TemplateAnchor(BaseModel):
    model_config = ConfigDict(extra='forbid')

    anchor_id: str
    heading_text: str
    normalized_heading: str
    section: str
    paragraph_id: str
    body_anchor_text: str
    body_paragraph_id: str
    confidence_base: float = 0.5


class TemplateMap(BaseModel):
    model_config = ConfigDict(extra='forbid')

    template_id: str
    template_version: str
    inspect_snapshot_id: str
    template_fingerprint: str
    source_filename: str
    sections: list[str]
    paragraphs: list[TemplateParagraph]
    anchors: list[TemplateAnchor]
    placeholders: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class StyleRoleBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')

    role: str
    source_find: str
    paragraph_class: str
    note: str = ''


class StyleRoleCatalog(BaseModel):
    model_config = ConfigDict(extra='forbid')

    template_id: str
    roles: list[StyleRoleBinding]

    def get(self, role: str) -> StyleRoleBinding | None:
        for item in self.roles:
            if item.role == role:
                return item
        return None


class InlineStyleToken(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str
    occurrence: int = 1
    text_style: dict[str, Any] = Field(default_factory=dict)


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra='forbid')

    type: str
    text: str | None = None
    items: list[str] = Field(default_factory=list)
    inline_styles: list[InlineStyleToken] = Field(default_factory=list)
    item_inline_styles: list[list[InlineStyleToken]] = Field(default_factory=list)
    item_depths: list[int] = Field(default_factory=list)
    style_role: str | None = None
    text_style: dict[str, Any] = Field(default_factory=dict)
    bullet_style: str | None = None
    native_style: dict[str, Any] = Field(default_factory=dict)
    native_list_kind: str | None = None
    list_level: int | None = None
    list_source_find: str | None = None
    native_only: bool = False


class TablePatchCell(BaseModel):
    model_config = ConfigDict(extra='forbid')

    cell_addr: str
    value: str


class TableRowPatchEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')

    column_ref: str
    value: str


class TableRowPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    row: int
    cells: list[TableRowPatchEntry]


class TableRecordPatchEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')

    column_ref: str
    value: str


class TableRecordPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    record_key: str
    cells: list[TableRecordPatchEntry]


class ContentSection(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section_key: str
    target_hint: str
    placeholder: str | None = None
    resolved_target_id: str | None = None
    blocks: list[ContentBlock]
    table_patches: list[TablePatchCell] = Field(default_factory=list)
    table_row_patches: list[TableRowPatch] = Field(default_factory=list)
    table_record_patches: list[TableRecordPatch] = Field(default_factory=list)
    table_entry_find: str | None = None
    table_entry_cell_addr: str | None = None
    table_entry_cursor_pos: list[int] | None = None
    table_record_key_column: str | None = None


class ContentSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source: str = 'markdown'
    inspect_snapshot_id: str | None = None
    template_fingerprint: str | None = None
    sections: list[ContentSection]


class AnchorResolution(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section_key: str
    target_hint: str
    inspect_snapshot_id: str | None = None
    resolved_via: str
    resolved_target_id: str | None = None
    matched_anchor_id: str
    matched_heading: str
    body_anchor_text: str
    confidence: float
    matched_signals: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)


class BodyScope(BaseModel):
    model_config = ConfigDict(extra='forbid')

    heading_paragraph_id: str
    body_start_paragraph_id: str
    body_end_paragraph_id: str


class TargetCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    matched_anchor_id: str
    matched_heading: str
    heading_paragraph_id: str
    body_start_paragraph_id: str
    body_end_paragraph_id: str
    recommended_resolved_target_id: str
    body_anchor_text: str
    resolved_target_id: str
    target_kind: str
    heading_text: str
    body_scope: BodyScope
    paragraph_range: str
    preview_text: str
    context_before: str = ''
    context_after: str = ''
    selection_preview: str = ''
    ownership_note: str = ''
    domain_note: str = ''
    is_conflicting: bool = False
    confidence: float
    confidence_label: str = 'high'
    matched_signals: list[str] = Field(default_factory=list)
    why_recommended: str = Field(default='', exclude=True)


class SectionTargetRecommendation(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section_key: str
    target_hint: str
    recommended_resolved_target_id: str | None = None
    recommended_target_id: str | None = None
    recommended_target_kind: str | None = None
    recommended_heading_text: str | None = None
    copy_ready_resolved_target_id: str | None = None
    selection_mode: str = 'recommended_single'
    selection_instruction: str = ''
    is_ambiguous: bool = False
    candidate_count: int = 0
    showing_candidate_count: int = 0
    candidate_state: str = 'pickable'
    candidate_state_label: str = '애매하지만 선택 가능'
    candidate_state_reason: str = ''
    section_domain_note: str = ''
    conflict_priority_summary: str = ''
    candidate_comparison_summary: str = ''
    top_choice_summary: str = ''
    recommended_action: str = ''
    section_actions: list[dict[str, str]] = Field(default_factory=list)
    advanced_section_actions: list[dict[str, str]] = Field(default_factory=list)
    candidates: list[TargetCandidate] = Field(default_factory=list)


class PlaceholderResolution(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section_key: str
    placeholder: str
    matched: bool
    raw_input: str | None = None


class PlaceholderFillItem(BaseModel):
    model_config = ConfigDict(extra='forbid')

    input_key: str
    placeholder: str
    value: str
    source: str


class PlaceholderFillSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')

    source: str = 'placeholder_fill'
    items: list[PlaceholderFillItem]


class ApplyScopePreview(BaseModel):
    model_config = ConfigDict(extra='forbid')

    section_key: str
    target_hint: str
    edit_shape: str
    exact_target_locked: bool
    resolved_target_id: str | None = None
    resolved_via: str | None = None
    resolved_target_type: str = 'body'
    target_kind: str
    heading_paragraph_id: str | None = None
    heading_before_preview_text: str = ''
    body_start_paragraph_id: str | None = None
    body_end_paragraph_id: str | None = None
    body_before_preview_text: str = ''
    apply_preview_text: str = ''
    warning_state: str = 'clear'
    blocking_warning_codes: list[str] = Field(default_factory=list)
    why_not_body_safe: str = ''
    start_paragraph_id: str | None = None
    end_paragraph_id: str | None = None
    paragraph_count: int = 0
    before_preview_text: str = ''
    after_preview_text: str = ''
    render_review_required: bool = True


class WarningBadge(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str
    severity: str = 'warning'
    stage: str = 'compile'
    blocking: bool = False
    section_key: str | None = None
    resolved_target_id: str | None = None
    summary: str
    detail: str = ''



class EditPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')

    operations: list[dict[str, Any]]
    validation: dict[str, Any] = Field(default_factory=dict)
    instruction_metadata: dict[str, Any] = Field(default_factory=dict)
    compile_readiness: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    resolutions: list[AnchorResolution] = Field(default_factory=list)
    target_recommendations: list[SectionTargetRecommendation] = Field(default_factory=list)
    placeholder_resolutions: list[PlaceholderResolution] = Field(default_factory=list)
    cleanup_operations: list[dict[str, Any]] = Field(default_factory=list)
    apply_scope_previews: list[ApplyScopePreview] = Field(default_factory=list)
    warning_badges: list[WarningBadge] = Field(default_factory=list)
    precondition_ok: bool = True
    execution_mode: str = 'fallback'
    native_action_count: int = 0
    native_action_used: list[dict[str, Any]] = Field(default_factory=list)
    fallback_reason: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    touched_ranges: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_targets: list[dict[str, Any]] = Field(default_factory=list)
    confirm_policy: dict[str, Any] = Field(default_factory=dict)
    approval_packet: dict[str, Any] = Field(default_factory=dict)
    policy_override: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _SectionDraft:
    heading: str
    target_hint: str
    placeholder: str | None
    resolved_target_id: str | None
    blocks: list[ContentBlock]
    table_patches: list[TablePatchCell]
    table_row_patches: list[TableRowPatch]
    table_record_patches: list[TableRecordPatch]
    table_entry_find: str | None
    table_entry_cell_addr: str | None
    table_entry_cursor_pos: list[int] | None
    table_record_key_column: str | None


def _parse_bool_directive(value: str, *, field_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {'true', 'yes', '1', 'on'}:
        return True
    if normalized in {'false', 'no', '0', 'off'}:
        return False
    raise TemplateEngineError(f'{field_name} directive must be true/false, got {value!r}')


def _parse_float_directive(value: str, *, field_name: str) -> float:
    try:
        return float(value.strip())
    except ValueError as exc:
        raise TemplateEngineError(f'{field_name} directive must be numeric, got {value!r}') from exc


def _parse_int_directive(value: str, *, field_name: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise TemplateEngineError(f'{field_name} directive must be an integer, got {value!r}') from exc


def _normalize_native_list_kind(value: str) -> str:
    normalized = value.strip().lower()
    mapping = {
        'bullet': 'bullet',
        'number': 'number',
        'outline': 'outline',
        'none': 'none',
    }
    if normalized not in mapping:
        supported = ', '.join(sorted(mapping))
        raise TemplateEngineError(f'unsupported @native_list_kind value {value!r}. Supported: {supported}')
    return mapping[normalized]


def _infer_bullet_style_from_marker(marker: str) -> str:
    if marker == '-':
        return 'dash'
    if marker in {'•', '·', '◦'}:
        return 'dot'
    if marker == '●':
        return 'disc'
    if marker == '○':
        return 'circle'
    return 'square'


def _normalize_section_heading_label(text: str) -> str:
    compact = re.sub(r'\s+', ' ', str(text or '').strip())
    compact = re.sub(r'\([^)]*\)$', '', compact).strip()
    return compact


def _looks_like_section_heading_text(text: str) -> bool:
    compact = _normalize_section_heading_label(text)
    if not compact or len(compact) > 120:
        return False
    if SECTION_HEADING_NUMBER_RE.match(compact):
        return True
    single_level = re.match(r'^(\d+)\.\s+(.+)$', compact)
    if not single_level:
        return False
    tail = single_level.group(2).strip()
    if any(keyword in tail for keyword in HEADING_KEYWORDS):
        return True
    if tail in {'적요', '결론', '참고 문헌', '참고문헌', '서론', '재료 및 방법', '결과 및 고찰', '결과', '고찰'}:
        return True
    return False


def _paragraph_matches_section_heading(paragraph_text: str, section_key: str) -> bool:
    left = _normalize_section_heading_label(paragraph_text)
    right = _normalize_section_heading_label(section_key)
    if not left or not right:
        return False
    if left == right:
        return True
    return left.startswith(right) or right.startswith(left)


def _parse_authoring_list_item(raw_line: str) -> tuple[str, str, int, str | None] | None:
    stripped = raw_line.strip()
    if not stripped:
        return None

    if _looks_like_section_heading_text(stripped):
        return None

    leading = len(raw_line) - len(raw_line.lstrip(' \t'))
    indent_width = raw_line[:leading].replace('\t', '    ')
    depth = max(1, (len(indent_width) // 2) + 1)

    bullet_match = AUTHORING_BULLET_RE.match(stripped)
    if bullet_match:
        inferred_style = None if NATIVE_FIRST_LIST_POLICY else _infer_bullet_style_from_marker(bullet_match.group('marker'))
        return (
            'bullet_list',
            bullet_match.group('text').strip(),
            depth,
            inferred_style,
        )

    number_match = AUTHORING_NUMBER_RE.match(stripped)
    if number_match:
        return ('numbered_list', number_match.group('text').strip(), depth, None)

    return None


def _parse_inline_strong_markup(text: str) -> tuple[str, list[InlineStyleToken]]:
    if '**' not in text:
        return text, []

    plain_parts: list[str] = []
    inline_styles: list[InlineStyleToken] = []
    occurrence_map: dict[str, int] = {}
    cursor = 0

    for match in re.finditer(r'\*\*(.+?)\*\*', text):
        start, end = match.span()
        if start > cursor:
            plain_parts.append(text[cursor:start])
        token_text = match.group(1)
        plain_parts.append(token_text)
        occurrence_map[token_text] = occurrence_map.get(token_text, 0) + 1
        inline_styles.append(
            InlineStyleToken(
                text=token_text,
                occurrence=occurrence_map[token_text],
                text_style={'bold': True},
            )
        )
        cursor = end

    if cursor < len(text):
        plain_parts.append(text[cursor:])

    plain_text = ''.join(plain_parts)
    return plain_text, inline_styles


def _text_of(elem: ET.Element) -> str:
    parts: list[str] = []
    for node in elem.iter():
        if node.tag == f'{{{HP_NS}}}t' and node.text:
            parts.append(node.text)
    return ''.join(parts).strip()


def _normalize_text(text: str) -> str:
    compact = re.sub(r'\s+', ' ', text).strip()
    return compact.casefold()


def canonicalize_placeholder_token(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise TemplateEngineError('placeholder token must not be empty')
    if PLACEHOLDER_RE.fullmatch(raw):
        return raw

    candidate = raw
    if candidate.startswith('[[BR::') and candidate.endswith(']]'):
        candidate = candidate[6:-2]
    elif candidate.startswith('BR::'):
        candidate = candidate[4:]

    parts = [part for part in re.split(r'[:/\\>\-]+', candidate) if part.strip()]
    normalized_parts: list[str] = []
    for part in parts:
        cleaned = PLACEHOLDER_SEGMENT_RE.sub('_', part.strip()).strip('_').upper()
        if cleaned:
            normalized_parts.append(cleaned)
    if not normalized_parts:
        raise TemplateEngineError(f'failed to build placeholder token from {value!r}')
    return f'[[BR::{"::".join(normalized_parts)}]]'


def placeholder_naming_rules() -> dict[str, Any]:
    return {
        'prefix': 'BR',
        'format': '[[BR::SCOPE::NAME]]',
        'table_cell_format': '[[BR::TABLE::TABLE_NAME::R03_C02]]',
        'segment_style': 'UPPER_SNAKE_CASE',
        'separator': '::',
        'examples': [
            '[[BR::COMPANY_NAME]]',
            '[[BR::TABLE::COMPANY_OVERVIEW::R03_C02]]',
            '[[BR::IMAGE::CAPTION_01]]',
        ],
    }


def canonicalize_placeholder_segment(value: str) -> str:
    cleaned = PLACEHOLDER_SEGMENT_RE.sub('_', str(value).strip()).strip('_').upper()
    if not cleaned:
        raise TemplateEngineError(f'invalid placeholder segment: {value!r}')
    return cleaned


def build_table_cell_placeholder(table_name: str, row: int, col: int) -> str:
    if row <= 0 or col <= 0:
        raise TemplateEngineError(f'table placeholder row/col must be positive integers, got row={row}, col={col}')
    table_segment = canonicalize_placeholder_segment(table_name)
    return f'[[BR::TABLE::{table_segment}::R{row:02d}_C{col:02d}]]'


def _coerce_fill_value(value: Any, *, field_name: str) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise TemplateEngineError(f'{field_name} value must be a string, number, boolean, or null')


def _parse_table_cell_key(value: str) -> tuple[int, int]:
    matched = TABLE_CELL_KEY_RE.fullmatch(str(value).strip())
    if not matched:
        raise TemplateEngineError(f'unsupported table cell key: {value!r}. Use r1c1, R01_C02, or similar')
    return int(matched.group(1)), int(matched.group(2))


def _column_letters_to_index(value: str) -> int:
    total = 0
    for char in value.upper():
        if not ('A' <= char <= 'Z'):
            raise TemplateEngineError(f'unsupported table column letters: {value!r}')
        total = (total * 26) + (ord(char) - ord('A') + 1)
    return total


def _column_index_to_letters(value: int) -> str:
    if value <= 0:
        raise TemplateEngineError(f'table column index must be positive, got {value}')
    letters: list[str] = []
    current = value
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(ord('A') + remainder))
    return ''.join(reversed(letters))


def normalize_table_cell_addr(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise TemplateEngineError('table cell address must not be empty')
    addr_match = TABLE_CELL_ADDR_RE.fullmatch(raw)
    if addr_match:
        letters = addr_match.group(1).upper()
        row = int(addr_match.group(2))
        return f'{letters}{row}'
    row, col = _parse_table_cell_key(raw)
    return f'{_column_index_to_letters(col)}{row}'


def _parse_cursor_pos_directive(value: str, *, field_name: str) -> list[int]:
    raw = value.strip()
    if not raw:
        raise TemplateEngineError(f'{field_name} directive requires a non-empty value')
    if raw.startswith('['):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TemplateEngineError(f'{field_name} must be a 3-item JSON array or comma-separated integers') from exc
        if not isinstance(parsed, list):
            raise TemplateEngineError(f'{field_name} must decode to a JSON array')
        items = parsed
    else:
        items = [part.strip() for part in raw.split(',')]
    if len(items) != 3:
        raise TemplateEngineError(f'{field_name} must contain exactly 3 integers')
    normalized: list[int] = []
    for item in items:
        if isinstance(item, bool):
            raise TemplateEngineError(f'{field_name} entries must be integers')
        try:
            normalized.append(int(item))
        except (TypeError, ValueError) as exc:
            raise TemplateEngineError(f'{field_name} entries must be integers') from exc
    return normalized


def _parse_table_cell_directive(value: str) -> TablePatchCell:
    raw = value.strip()
    if not raw:
        raise TemplateEngineError('@table_cell directive requires a non-empty value')
    matched = re.match(r'^(?P<cell>[^:=\s]+)\s*(?:=|:)\s*(?P<text>.*)$', raw)
    if not matched:
        raise TemplateEngineError('@table_cell directive must look like "B2 = 값" or "r2c2: 값"')
    return TablePatchCell(
        cell_addr=normalize_table_cell_addr(matched.group('cell')),
        value=matched.group('text').strip(),
    )


def _parse_table_row_directive(value: str) -> TableRowPatch:
    raw = value.strip()
    if not raw:
        raise TemplateEngineError('@table_row directive requires a non-empty value')
    parts = [part.strip() for part in raw.split('|')]
    if not parts:
        raise TemplateEngineError('@table_row directive requires a row and at least one column assignment')
    row_token = parts[0]
    try:
        row = int(row_token)
    except ValueError as exc:
        raise TemplateEngineError('@table_row first segment must be an integer row number, for example: 2 | 소속=값') from exc
    if row <= 0:
        raise TemplateEngineError('@table_row row number must be positive')
    cells: list[TableRowPatchEntry] = []
    for segment in parts[1:]:
        if not segment:
            continue
        matched = re.match(r'^(?P<col>[^:=]+?)\s*(?:=|:)\s*(?P<text>.*)$', segment)
        if not matched:
            raise TemplateEngineError('@table_row assignments must look like "헤더=값"')
        column_ref = matched.group('col').strip()
        if not column_ref:
            raise TemplateEngineError('@table_row assignment requires a non-empty header or column reference')
        cells.append(TableRowPatchEntry(column_ref=column_ref, value=matched.group('text').strip()))
    if not cells:
        raise TemplateEngineError('@table_row requires at least one column assignment')
    return TableRowPatch(row=row, cells=cells)


def _parse_table_record_directive(value: str) -> TableRecordPatch:
    raw = value.strip()
    if not raw:
        raise TemplateEngineError('@table_record directive requires a non-empty value')
    parts = [part.strip() for part in raw.split('|')]
    record_key = parts[0].strip() if parts else ''
    if not record_key:
        raise TemplateEngineError('@table_record first segment must be a non-empty record key')
    cells: list[TableRecordPatchEntry] = []
    for segment in parts[1:]:
        if not segment:
            continue
        matched = re.match(r'^(?P<col>[^:=]+?)\s*(?:=|:)\s*(?P<text>.*)$', segment)
        if not matched:
            raise TemplateEngineError('@table_record assignments must look like "헤더=값"')
        column_ref = matched.group('col').strip()
        if not column_ref:
            raise TemplateEngineError('@table_record assignment requires a non-empty header or column reference')
        cells.append(TableRecordPatchEntry(column_ref=column_ref, value=matched.group('text').strip()))
    if not cells:
        raise TemplateEngineError('@table_record requires at least one column assignment')
    return TableRecordPatch(record_key=record_key, cells=cells)


def parse_placeholder_fill_json(text: str) -> PlaceholderFillSpec:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditOperationError(f'invalid placeholder_fills_json: {exc}') from exc

    if not isinstance(raw, dict):
        raise TemplateEngineError('placeholder_fills_json must be a JSON object')

    items: list[PlaceholderFillItem] = []
    seen: set[str] = set()

    placeholders = raw.get('placeholders', {})
    if placeholders is not None:
        if not isinstance(placeholders, dict):
            raise TemplateEngineError('placeholders must be a JSON object of placeholder -> value')
        for input_key, value in placeholders.items():
            placeholder = canonicalize_placeholder_token(str(input_key))
            if placeholder in seen:
                raise TemplateEngineError(f'duplicate placeholder fill: {placeholder}')
            seen.add(placeholder)
            items.append(
                PlaceholderFillItem(
                    input_key=str(input_key),
                    placeholder=placeholder,
                    value=_coerce_fill_value(value, field_name=str(input_key)),
                    source='placeholders',
                )
            )

    tables = raw.get('tables', [])
    if tables is not None:
        if not isinstance(tables, list):
            raise TemplateEngineError('tables must be a JSON array')
        for index, table in enumerate(tables, start=1):
            if not isinstance(table, dict):
                raise TemplateEngineError(f'tables[{index}] must be a JSON object')
            table_name = table.get('name') or table.get('table') or table.get('scope')
            if not table_name or not str(table_name).strip():
                raise TemplateEngineError(f'tables[{index}] requires a non-empty name')
            cells = table.get('cells', {})
            if not isinstance(cells, dict):
                raise TemplateEngineError(f'tables[{index}].cells must be a JSON object')
            for cell_key, value in cells.items():
                row, col = _parse_table_cell_key(str(cell_key))
                placeholder = build_table_cell_placeholder(str(table_name), row, col)
                if placeholder in seen:
                    raise TemplateEngineError(f'duplicate placeholder fill: {placeholder}')
                seen.add(placeholder)
                items.append(
                    PlaceholderFillItem(
                        input_key=f'{table_name}:{cell_key}',
                        placeholder=placeholder,
                        value=_coerce_fill_value(value, field_name=f'{table_name}:{cell_key}'),
                        source='table_cells',
                    )
                )

    table_cells = raw.get('table_cells', [])
    if table_cells is not None:
        if not isinstance(table_cells, list):
            raise TemplateEngineError('table_cells must be a JSON array')
        for index, item in enumerate(table_cells, start=1):
            if not isinstance(item, dict):
                raise TemplateEngineError(f'table_cells[{index}] must be a JSON object')
            table_name = item.get('table') or item.get('name') or item.get('scope')
            if not table_name or not str(table_name).strip():
                raise TemplateEngineError(f'table_cells[{index}] requires table/name/scope')
            row = item.get('row')
            col = item.get('col')
            if not isinstance(row, int) or not isinstance(col, int):
                raise TemplateEngineError(f'table_cells[{index}] requires integer row and col')
            placeholder = build_table_cell_placeholder(str(table_name), row, col)
            if placeholder in seen:
                raise TemplateEngineError(f'duplicate placeholder fill: {placeholder}')
            seen.add(placeholder)
            items.append(
                PlaceholderFillItem(
                    input_key=f'{table_name}:r{row}c{col}',
                    placeholder=placeholder,
                    value=_coerce_fill_value(item.get('value'), field_name=f'{table_name}:r{row}c{col}'),
                    source='table_cells',
                )
            )

    if not items:
        raise TemplateEngineError('placeholder_fills_json produced no fill items')
    return PlaceholderFillSpec(items=items)


def _classify_paragraph(text: str) -> str:
    compact = ' '.join(text.split())
    if not compact:
        return 'empty'
    if NUMBERED_RE.match(compact):
        return 'number'
    if compact.startswith(BULLET_PREFIXES):
        return 'bullet'
    if _looks_like_heading(compact):
        return 'heading'
    return 'body'


def _looks_like_heading(text: str) -> bool:
    compact = ' '.join(text.split())
    if not compact:
        return False
    if compact.endswith(('문단', '설명 문단', '기준 문단')):
        return False
    if len(compact) >= 12 and compact.endswith(BODY_SENTENCE_ENDINGS):
        return False
    if compact.startswith('※'):
        return False
    if len(compact) <= 36 and any(token in compact for token in HEADING_KEYWORDS):
        return True
    if compact.endswith(':') and len(compact) <= 48:
        return True
    if NUMBERED_RE.match(compact) and len(compact) <= 48:
        return True
    return False


def _is_heading_candidate_text(text: str, paragraph_class: str) -> bool:
    compact = ' '.join(text.split())
    if not compact:
        return False
    if compact.startswith('※'):
        return False
    if paragraph_class == 'heading':
        return True
    if paragraph_class == 'number' and len(compact) <= 48 and any(token in compact for token in HEADING_KEYWORDS):
        return True
    return False


def _is_body_anchor_candidate(paragraph: TemplateParagraph) -> bool:
    compact = ' '.join(paragraph.text.split())
    if not compact:
        return False
    if paragraph.is_heading_candidate:
        return False
    if compact.startswith('※'):
        return False
    return True


def _is_clean_body_exemplar_text(text: str) -> bool:
    compact = ' '.join(text.split())
    if not compact:
        return False
    if len(compact) < 6:
        return False
    if compact in {'∨', '○', '●', '-'}:
        return False
    colon_index = compact.find(':')
    if 0 <= colon_index <= 12 and len(compact) <= 48:
        return False
    if '<' in compact and '>' in compact:
        return False
    if len(compact) <= 48 and sum(token in compact for token in TABLEISH_LABEL_TOKENS) >= 3:
        return False
    if len(compact) <= 18 and sum(token in compact for token in TABLEISH_LABEL_TOKENS) >= 1:
        return False
    numbered_markers = sum(compact.count(marker) for marker in ('1.', '2.', '3.', '4.', '(1)', '(2)', '(3)', '①', '②', '③'))
    bullet_markers = sum(compact.count(marker) for marker in ('■', '- ', '· '))
    if len(compact) >= 120 and (numbered_markers >= 3 or bullet_markers >= 3):
        return False
    return True


def _is_clean_number_exemplar_text(text: str) -> bool:
    compact = ' '.join(text.split())
    if not compact:
        return False
    numbered_markers = sum(compact.count(marker) for marker in ('1.', '2.', '3.', '4.', '(1)', '(2)', '(3)', '①', '②', '③', '④', '⑤'))
    if len(compact) >= 40 and numbered_markers >= 4:
        return False
    if len(compact) >= 120 and numbered_markers >= 3:
        return False
    return True


def _list_section_paths(names: list[str]) -> list[str]:
    return sorted(name for name in names if SECTION_RE.match(name))


def _parse_section_roots(input_path: Path) -> list[tuple[str, ET.Element]]:
    with zipfile.ZipFile(input_path) as zf:
        selected = _list_section_paths(zf.namelist())
        if not selected:
            raise TemplateEngineError('no Contents/section*.xml entries found in package')
        return [(name, ET.fromstring(zf.read(name))) for name in selected]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _build_snapshot_identity(path: Path) -> tuple[str, str, str]:
    file_hash = _file_sha256(path)
    template_version = path.stem
    inspect_snapshot_id = f'inspect:{file_hash[:12]}'
    template_fingerprint = f'tfp:{template_version}:{file_hash[:16]}'
    return template_version, inspect_snapshot_id, template_fingerprint


def build_template_map(input_path: str | Path) -> TemplateMap:
    path = Path(input_path)
    roots = _parse_section_roots(path)
    template_version, inspect_snapshot_id, template_fingerprint = _build_snapshot_identity(path)
    paragraphs: list[TemplateParagraph] = []
    anchors: list[TemplateAnchor] = []
    placeholders: list[str] = []
    global_index = 0

    for section_name, root in roots:
        raw_records: list[dict[str, Any]] = []
        section_index = 0
        for para in root.findall('.//hp:p', NS):
            section_index += 1
            global_index += 1
            text = _text_of(para)
            raw_records.append(
                {
                    'index': global_index,
                    'section': section_name,
                    'section_paragraph_index': section_index,
                    'text': text,
                    'normalized_text': _normalize_text(text),
                    'paragraph_class': _classify_paragraph(text),
                    'table_count': len(para.findall('.//hp:tbl', NS)),
                    'picture_count': len(para.findall('.//hp:pic', NS)),
                    'control_count': len(para.findall('.//hp:ctrl', NS)),
                    'field_count': len(para.findall('.//hp:fieldBegin', NS)),
                    'page_break_count': 1 if para.attrib.get('pageBreak') == '1' else 0,
                }
            )

        for idx, record in enumerate(raw_records):
            prev_text = raw_records[idx - 1]['text'] if idx > 0 else ''
            next_text = raw_records[idx + 1]['text'] if idx + 1 < len(raw_records) else ''
            para_model = TemplateParagraph(
                paragraph_id=f'{section_name}#p{record["section_paragraph_index"]}',
                index=record['index'],
                section=section_name,
                section_paragraph_index=record['section_paragraph_index'],
                text=record['text'],
                normalized_text=record['normalized_text'],
                paragraph_class=record['paragraph_class'],
                is_heading_candidate=_is_heading_candidate_text(record['text'], record['paragraph_class']),
                prev_text=prev_text,
                next_text=next_text,
                table_count=record['table_count'],
                picture_count=record['picture_count'],
                control_count=record['control_count'],
                field_count=record['field_count'],
                page_break_count=record['page_break_count'],
            )
            paragraphs.append(para_model)
            placeholders.extend(PLACEHOLDER_RE.findall(para_model.text))

        for idx, para in enumerate(paragraphs):
            if para.section != section_name or not para.is_heading_candidate:
                continue
            body_anchor = para
            for candidate in paragraphs:
                if candidate.section != section_name:
                    continue
                if candidate.section_paragraph_index <= para.section_paragraph_index:
                    continue
                if candidate.is_heading_candidate:
                    break
                if _is_body_anchor_candidate(candidate):
                    body_anchor = candidate
                    break
            anchors.append(
                TemplateAnchor(
                    anchor_id=f'{section_name}#anchor{para.section_paragraph_index}',
                    heading_text=para.text,
                    normalized_heading=para.normalized_text,
                    section=section_name,
                    paragraph_id=para.paragraph_id,
                    body_anchor_text=body_anchor.text or para.text,
                    body_paragraph_id=body_anchor.paragraph_id,
                    confidence_base=0.55 if body_anchor.text and body_anchor.paragraph_id != para.paragraph_id else 0.45,
                )
            )

    stats = {
        'paragraph_count': len(paragraphs),
        'anchor_count': len(anchors),
        'heading_count': sum(1 for para in paragraphs if para.is_heading_candidate),
        'placeholder_count': len(sorted(set(placeholders))),
        'table_paragraph_count': sum(1 for para in paragraphs if para.table_count > 0),
        'picture_paragraph_count': sum(1 for para in paragraphs if para.picture_count > 0),
        'control_paragraph_count': sum(1 for para in paragraphs if para.control_count > 0),
    }
    return TemplateMap(
        template_id=path.stem,
        template_version=template_version,
        inspect_snapshot_id=inspect_snapshot_id,
        template_fingerprint=template_fingerprint,
        source_filename=path.name,
        sections=[section_name for section_name, _root in roots],
        paragraphs=paragraphs,
        anchors=anchors,
        placeholders=sorted(set(placeholders)),
        stats=stats,
    )


def build_style_role_catalog(
    template_map: TemplateMap,
    *,
    scope_section: str | None = None,
    scope_paragraph: TemplateParagraph | None = None,
    exclude_paragraph_ids: set[str] | None = None,
) -> StyleRoleCatalog:
    roles: list[StyleRoleBinding] = []
    excluded_ids = exclude_paragraph_ids or set()

    scoped_paragraphs = [para for para in template_map.paragraphs if scope_section is None or para.section == scope_section]
    fallback_paragraphs = [para for para in template_map.paragraphs if scope_section is not None and para.section != scope_section]
    local_region_start_index: int | None = None
    local_region_end_index: int | None = None

    if scope_paragraph is not None:
        same_section = [para for para in template_map.paragraphs if para.section == scope_paragraph.section]
        prior_headings = [
            para.section_paragraph_index
            for para in same_section
            if para.section_paragraph_index < scope_paragraph.section_paragraph_index and para.is_heading_candidate
        ]
        next_headings = [
            para.section_paragraph_index
            for para in same_section
            if para.section_paragraph_index > scope_paragraph.section_paragraph_index and para.is_heading_candidate
        ]
        local_region_start_index = max(prior_headings) if prior_headings else None
        local_region_end_index = min(next_headings) if next_headings else None

    def _is_within_local_region(para: TemplateParagraph) -> bool:
        if scope_paragraph is None or para.section != scope_paragraph.section:
            return False
        if local_region_start_index is not None and para.section_paragraph_index < local_region_start_index:
            return False
        if local_region_end_index is not None and para.section_paragraph_index >= local_region_end_index:
            return False
        return True

    def _candidate_paragraphs() -> list[TemplateParagraph]:
        if scope_paragraph is not None:
            return sorted(
                template_map.paragraphs,
                key=lambda para: (
                    0 if para.section == scope_paragraph.section else 1,
                    abs(para.section_paragraph_index - scope_paragraph.section_paragraph_index)
                    if para.section == scope_paragraph.section
                    else para.index + 100000,
                    para.index,
                ),
            )
        return scoped_paragraphs + fallback_paragraphs

    def _find_first(
        paragraph_class: str,
        fallback: str | None = None,
        *,
        prefer_non_heading: bool = False,
        prefer_plain: bool = False,
        prefer_clean_number: bool = False,
        local_only: bool = False,
    ) -> str | None:
        preferred_local: list[TemplateParagraph] = []
        relaxed_local: list[TemplateParagraph] = []
        preferred: list[TemplateParagraph] = []
        relaxed: list[TemplateParagraph] = []
        for para in _candidate_paragraphs():
            if para.paragraph_id in excluded_ids:
                continue
            if para.paragraph_class != paragraph_class or not para.text:
                continue
            if para.text in template_map.placeholders or PLACEHOLDER_RE.fullmatch(para.text.strip()):
                continue
            if prefer_non_heading and para.is_heading_candidate:
                continue
            if prefer_plain:
                compact = para.text.strip()
                if NUMBERED_RE.match(compact) or compact.startswith(BULLET_PREFIXES) or compact.startswith('·'):
                    continue
                if not _is_clean_body_exemplar_text(compact):
                    continue
            if prefer_clean_number and not _is_clean_number_exemplar_text(para.text):
                continue
            relaxed.append(para)
            if (
                scope_paragraph is not None
                and _is_within_local_region(para)
                and abs(para.section_paragraph_index - scope_paragraph.section_paragraph_index) <= 6
            ):
                relaxed_local.append(para)
            compact_len = len(' '.join(para.text.split()))
            if 4 <= compact_len <= 120 and '\n' not in para.text and '\r' not in para.text:
                preferred.append(para)
                if (
                    scope_paragraph is not None
                    and _is_within_local_region(para)
                    and abs(para.section_paragraph_index - scope_paragraph.section_paragraph_index) <= 6
                ):
                    preferred_local.append(para)

        if scope_paragraph is not None:
            if local_only:
                for para in preferred_local + relaxed_local:
                    if para.text:
                        return para.text
                return fallback
            for para in preferred_local + relaxed_local + preferred + relaxed:
                if para.text:
                    return para.text
            return fallback

        for para in preferred + relaxed:
            if para.text:
                return para.text
        return fallback

    def _find_bullet_prefix(prefixes: tuple[str, ...], fallback: str | None = None, *, local_only: bool = False) -> str | None:
        preferred_local: list[TemplateParagraph] = []
        relaxed_local: list[TemplateParagraph] = []
        preferred: list[TemplateParagraph] = []
        relaxed: list[TemplateParagraph] = []
        for para in _candidate_paragraphs():
            if para.paragraph_id in excluded_ids:
                continue
            compact = para.text.strip() if para.text else ''
            if not compact or not compact.startswith(prefixes):
                continue
            relaxed.append(para)
            if (
                scope_paragraph is not None
                and _is_within_local_region(para)
                and abs(para.section_paragraph_index - scope_paragraph.section_paragraph_index) <= 6
            ):
                relaxed_local.append(para)
            compact_len = len(' '.join(compact.split()))
            if 4 <= compact_len <= 120 and '\n' not in compact and '\r' not in compact:
                preferred.append(para)
                if (
                    scope_paragraph is not None
                    and _is_within_local_region(para)
                    and abs(para.section_paragraph_index - scope_paragraph.section_paragraph_index) <= 6
                ):
                    preferred_local.append(para)

        if scope_paragraph is not None:
            if local_only:
                for para in preferred_local + relaxed_local:
                    if para.text:
                        return para.text
                return fallback
            for para in preferred_local + relaxed_local + preferred + relaxed:
                if para.text:
                    return para.text
            return fallback

        for para in preferred + relaxed:
            if para.text:
                return para.text
        return fallback

    section_heading = _find_first('heading')
    if scope_paragraph is not None:
        body = _find_first('body', prefer_plain=True, local_only=True)
    else:
        body = _find_first('body', fallback=section_heading, prefer_plain=True) or _find_first('body', fallback=section_heading)
    bullet = _find_first('bullet', local_only=scope_paragraph is not None)
    if scope_paragraph is not None:
        number = _find_first('number', prefer_non_heading=True, prefer_clean_number=True, local_only=True)
    else:
        number = _find_first('number', prefer_non_heading=True, prefer_clean_number=True) or _find_first('number', fallback=body, prefer_clean_number=True)
    bullet_level_1 = _find_bullet_prefix(('■', '●', '○', '▪'), fallback=bullet, local_only=scope_paragraph is not None)
    bullet_level_2 = _find_bullet_prefix(('-', '•', '*'), fallback=bullet_level_1 or bullet, local_only=scope_paragraph is not None)
    bullet_level_3 = _find_bullet_prefix(('·', '‣', '▪'), fallback=bullet_level_2 or bullet_level_1 or bullet, local_only=scope_paragraph is not None)
    if scope_paragraph is not None:
        number_level_1 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True, local_only=True)
        number_level_2 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True, local_only=True)
        number_level_3 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True, local_only=True)
    else:
        number_level_1 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True) or _find_first('number', fallback=number or body, prefer_clean_number=True)
        number_level_2 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True) or _find_first('number', fallback=number_level_1 or number or body, prefer_clean_number=True)
        number_level_3 = _find_first('number', prefer_non_heading=True, prefer_clean_number=True) or _find_first('number', fallback=number_level_2 or number_level_1 or number or body, prefer_clean_number=True)

    if section_heading:
        roles.append(StyleRoleBinding(role='section_heading', source_find=section_heading, paragraph_class='heading', note='첫 heading exemplar'))
    if body:
        roles.append(StyleRoleBinding(role='body', source_find=body, paragraph_class='body', note='첫 body exemplar'))
    if bullet:
        roles.append(StyleRoleBinding(role='bullet', source_find=bullet, paragraph_class='bullet', note='첫 bullet exemplar 또는 body fallback'))
    if bullet_level_1:
        roles.append(StyleRoleBinding(role='bullet_level_1', source_find=bullet_level_1, paragraph_class='bullet', note='1단 bullet exemplar'))
    if bullet_level_2:
        roles.append(StyleRoleBinding(role='bullet_level_2', source_find=bullet_level_2, paragraph_class='bullet', note='2단 bullet exemplar 또는 1단 fallback'))
    if bullet_level_3:
        roles.append(StyleRoleBinding(role='bullet_level_3', source_find=bullet_level_3, paragraph_class='bullet', note='3단 bullet exemplar 또는 2단 fallback'))
    if number:
        roles.append(StyleRoleBinding(role='number', source_find=number, paragraph_class='number', note='첫 number exemplar 또는 body fallback'))
    if number_level_1:
        roles.append(StyleRoleBinding(role='number_level_1', source_find=number_level_1, paragraph_class='number', note='1단 number exemplar'))
    if number_level_2:
        roles.append(StyleRoleBinding(role='number_level_2', source_find=number_level_2, paragraph_class='number', note='2단 number exemplar 또는 1단 fallback'))
    if number_level_3:
        roles.append(StyleRoleBinding(role='number_level_3', source_find=number_level_3, paragraph_class='number', note='3단 number exemplar 또는 2단 fallback'))

    return StyleRoleCatalog(template_id=template_map.template_id, roles=roles)


def parse_markdown_content(markdown: str) -> ContentSpec:
    lines = markdown.splitlines()
    drafts: list[_SectionDraft] = []
    current: _SectionDraft | None = None
    inspect_snapshot_id: str | None = None
    template_fingerprint: str | None = None
    paragraph_buffer: list[str] = []
    list_kind: str | None = None
    list_items: list[str] = []
    list_item_depths: list[int] = []
    pending_text_style: dict[str, Any] = {}
    pending_bullet_style: str | None = None
    pending_native_style: dict[str, Any] = {}
    pending_native_list_kind: str | None = None
    pending_list_level: int | None = None
    pending_list_source_find: str | None = None
    pending_native_only: bool = False

    def consume_pending_text_style() -> dict[str, Any]:
        nonlocal pending_text_style
        style = dict(pending_text_style)
        pending_text_style = {}
        return style

    def consume_pending_bullet_style() -> str | None:
        nonlocal pending_bullet_style
        style = pending_bullet_style
        pending_bullet_style = None
        return style

    def consume_pending_native_style() -> dict[str, Any]:
        nonlocal pending_native_style
        style = dict(pending_native_style)
        pending_native_style = {}
        return style

    def consume_pending_native_list_kind() -> str | None:
        nonlocal pending_native_list_kind
        kind = pending_native_list_kind
        pending_native_list_kind = None
        return kind

    def consume_pending_list_level() -> int | None:
        nonlocal pending_list_level
        value = pending_list_level
        pending_list_level = None
        return value

    def consume_pending_list_source_find() -> str | None:
        nonlocal pending_list_source_find
        value = pending_list_source_find
        pending_list_source_find = None
        return value

    def consume_pending_native_only() -> bool:
        nonlocal pending_native_only
        value = pending_native_only
        pending_native_only = False
        return value

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        if current is None:
            return
        text = ' '.join(part.strip() for part in paragraph_buffer if part.strip()).strip()
        if text:
            plain_text, inline_styles = _parse_inline_strong_markup(text)
            current.blocks.append(
                ContentBlock(
                    type='paragraph',
                    text=plain_text,
                    inline_styles=inline_styles,
                    style_role='body',
                    text_style=consume_pending_text_style(),
                    native_style=consume_pending_native_style(),
                    native_only=consume_pending_native_only(),
                )
            )
        paragraph_buffer = []

    def flush_list() -> None:
        nonlocal list_kind, list_items, list_item_depths
        if current is None:
            return
        if list_kind and list_items:
            normalized_items: list[str] = []
            item_inline_styles: list[list[InlineStyleToken]] = []
            for item in list_items:
                plain_item, inline_styles = _parse_inline_strong_markup(item)
                normalized_items.append(plain_item)
                item_inline_styles.append(inline_styles)
            current.blocks.append(
                ContentBlock(
                    type=list_kind,
                    items=normalized_items,
                    item_inline_styles=item_inline_styles,
                    item_depths=list_item_depths[:] or [1] * len(list_items),
                    style_role='body',
                    text_style=consume_pending_text_style(),
                    bullet_style=consume_pending_bullet_style(),
                    native_style=consume_pending_native_style(),
                    native_list_kind=consume_pending_native_list_kind(),
                    list_level=consume_pending_list_level(),
                    list_source_find=consume_pending_list_source_find(),
                    native_only=consume_pending_native_only(),
                )
            )
        list_kind = None
        list_items = []
        list_item_depths = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        heading_match = re.match(r'^(#{1,3})\s+(.+?)\s*$', stripped)
        if heading_match:
            flush_paragraph()
            flush_list()
            current = _SectionDraft(
                heading=heading_match.group(2).strip(),
                target_hint=heading_match.group(2).strip(),
                placeholder=None,
                resolved_target_id=None,
                blocks=[],
                table_patches=[],
                table_row_patches=[],
                table_record_patches=[],
                table_entry_find=None,
                table_entry_cell_addr=None,
                table_entry_cursor_pos=None,
                table_record_key_column=None,
            )
            drafts.append(current)
            continue
        if stripped.startswith('@inspect_snapshot_id:'):
            flush_paragraph()
            flush_list()
            inspect_snapshot_id = stripped.split(':', 1)[1].strip() or None
            continue
        if stripped.startswith('@template_fingerprint:'):
            flush_paragraph()
            flush_list()
            template_fingerprint = stripped.split(':', 1)[1].strip() or None
            continue
        if current is None:
            continue
        if stripped.startswith('@target:'):
            flush_paragraph()
            flush_list()
            current.target_hint = stripped.split(':', 1)[1].strip() or current.target_hint
            continue
        if stripped.startswith('@bold:'):
            flush_paragraph()
            flush_list()
            pending_text_style['bold'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@bold')
            continue
        if stripped.startswith('@italic:'):
            flush_paragraph()
            flush_list()
            pending_text_style['italic'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@italic')
            continue
        if stripped.startswith('@underline:'):
            flush_paragraph()
            flush_list()
            pending_text_style['underline'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@underline')
            continue
        if stripped.startswith('@underline_shape:'):
            flush_paragraph()
            flush_list()
            pending_text_style['underline_shape'] = _parse_int_directive(stripped.split(':', 1)[1], field_name='@underline_shape')
            continue
        if stripped.startswith('@font:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@font directive requires a non-empty font name')
            pending_text_style['face_name'] = value
            continue
        if stripped.startswith('@font_size_pt:'):
            flush_paragraph()
            flush_list()
            pending_text_style['height_pt'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@font_size_pt')
            continue
        if stripped.startswith('@text_color:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@text_color directive requires a non-empty value')
            pending_text_style['text_color_rgb'] = value
            continue
        if stripped.startswith('@shade_color:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@shade_color directive requires a non-empty value')
            pending_text_style['shade_color_rgb'] = value
            continue
        if stripped.startswith('@bullet_style:'):
            flush_paragraph()
            flush_list()
            raise TemplateEngineError('@bullet_style is no longer supported. Use Hancom native @list plus @list_level/@list_source_find, or write literal characters directly in text when you intentionally want characters.')
            continue
        if stripped.startswith('@list:'):
            flush_paragraph()
            flush_list()
            pending_native_list_kind = _normalize_native_list_kind(stripped.split(':', 1)[1])
            continue
        if stripped.startswith('@native_list_kind:'):
            flush_paragraph()
            flush_list()
            pending_native_list_kind = _normalize_native_list_kind(stripped.split(':', 1)[1])
            continue
        if stripped.startswith('@list_level:'):
            flush_paragraph()
            flush_list()
            parsed_level = _parse_int_directive(stripped.split(':', 1)[1], field_name='@list_level')
            if parsed_level < 1:
                raise TemplateEngineError('@list_level directive must be a positive integer')
            pending_list_level = parsed_level
            continue
        if stripped.startswith('@list_source_find:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@list_source_find directive requires a non-empty value')
            pending_list_source_find = value
            continue
        if stripped.startswith('@native_only:'):
            flush_paragraph()
            flush_list()
            pending_native_only = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@native_only')
            continue
        if stripped.startswith('@native_indent:'):
            flush_paragraph()
            flush_list()
            pending_native_style['indentation'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@native_indent')
            continue
        if stripped.startswith('@native_left_margin:'):
            flush_paragraph()
            flush_list()
            pending_native_style['left_margin'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@native_left_margin')
            continue
        if stripped.startswith('@native_right_margin:'):
            flush_paragraph()
            flush_list()
            pending_native_style['right_margin'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@native_right_margin')
            continue
        if stripped.startswith('@native_prev_spacing:'):
            flush_paragraph()
            flush_list()
            pending_native_style['prev_spacing'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@native_prev_spacing')
            continue
        if stripped.startswith('@native_next_spacing:'):
            flush_paragraph()
            flush_list()
            pending_native_style['next_spacing'] = _parse_float_directive(stripped.split(':', 1)[1], field_name='@native_next_spacing')
            continue
        if stripped.startswith('@native_line_spacing:'):
            flush_paragraph()
            flush_list()
            pending_native_style['line_spacing'] = int(_parse_float_directive(stripped.split(':', 1)[1], field_name='@native_line_spacing'))
            continue
        if stripped.startswith('@native_align:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@native_align directive requires a non-empty value')
            pending_native_style['align'] = value
            continue
        if stripped.startswith('@native_pagebreak_before:'):
            flush_paragraph()
            flush_list()
            pending_native_style['pagebreak_before'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@native_pagebreak_before')
            continue
        if stripped.startswith('@native_keep_with_next:'):
            flush_paragraph()
            flush_list()
            pending_native_style['keep_with_next'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@native_keep_with_next')
            continue
        if stripped.startswith('@native_keep_lines_together:'):
            flush_paragraph()
            flush_list()
            pending_native_style['keep_lines_together'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@native_keep_lines_together')
            continue
        if stripped.startswith('@native_widow_orphan:'):
            flush_paragraph()
            flush_list()
            pending_native_style['widow_orphan'] = _parse_bool_directive(stripped.split(':', 1)[1], field_name='@native_widow_orphan')
            continue
        if stripped.startswith('@resolved_target_id:'):
            flush_paragraph()
            flush_list()
            current.resolved_target_id = stripped.split(':', 1)[1].strip() or None
            continue
        if stripped.startswith('@table_entry_find:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@table_entry_find directive requires a non-empty value')
            current.table_entry_find = value
            continue
        if stripped.startswith('@table_entry_cell:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@table_entry_cell directive requires a non-empty value')
            current.table_entry_cell_addr = normalize_table_cell_addr(value)
            continue
        if stripped.startswith('@table_entry_cursor_pos:'):
            flush_paragraph()
            flush_list()
            current.table_entry_cursor_pos = _parse_cursor_pos_directive(
                stripped.split(':', 1)[1],
                field_name='@table_entry_cursor_pos',
            )
            continue
        if stripped.startswith('@table_record_key_column:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@table_record_key_column directive requires a non-empty value')
            current.table_record_key_column = value
            continue
        if stripped.startswith('@table_cell:'):
            flush_paragraph()
            flush_list()
            current.table_patches.append(_parse_table_cell_directive(stripped.split(':', 1)[1]))
            continue
        if stripped.startswith('@table_row:'):
            flush_paragraph()
            flush_list()
            current.table_row_patches.append(_parse_table_row_directive(stripped.split(':', 1)[1]))
            continue
        if stripped.startswith('@table_record:'):
            flush_paragraph()
            flush_list()
            current.table_record_patches.append(_parse_table_record_directive(stripped.split(':', 1)[1]))
            continue
        if stripped.startswith('@placeholder:'):
            flush_paragraph()
            flush_list()
            value = stripped.split(':', 1)[1].strip()
            if not value:
                raise TemplateEngineError('@placeholder directive requires a non-empty token')
            current.placeholder = canonicalize_placeholder_token(value)
            continue
        list_item = _parse_authoring_list_item(raw_line)
        if list_item:
            flush_paragraph()
            next_kind, item_text, depth, inferred_bullet_style = list_item
            if list_kind not in {None, next_kind}:
                flush_list()
            list_kind = next_kind
            if next_kind == 'bullet_list' and pending_bullet_style is None and inferred_bullet_style:
                pending_bullet_style = inferred_bullet_style
            list_items.append(item_text)
            list_item_depths.append(depth)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if list_kind:
            flush_list()
        paragraph_buffer.append(stripped)

    flush_paragraph()
    flush_list()

    sections = [
        ContentSection(
            section_key=draft.heading,
            target_hint=draft.target_hint,
            placeholder=draft.placeholder,
            resolved_target_id=draft.resolved_target_id,
            blocks=draft.blocks,
            table_patches=draft.table_patches,
            table_row_patches=draft.table_row_patches,
            table_record_patches=draft.table_record_patches,
            table_entry_find=draft.table_entry_find,
            table_entry_cell_addr=draft.table_entry_cell_addr,
            table_entry_cursor_pos=draft.table_entry_cursor_pos,
            table_record_key_column=draft.table_record_key_column,
        )
        for draft in drafts
        if draft.blocks or draft.table_patches or draft.table_row_patches or draft.table_record_patches
    ]
    if not sections:
        raise TemplateEngineError('markdown authoring must contain at least one heading with paragraph or list content')
    return ContentSpec(
        inspect_snapshot_id=inspect_snapshot_id,
        template_fingerprint=template_fingerprint,
        sections=sections,
    )


def _token_set(text: str) -> set[str]:
    return {token for token in re.split(r'[^0-9A-Za-z가-힣]+', _normalize_text(text)) if token}


def _build_exact_target_text_mismatch_warning(target_text: str, rendered_text: str) -> dict[str, Any] | None:
    target_compact = ' '.join(str(target_text or '').split())
    rendered_compact = ' '.join(str(rendered_text or '').split())
    if not target_compact or not rendered_compact:
        return None

    target_tokens = _token_set(target_compact)
    rendered_tokens = _token_set(rendered_compact)
    if not target_tokens or not rendered_tokens:
        return None

    overlap = target_tokens & rendered_tokens
    overlap_count = len(overlap)
    shared_ratio = overlap_count / max(1, min(len(target_tokens), len(rendered_tokens)))
    rendered_coverage = overlap_count / max(1, len(rendered_tokens))
    target_prefix = target_compact[:1]
    rendered_prefix = rendered_compact[:1]
    bullet_prefixes = ('■', '●', '○', '▪', '-', '•', '*', '·')
    bullet_prefix_mismatch = (
        target_prefix != rendered_prefix
        and (target_prefix in bullet_prefixes or rendered_prefix in bullet_prefixes)
    )
    short_target_long_render = (
        len(target_tokens) <= 4
        and len(rendered_tokens) >= 8
        and rendered_coverage <= 0.2
    )

    if not short_target_long_render and (overlap_count >= 2 or shared_ratio >= 0.35):
        return None
    if overlap_count == 0 or bullet_prefix_mismatch or short_target_long_render:
        return {
            'overlap_count': overlap_count,
            'shared_ratio': round(shared_ratio, 3),
            'rendered_coverage': round(rendered_coverage, 3),
            'target_token_count': len(target_tokens),
            'rendered_token_count': len(rendered_tokens),
            'target_preview': target_compact[:140],
            'rendered_preview': rendered_compact[:140],
            'bullet_prefix_mismatch': bullet_prefix_mismatch,
            'short_target_long_render': short_target_long_render,
        }
    return None


def _is_generic_target_hint(text: str) -> bool:
    tokens = _token_set(text)
    if not tokens:
        return False
    return all(token in GENERIC_TARGET_HINTS for token in tokens)


def _find_paragraph(template_map: TemplateMap, paragraph_id: str) -> TemplateParagraph:
    for paragraph in template_map.paragraphs:
        if paragraph.paragraph_id == paragraph_id:
            return paragraph
    raise TemplateEngineError(f'resolved_target_id not found in template_map.paragraphs: {paragraph_id!r}')


def _paragraph_position(paragraph: TemplateParagraph) -> tuple[str, int]:
    return paragraph.section, paragraph.section_paragraph_index


def _find_nearest_anchor_for_paragraph(template_map: TemplateMap, paragraph: TemplateParagraph) -> TemplateAnchor | None:
    matching_anchor = None
    paragraph_position = paragraph.section_paragraph_index
    for anchor in template_map.anchors:
        if anchor.section != paragraph.section:
            continue
        anchor_position = int(anchor.paragraph_id.rsplit('#p', 1)[-1])
        if anchor_position <= paragraph_position:
            matching_anchor = anchor
        elif matching_anchor is not None:
            break
    return matching_anchor


def _find_anchor_body_end_paragraph(template_map: TemplateMap, anchor: TemplateAnchor) -> TemplateParagraph:
    anchor_start_idx = int(anchor.paragraph_id.rsplit('#p', 1)[-1])
    next_anchor_start_idx: int | None = None
    for candidate in template_map.anchors:
        if candidate.section != anchor.section:
            continue
        candidate_idx = int(candidate.paragraph_id.rsplit('#p', 1)[-1])
        if candidate_idx > anchor_start_idx:
            next_anchor_start_idx = candidate_idx
            break

    body_start = _find_paragraph(template_map, anchor.body_paragraph_id)
    body_end = body_start
    for paragraph in template_map.paragraphs:
        if paragraph.section != anchor.section:
            continue
        if paragraph.section_paragraph_index < body_start.section_paragraph_index:
            continue
        if next_anchor_start_idx is not None and paragraph.section_paragraph_index >= next_anchor_start_idx:
            break
        if paragraph.text:
            body_end = paragraph
    return body_end


def _format_paragraph_range(start_paragraph_id: str, end_paragraph_id: str) -> str:
    if start_paragraph_id == end_paragraph_id:
        return start_paragraph_id
    return f'{start_paragraph_id}..{end_paragraph_id}'


def _target_kind_for_scope(start_paragraph_id: str, end_paragraph_id: str) -> str:
    return 'body_paragraph' if start_paragraph_id == end_paragraph_id else 'paragraph_range'


def _truncate_preview(text: str, max_chars: int = 120) -> str:
    compact = ' '.join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + '…'


def _combine_preview_lines(first_text: str, last_text: str) -> str:
    if not last_text or last_text == first_text:
        return _truncate_preview(first_text)
    return _truncate_preview(f'{first_text} … {last_text}')


def _normalize_confidence(score: float) -> float:
    return round(min(max(score, 0.0), 1.0), 3)


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.9:
        return 'high'
    if confidence >= 0.75:
        return 'medium'
    return 'low'


def _build_preview_text(start_paragraph: TemplateParagraph, end_paragraph: TemplateParagraph) -> str:
    if start_paragraph.paragraph_id == end_paragraph.paragraph_id:
        return _truncate_preview(start_paragraph.text)
    return _combine_preview_lines(start_paragraph.text, end_paragraph.text)


def _build_anchor_scope_preview_text(template_map: TemplateMap, anchor: TemplateAnchor) -> str:
    body_start = _find_paragraph(template_map, anchor.body_paragraph_id)
    body_end = _find_anchor_body_end_paragraph(template_map, anchor)
    return _build_preview_text(body_start, body_end)


def _find_prev_nonempty_paragraph(template_map: TemplateMap, paragraph: TemplateParagraph) -> TemplateParagraph | None:
    prev: TemplateParagraph | None = None
    for item in template_map.paragraphs:
        if item.section != paragraph.section:
            continue
        if item.section_paragraph_index >= paragraph.section_paragraph_index:
            break
        if item.text.strip():
            prev = item
    return prev


def _find_next_nonempty_paragraph(template_map: TemplateMap, paragraph: TemplateParagraph) -> TemplateParagraph | None:
    for item in template_map.paragraphs:
        if item.section != paragraph.section:
            continue
        if item.section_paragraph_index <= paragraph.section_paragraph_index:
            continue
        if item.text.strip():
            return item
    return None


def _build_candidate_selection_preview(template_map: TemplateMap, anchor: TemplateAnchor) -> tuple[str, str, str]:
    heading_paragraph = _find_paragraph(template_map, anchor.paragraph_id)
    body_start = _find_paragraph(template_map, anchor.body_paragraph_id)
    body_end = _find_anchor_body_end_paragraph(template_map, anchor)
    before = _find_prev_nonempty_paragraph(template_map, heading_paragraph)
    after = _find_next_nonempty_paragraph(template_map, body_end)
    before_text = _truncate_preview(before.text) if before else ''
    after_text = _truncate_preview(after.text) if after else ''
    focus = _build_preview_text(body_start, body_end)
    parts = [part for part in [before_text, f'[{anchor.heading_text}]', focus, after_text] if part]
    return before_text, after_text, ' | '.join(parts)


def _build_why_recommended(signals: list[str]) -> str:
    if not signals:
        return 'best available heading/body match'
    reasons: list[str] = []
    for signal in signals:
        if signal == 'exact_heading':
            reasons.append('exact heading match')
        elif signal == 'normalized_contains':
            reasons.append('normalized heading containment')
        elif signal == 'body_contains':
            reasons.append('body text supports the match')
        elif signal.startswith('token_overlap:'):
            overlap = signal.split(':', 1)[1]
            reasons.append(f'token overlap {overlap}')
        else:
            reasons.append(signal)
    return ', '.join(reasons)


def _has_semantic_anchor_signal(signals: list[str]) -> bool:
    semantic_prefixes = (
        'exact_heading',
        'normalized_contains',
        'token_overlap:',
        'generic_hint_token_overlap:',
        'body_contains',
        'section_key_exact_heading',
        'section_key_contains',
        'section_key_token_overlap:',
        'authored_preview_exact_body',
        'authored_preview_contains_body',
        'authored_preview_token_overlap:',
        'authored_preview_exact_scope',
        'authored_preview_contains_scope',
        'authored_preview_scope_token_overlap:',
    )
    return any(signal.startswith(semantic_prefixes) for signal in signals)


def _paragraphs_between(
    template_map: TemplateMap,
    start_paragraph_id: str,
    end_paragraph_id: str,
) -> list[TemplateParagraph]:
    start_paragraph = _find_paragraph(template_map, start_paragraph_id)
    end_paragraph = _find_paragraph(template_map, end_paragraph_id)
    if start_paragraph.section != end_paragraph.section:
        return []
    return [
        paragraph
        for paragraph in template_map.paragraphs
        if paragraph.section == start_paragraph.section
        and start_paragraph.section_paragraph_index <= paragraph.section_paragraph_index <= end_paragraph.section_paragraph_index
    ]


def _anchor_scope_metrics(template_map: TemplateMap, anchor: TemplateAnchor) -> dict[str, Any]:
    body_end = _find_anchor_body_end_paragraph(template_map, anchor)
    paragraphs = _paragraphs_between(template_map, anchor.body_paragraph_id, body_end.paragraph_id)
    table_count = sum(paragraph.table_count for paragraph in paragraphs)
    picture_count = sum(paragraph.picture_count for paragraph in paragraphs)
    control_count = sum(paragraph.control_count for paragraph in paragraphs)
    field_count = sum(paragraph.field_count for paragraph in paragraphs)
    page_break_count = sum(paragraph.page_break_count for paragraph in paragraphs)
    return {
        'paragraph_count': len(paragraphs),
        'table_count': table_count,
        'picture_count': picture_count,
        'control_count': control_count,
        'field_count': field_count,
        'page_break_count': page_break_count,
        'is_complex': any((table_count, picture_count, control_count, field_count, page_break_count)),
    }


def _estimate_authored_units(blocks: list[ContentBlock] | None) -> dict[str, Any]:
    authored_blocks = blocks or []
    paragraph_units = 0
    list_item_units = 0
    has_list = False
    for block in authored_blocks:
        if block.type == 'paragraph':
            if (block.text or '').strip():
                paragraph_units += 1
        elif block.type in {'bullet_list', 'numbered_list'}:
            item_count = len([item for item in block.items if (item or '').strip()])
            if item_count > 0:
                list_item_units += item_count
                has_list = True
    total_units = paragraph_units + list_item_units
    return {
        'paragraph_units': paragraph_units,
        'list_item_units': list_item_units,
        'total_units': total_units,
        'has_list': has_list,
    }


def _is_ambiguous_recommendation(scored: list[tuple[float, TemplateAnchor, list[str]]]) -> bool:
    if len(scored) < 2:
        return False
    top_score = scored[0][0]
    second_score = scored[1][0]
    return top_score < 0.9 or (top_score - second_score) < 0.12


def _score_section_anchors(
    template_map: TemplateMap,
    target_hint: str,
    *,
    section_key: str | None = None,
    authored_preview_text: str | None = None,
    authored_blocks: list[ContentBlock] | None = None,
    prefer_non_structural: bool = True,
) -> list[tuple[float, TemplateAnchor, list[str]]]:
    normalized_hint = _normalize_text(target_hint)
    hint_tokens = _token_set(target_hint)
    generic_target_hint = _is_generic_target_hint(target_hint)
    normalized_section_key = _normalize_text(section_key or '')
    section_key_tokens = _token_set(section_key or '')
    normalized_authored_preview = _normalize_text(authored_preview_text or '')
    authored_preview_tokens = _token_set(authored_preview_text or '')
    authored_units = _estimate_authored_units(authored_blocks)
    if not normalized_hint:
        raise TemplateEngineError('section target hint must not be empty')

    scored: list[tuple[float, TemplateAnchor, list[str]]] = []
    for anchor in template_map.anchors:
        score = anchor.confidence_base
        signals: list[str] = []
        anchor_scope_preview = _build_anchor_scope_preview_text(template_map, anchor)
        scope_metrics = _anchor_scope_metrics(template_map, anchor)
        if anchor.normalized_heading == normalized_hint:
            score += 0.2 if generic_target_hint else 0.45
            signals.append('exact_heading_generic_hint' if generic_target_hint else 'exact_heading')
        elif normalized_hint in anchor.normalized_heading or anchor.normalized_heading in normalized_hint:
            score += 0.08 if generic_target_hint else 0.28
            signals.append('normalized_contains_generic_hint' if generic_target_hint else 'normalized_contains')
        overlap = len(hint_tokens & _token_set(anchor.heading_text))
        if overlap:
            score += min(0.06, 0.02 * overlap) if generic_target_hint else min(0.18, 0.06 * overlap)
            signals.append(f'generic_hint_token_overlap:{overlap}' if generic_target_hint else f'token_overlap:{overlap}')
        if anchor.body_anchor_text and normalized_hint in _normalize_text(anchor.body_anchor_text):
            score += 0.02 if generic_target_hint else 0.08
            signals.append('body_contains_generic_hint' if generic_target_hint else 'body_contains')
        if normalized_section_key:
            if anchor.normalized_heading == normalized_section_key:
                score += 0.24
                signals.append('section_key_exact_heading')
            elif normalized_section_key in anchor.normalized_heading or anchor.normalized_heading in normalized_section_key:
                score += 0.14
                signals.append('section_key_contains')
            section_overlap = len(section_key_tokens & _token_set(anchor.heading_text))
            if section_overlap:
                score += min(0.12, 0.04 * section_overlap)
                signals.append(f'section_key_token_overlap:{section_overlap}')
        if normalized_authored_preview and anchor.body_anchor_text:
            normalized_body = _normalize_text(anchor.body_anchor_text)
            if normalized_body == normalized_authored_preview:
                score += 0.32
                signals.append('authored_preview_exact_body')
            elif normalized_authored_preview in normalized_body or normalized_body in normalized_authored_preview:
                score += 0.18
                signals.append('authored_preview_contains_body')
            preview_overlap = len(authored_preview_tokens & _token_set(anchor.body_anchor_text))
            if preview_overlap:
                score += min(0.16, 0.04 * preview_overlap)
                signals.append(f'authored_preview_token_overlap:{preview_overlap}')
        if normalized_authored_preview and anchor_scope_preview:
            normalized_scope_preview = _normalize_text(anchor_scope_preview)
            if normalized_scope_preview == normalized_authored_preview:
                score += 0.26
                signals.append('authored_preview_exact_scope')
            elif normalized_authored_preview in normalized_scope_preview or normalized_scope_preview in normalized_authored_preview:
                score += 0.14
                signals.append('authored_preview_contains_scope')
            scope_overlap = len(authored_preview_tokens & _token_set(anchor_scope_preview))
            if scope_overlap:
                score += min(0.12, 0.03 * scope_overlap)
                signals.append(f'authored_preview_scope_token_overlap:{scope_overlap}')

        semantic_match = _has_semantic_anchor_signal(signals)
        paragraph_count = int(scope_metrics.get('paragraph_count', 0) or 0)
        total_units = int(authored_units.get('total_units', 0) or 0)
        list_item_units = int(authored_units.get('list_item_units', 0) or 0)
        has_list = bool(authored_units.get('has_list'))
        if semantic_match and total_units > 0 and paragraph_count > 0:
            shortfall = total_units - paragraph_count
            if shortfall > 0:
                score -= min(0.28, 0.05 * shortfall + (0.08 if has_list else 0.04))
                signals.append(f'paragraph_capacity_shortfall:{paragraph_count}/{total_units}')
            else:
                closeness = abs(paragraph_count - total_units)
                if closeness <= 3:
                    score += max(0.0, 0.14 - 0.03 * closeness)
                    signals.append(f'paragraph_capacity_fit:{paragraph_count}/{total_units}')
        if semantic_match and has_list and list_item_units > 0 and paragraph_count > 0:
            list_shortfall = list_item_units - paragraph_count
            if list_shortfall > 0:
                score -= min(0.32, 0.06 * list_shortfall + 0.08)
                signals.append(f'list_capacity_shortfall:{paragraph_count}/{list_item_units}')
            elif paragraph_count <= list_item_units + 2:
                score += 0.08
                signals.append(f'list_capacity_fit:{paragraph_count}/{list_item_units}')
        if semantic_match and prefer_non_structural:
            structural_penalty = 0.0
            if scope_metrics.get('table_count'):
                structural_penalty += 0.18 + min(0.16, 0.04 * int(scope_metrics['table_count']))
            if scope_metrics.get('picture_count'):
                structural_penalty += 0.10 + min(0.12, 0.03 * int(scope_metrics['picture_count']))
            if scope_metrics.get('control_count'):
                structural_penalty += 0.08 + min(0.10, 0.02 * int(scope_metrics['control_count']))
            if scope_metrics.get('field_count'):
                structural_penalty += min(0.08, 0.02 * int(scope_metrics['field_count']))
            if scope_metrics.get('page_break_count'):
                structural_penalty += 0.12
            if structural_penalty > 0:
                score -= min(0.52, structural_penalty)
                signals.append(
                    'non_structural_preference:'
                    f"tables={scope_metrics.get('table_count', 0)},"
                    f"pictures={scope_metrics.get('picture_count', 0)},"
                    f"controls={scope_metrics.get('control_count', 0)},"
                    f"fields={scope_metrics.get('field_count', 0)},"
                    f"page_breaks={scope_metrics.get('page_break_count', 0)}"
                )
        if not semantic_match:
            score -= 0.35
            signals.append('no_semantic_match')
        scored.append((score, anchor, signals))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored
def build_section_target_recommendation(
    template_map: TemplateMap,
    *,
    section_key: str,
    target_hint: str,
    authored_preview_text: str | None = None,
    authored_blocks: list[ContentBlock] | None = None,
    prefer_non_structural: bool = True,
    limit: int = 3,
) -> SectionTargetRecommendation:
    scored = _score_section_anchors(
        template_map,
        target_hint,
        section_key=section_key,
        authored_preview_text=authored_preview_text,
        authored_blocks=authored_blocks,
        prefer_non_structural=prefer_non_structural,
    )
    semantic_scored = [item for item in scored if _has_semantic_anchor_signal(item[2])]
    if semantic_scored:
        scored = semantic_scored
    if not scored:
        raise TemplateEngineError('template map does not contain any anchors to resolve')

    is_ambiguous = _is_ambiguous_recommendation(scored)
    show_limit = min(limit, 3) if is_ambiguous else 1
    candidates: list[TargetCandidate] = []
    for score, anchor, signals in scored[:show_limit]:
        body_start = _find_paragraph(template_map, anchor.body_paragraph_id)
        body_end = _find_anchor_body_end_paragraph(template_map, anchor)
        paragraph_range = _format_paragraph_range(anchor.body_paragraph_id, body_end.paragraph_id)
        target_kind = _target_kind_for_scope(anchor.body_paragraph_id, body_end.paragraph_id)
        confidence = _normalize_confidence(score)
        context_before, context_after, selection_preview = _build_candidate_selection_preview(template_map, anchor)
        candidates.append(
            TargetCandidate(
                matched_anchor_id=anchor.anchor_id,
                matched_heading=anchor.heading_text,
                heading_paragraph_id=anchor.paragraph_id,
                body_start_paragraph_id=anchor.body_paragraph_id,
                body_end_paragraph_id=body_end.paragraph_id,
                recommended_resolved_target_id=paragraph_range,
                body_anchor_text=anchor.body_anchor_text,
                resolved_target_id=paragraph_range,
                target_kind=target_kind,
                heading_text=anchor.heading_text,
                body_scope=BodyScope(
                    heading_paragraph_id=anchor.paragraph_id,
                    body_start_paragraph_id=anchor.body_paragraph_id,
                    body_end_paragraph_id=body_end.paragraph_id,
                ),
                paragraph_range=paragraph_range,
                preview_text=_build_preview_text(body_start, body_end),
                context_before=context_before,
                context_after=context_after,
                selection_preview=selection_preview,
                confidence=confidence,
                confidence_label=_confidence_label(confidence),
                matched_signals=signals,
                why_recommended=_build_why_recommended(signals),
            )
        )

    top_candidate = candidates[0] if candidates else None
    selection_mode = 'choose_one_of_top3' if is_ambiguous else 'recommended_single'
    selection_instruction = (
        'Choose one candidate resolved_target_id from the top 3 and resend with that exact target.'
        if is_ambiguous
        else 'Use the recommended_resolved_target_id and resend with that exact target.'
    )
    return SectionTargetRecommendation(
        section_key=section_key,
        target_hint=target_hint,
        recommended_resolved_target_id=top_candidate.recommended_resolved_target_id if top_candidate else None,
        recommended_target_id=top_candidate.resolved_target_id if top_candidate else None,
        recommended_target_kind=top_candidate.target_kind if top_candidate else None,
        recommended_heading_text=top_candidate.heading_text if top_candidate else None,
        copy_ready_resolved_target_id=top_candidate.resolved_target_id if top_candidate else None,
        selection_mode=selection_mode,
        selection_instruction=selection_instruction,
        is_ambiguous=is_ambiguous,
        candidate_count=len(scored),
        showing_candidate_count=len(candidates),
        candidates=candidates,
    )


def _normalize_heading_like_exact_target_to_body_scope(
    template_map: TemplateMap,
    *,
    anchor: TemplateAnchor | None,
    paragraph: TemplateParagraph,
    section_key: str,
    target_hint: str,
    resolved_via: str,
    matched_signals: list[str],
    original_target_id: str,
) -> tuple[TemplateAnchor, TemplateParagraph, TemplateParagraph | None, AnchorResolution] | None:
    if paragraph.paragraph_class not in {'heading', 'number'}:
        return None
    if anchor is None:
        return None

    body_start = _find_paragraph(template_map, anchor.body_paragraph_id)
    if body_start.paragraph_id == paragraph.paragraph_id:
        return None

    body_end = _find_anchor_body_end_paragraph(template_map, anchor)
    normalized_target_id = _format_paragraph_range(body_start.paragraph_id, body_end.paragraph_id)
    resolution = AnchorResolution(
        section_key=section_key,
        target_hint=target_hint,
        inspect_snapshot_id=template_map.inspect_snapshot_id,
        resolved_via=f'{resolved_via}_body_scope',
        resolved_target_id=normalized_target_id,
        matched_anchor_id=anchor.anchor_id,
        matched_heading=anchor.heading_text,
        body_anchor_text=body_start.text or anchor.body_anchor_text,
        confidence=1.0,
        matched_signals=[
            *matched_signals,
            'heading_like_exact_target_shifted_to_body_scope',
            f'original_target_id:{original_target_id}',
        ],
        alternatives=[original_target_id],
    )
    return anchor, body_start, (body_end if body_end.paragraph_id != body_start.paragraph_id else None), resolution


def resolve_exact_target(
    template_map: TemplateMap,
    resolved_target_id: str,
    *,
    section_key: str,
    target_hint: str,
) -> tuple[TemplateAnchor, TemplateParagraph, TemplateParagraph | None, AnchorResolution]:
    target_id = str(resolved_target_id).strip()
    if not target_id:
        raise TemplateEngineError('resolved_target_id must not be empty when provided')

    range_match = PARAGRAPH_RANGE_RE.fullmatch(target_id)
    if range_match:
        start_paragraph = _find_paragraph(template_map, range_match.group('start'))
        end_paragraph = _find_paragraph(template_map, range_match.group('end'))
        if _paragraph_position(start_paragraph)[0] != _paragraph_position(end_paragraph)[0]:
            raise TemplateEngineError('resolved_target_id paragraph range must stay within one section')
        if start_paragraph.section_paragraph_index > end_paragraph.section_paragraph_index:
            raise TemplateEngineError('resolved_target_id paragraph range start must be before or equal to end')
        anchor = _find_nearest_anchor_for_paragraph(template_map, start_paragraph)
        if anchor is None:
            raise TemplateEngineError(f'resolved_target_id {target_id!r} did not map to a known anchor context')
        normalized_heading_scope = None
        if start_paragraph.paragraph_id == end_paragraph.paragraph_id:
            normalized_heading_scope = _normalize_heading_like_exact_target_to_body_scope(
                template_map,
                anchor=anchor,
                paragraph=start_paragraph,
                section_key=section_key,
                target_hint=target_hint,
                resolved_via='exact_paragraph_range_id',
                matched_signals=['exact_paragraph_range_id'],
                original_target_id=target_id,
            )
        if normalized_heading_scope is not None:
            return normalized_heading_scope
        resolution = AnchorResolution(
            section_key=section_key,
            target_hint=target_hint,
            inspect_snapshot_id=template_map.inspect_snapshot_id,
            resolved_via='exact_paragraph_range_id',
            resolved_target_id=target_id,
            matched_anchor_id=anchor.anchor_id,
            matched_heading=anchor.heading_text,
            body_anchor_text=start_paragraph.text,
            confidence=1.0,
            matched_signals=['exact_paragraph_range_id'],
            alternatives=[],
        )
        return anchor, start_paragraph, end_paragraph, resolution

    for anchor in template_map.anchors:
        if anchor.anchor_id == target_id:
            paragraph = _find_paragraph(template_map, anchor.body_paragraph_id)
            resolution = AnchorResolution(
                section_key=section_key,
                target_hint=target_hint,
                inspect_snapshot_id=template_map.inspect_snapshot_id,
                resolved_via='exact_anchor_id',
                resolved_target_id=anchor.body_paragraph_id,
                matched_anchor_id=anchor.anchor_id,
                matched_heading=anchor.heading_text,
                body_anchor_text=paragraph.text or anchor.body_anchor_text,
                confidence=1.0,
                matched_signals=['exact_anchor_id'],
                alternatives=[],
            )
            return anchor, paragraph, None, resolution

        if anchor.body_paragraph_id == target_id:
            paragraph = _find_paragraph(template_map, anchor.body_paragraph_id)
            resolution = AnchorResolution(
                section_key=section_key,
                target_hint=target_hint,
                inspect_snapshot_id=template_map.inspect_snapshot_id,
                resolved_via='exact_body_paragraph_id',
                resolved_target_id=paragraph.paragraph_id,
                matched_anchor_id=anchor.anchor_id,
                matched_heading=anchor.heading_text,
                body_anchor_text=paragraph.text,
                confidence=1.0,
                matched_signals=['exact_body_paragraph_id'],
                alternatives=[],
            )
            return anchor, paragraph, None, resolution

        if anchor.paragraph_id == target_id:
            paragraph = _find_paragraph(template_map, anchor.body_paragraph_id)
            resolution = AnchorResolution(
                section_key=section_key,
                target_hint=target_hint,
                inspect_snapshot_id=template_map.inspect_snapshot_id,
                resolved_via='exact_heading_paragraph_id',
                resolved_target_id=paragraph.paragraph_id,
                matched_anchor_id=anchor.anchor_id,
                matched_heading=anchor.heading_text,
                body_anchor_text=paragraph.text or anchor.body_anchor_text,
                confidence=1.0,
                matched_signals=['exact_heading_paragraph_id'],
                alternatives=[],
            )
            return anchor, paragraph, None, resolution

    paragraph = _find_paragraph(template_map, target_id)
    matching_anchor = _find_nearest_anchor_for_paragraph(template_map, paragraph)
    if matching_anchor is None:
        raise TemplateEngineError(f'resolved_target_id {target_id!r} did not map to a known anchor context')
    normalized_heading_scope = _normalize_heading_like_exact_target_to_body_scope(
        template_map,
        anchor=matching_anchor,
        paragraph=paragraph,
        section_key=section_key,
        target_hint=target_hint,
        resolved_via='exact_paragraph_id',
        matched_signals=['exact_paragraph_id'],
        original_target_id=target_id,
    )
    if normalized_heading_scope is not None:
        return normalized_heading_scope
    resolution = AnchorResolution(
        section_key=section_key,
        target_hint=target_hint,
        inspect_snapshot_id=template_map.inspect_snapshot_id,
        resolved_via='exact_paragraph_id',
        resolved_target_id=paragraph.paragraph_id,
        matched_anchor_id=matching_anchor.anchor_id,
        matched_heading=matching_anchor.heading_text,
        body_anchor_text=paragraph.text,
        confidence=0.99,
        matched_signals=['exact_paragraph_id'],
        alternatives=[],
    )
    return matching_anchor, paragraph, None, resolution


def resolve_section_anchor(template_map: TemplateMap, target_hint: str) -> tuple[TemplateAnchor, AnchorResolution]:
    scored = _score_section_anchors(template_map, target_hint, section_key=target_hint)
    if not scored:
        raise TemplateEngineError('template map does not contain any anchors to resolve')

    best_score, best_anchor, best_signals = scored[0]
    if best_score < 0.6:
        raise TemplateEngineError(
            f'failed to resolve section {target_hint!r} with sufficient confidence; best heading={best_anchor.heading_text!r} score={best_score:.2f}'
        )

    alternatives = [f'{anchor.heading_text} ({score:.2f})' for score, anchor, _ in scored[1:4]]
    resolution = AnchorResolution(
        section_key=target_hint,
        target_hint=target_hint,
        inspect_snapshot_id=template_map.inspect_snapshot_id,
        resolved_via='target_hint',
        resolved_target_id=best_anchor.body_paragraph_id,
        matched_anchor_id=best_anchor.anchor_id,
        matched_heading=best_anchor.heading_text,
        body_anchor_text=best_anchor.body_anchor_text,
        confidence=_normalize_confidence(best_score),
        matched_signals=best_signals,
        alternatives=alternatives,
    )
    return best_anchor, resolution


def resolve_placeholder(
    template_map: TemplateMap,
    placeholder: str,
    *,
    section_key: str,
    raw_input: str | None = None,
) -> PlaceholderResolution:
    matched = placeholder in template_map.placeholders
    if not matched:
        raise TemplateEngineError(f'placeholder not found in template: {placeholder!r}')
    return PlaceholderResolution(section_key=section_key, placeholder=placeholder, matched=True, raw_input=raw_input or placeholder)


def _render_bullet_line(item: str, *, level: int, bullet_style: str | None = None) -> str:
    del level, bullet_style
    return item.strip()


def _render_number_line(item: str, *, level: int, ordinal: int) -> str:
    del level, ordinal
    return item.strip()


def _render_block_lines(block: ContentBlock) -> list[str]:
    if block.type == 'paragraph' and block.text:
        return [block.text.strip()]
    if block.type == 'bullet_list':
        rendered: list[str] = []
        for index, item in enumerate(block.items):
            if not item or not item.strip():
                continue
            level = block.item_depths[index] if index < len(block.item_depths) else 1
            rendered.append(_render_bullet_line(item, level=level, bullet_style=block.bullet_style))
        return rendered
    if block.type == 'numbered_list':
        rendered: list[str] = []
        ordinal = 1
        for index, item in enumerate(block.items):
            if not item or not item.strip():
                continue
            level = block.item_depths[index] if index < len(block.item_depths) else 1
            rendered.append(_render_number_line(item, level=level, ordinal=ordinal))
            ordinal += 1
        return rendered
    raise TemplateEngineError(f'unsupported content block type: {block.type}')


def _join_rendered_lines(lines: list[str]) -> str:
    normalized = [line.rstrip() for line in lines if line is not None and line.strip()]
    return '\r\n'.join(normalized)


def _is_safe_exemplar_text(text: str | None) -> bool:
    if not text:
        return False
    compact = ' '.join(text.split())
    return 4 <= len(compact) <= 120 and '\n' not in text and '\r' not in text


def _paragraph_range_count(start_paragraph: TemplateParagraph, end_paragraph: TemplateParagraph | None) -> int:
    if end_paragraph is None:
        return 1
    return max(1, end_paragraph.index - start_paragraph.index + 1)


def _paragraphs_in_range(
    template_map: TemplateMap,
    start_paragraph: TemplateParagraph,
    end_paragraph: TemplateParagraph | None,
) -> list[TemplateParagraph]:
    if end_paragraph is None:
        return [start_paragraph]
    result: list[TemplateParagraph] = []
    for paragraph in template_map.paragraphs:
        if paragraph.section != start_paragraph.section:
            continue
        if start_paragraph.section_paragraph_index <= paragraph.section_paragraph_index <= end_paragraph.section_paragraph_index:
            result.append(paragraph)
    return result


def _find_nonempty_range_boundaries(
    template_map: TemplateMap,
    start_paragraph: TemplateParagraph,
    end_paragraph: TemplateParagraph | None,
) -> tuple[TemplateParagraph | None, TemplateParagraph | None]:
    paragraphs = _paragraphs_in_range(template_map, start_paragraph, end_paragraph)
    if not paragraphs:
        return None, None

    nonempty = [paragraph for paragraph in paragraphs if (paragraph.text or '').strip()]
    if not nonempty:
        return None, None
    return nonempty[0], nonempty[-1]


def _preview_text(text: str | None, *, limit: int = 160) -> str:
    compact = re.sub(r'\s+', ' ', text or '').strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + '…'


def _authored_preview_text(lines: list[str]) -> str:
    nonempty = [re.sub(r'\s+', ' ', line or '').strip() for line in lines if (line or '').strip()]
    if not nonempty:
        return ''
    return _combine_preview_lines(nonempty[0], nonempty[-1])


def _table_patch_preview(patches: list[TablePatchCell]) -> str:
    preview_items: list[str] = []
    for patch in patches[:4]:
        preview_items.append(f'{patch.cell_addr}={_preview_text(patch.value, limit=32)}')
    if len(patches) > 4:
        preview_items.append(f'+{len(patches) - 4} more')
    return '; '.join(preview_items)


def _table_row_patch_preview(row_patches: list[TableRowPatch]) -> str:
    preview_items: list[str] = []
    for row_patch in row_patches[:3]:
        cell_bits = ', '.join(
            f'{cell.column_ref}={_preview_text(cell.value, limit=24)}'
            for cell in row_patch.cells[:3]
        )
        preview_items.append(f'row {row_patch.row}: {cell_bits}')
    if len(row_patches) > 3:
        preview_items.append(f'+{len(row_patches) - 3} more rows')
    return '; '.join(preview_items)


def _table_record_patch_preview(record_patches: list[TableRecordPatch]) -> str:
    preview_items: list[str] = []
    for record_patch in record_patches[:3]:
        cell_bits = ', '.join(
            f'{cell.column_ref}={_preview_text(cell.value, limit=24)}'
            for cell in record_patch.cells[:3]
        )
        preview_items.append(f'{record_patch.record_key}: {cell_bits}')
    if len(record_patches) > 3:
        preview_items.append(f'+{len(record_patches) - 3} more records')
    return '; '.join(preview_items)


def _find_first_table_element_for_structural_range(
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
) -> ET.Element | None:
    table_paragraph_ids = list(structural_range.get('table_paragraph_ids') or [])
    for paragraph_id in table_paragraph_ids:
        section_name, paragraph_index = _parse_paragraph_id(paragraph_id)
        root = section_root_map.get(section_name)
        if root is None:
            continue
        paragraphs = root.findall('.//hp:p', NS)
        if paragraph_index < 1 or paragraph_index > len(paragraphs):
            continue
        paragraph = paragraphs[paragraph_index - 1]
        table = paragraph.find('.//hp:tbl', NS)
        if table is not None:
            return table
    return None


def _extract_table_header_map(
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
) -> dict[str, str]:
    table = _find_first_table_element_for_structural_range(section_root_map, structural_range)
    if table is None:
        return {}
    first_row = table.find('./hp:tr', NS)
    if first_row is None:
        return {}
    header_map: dict[str, str] = {}
    for col_index, cell in enumerate(first_row.findall('./hp:tc', NS), start=1):
        text = ' '.join(''.join(cell.itertext()).split()).strip()
        if not text:
            continue
        header_map[_normalize_text(text)] = _column_index_to_letters(col_index)
        header_map[_normalize_text(text.replace(' ', ''))] = _column_index_to_letters(col_index)
    return header_map


def _extract_table_rows(table: ET.Element | None) -> list[list[str]]:
    if table is None:
        return []
    rows: list[list[str]] = []
    for tr in table.findall('./hp:tr', NS):
        row_values: list[str] = []
        for tc in tr.findall('./hp:tc', NS):
            row_values.append(' '.join(''.join(tc.itertext()).split()).strip())
        rows.append(row_values)
    return rows


def _extract_first_inner_table_paragraph_text(
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
) -> str | None:
    table = _find_first_table_element_for_structural_range(section_root_map, structural_range)
    if table is None:
        return None
    for para in table.findall('.//hp:tc//hp:p', NS):
        text = ' '.join(''.join(para.itertext()).split()).strip()
        if text:
            return text
    return None


def _extract_table_fingerprint(
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
) -> dict[str, Any]:
    table = _find_first_table_element_for_structural_range(section_root_map, structural_range)
    rows = _extract_table_rows(table)
    if not rows:
        return {}
    header_row = rows[0]
    data_rows = rows[1:]
    first_col_keys = [row[0] for row in data_rows if row and row[0]]
    payload = {
        'row_count': len(rows),
        'col_count': max((len(row) for row in rows), default=0),
        'header_row': header_row,
        'first_col_keys_sample': first_col_keys[:8],
    }
    payload['fingerprint'] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]
    return payload


def _extract_table_record_key_map(
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
    *,
    record_key_column: str | None = None,
) -> dict[str, int]:
    table = _find_first_table_element_for_structural_range(section_root_map, structural_range)
    rows = _extract_table_rows(table)
    if len(rows) < 2:
        return {}
    header_row = rows[0]
    key_col_index = 0
    if record_key_column:
        header_map = { _normalize_text(text): idx for idx, text in enumerate(header_row) if text }
        compact_header_map = { _normalize_text(text.replace(' ', '')): idx for idx, text in enumerate(header_row) if text }
        normalized = _normalize_text(record_key_column)
        compact = _normalize_text(record_key_column.replace(' ', ''))
        if normalized in header_map:
            key_col_index = header_map[normalized]
        elif compact in compact_header_map:
            key_col_index = compact_header_map[compact]
        elif re.fullmatch(r'[A-Za-z]+', record_key_column.strip()):
            key_col_index = _column_letters_to_index(record_key_column.strip().upper()) - 1
        elif re.fullmatch(r'\d+', record_key_column.strip()):
            key_col_index = int(record_key_column.strip()) - 1
        else:
            raise TemplateEngineError(f'unknown table record key column reference: {record_key_column!r}')
    record_map: dict[str, int] = {}
    for row_index_1based, row in enumerate(rows[1:], start=2):
        if key_col_index >= len(row):
            continue
        key_text = row[key_col_index].strip()
        if not key_text:
            continue
        normalized = _normalize_text(key_text)
        compact = _normalize_text(key_text.replace(' ', ''))
        if normalized in record_map or compact in record_map:
            raise TemplateEngineError(f'duplicate table record key detected: {key_text!r}')
        record_map[normalized] = row_index_1based
        record_map[compact] = row_index_1based
    return record_map


def _resolve_table_column_ref(column_ref: str, header_map: dict[str, str]) -> str:
    raw = str(column_ref).strip()
    if not raw:
        raise TemplateEngineError('table column reference must not be empty')
    normalized = _normalize_text(raw)
    compact = _normalize_text(raw.replace(' ', ''))
    if normalized in header_map:
        return header_map[normalized]
    if compact in header_map:
        return header_map[compact]
    if re.fullmatch(r'[A-Za-z]+', raw):
        return raw.upper()
    if re.fullmatch(r'\d+', raw):
        return _column_index_to_letters(int(raw))
    raise TemplateEngineError(f'unknown table column/header reference: {raw!r}')


def _resolve_effective_table_patches(
    *,
    direct_patches: list[TablePatchCell],
    row_patches: list[TableRowPatch],
    record_patches: list[TableRecordPatch],
    section_root_map: dict[str, ET.Element],
    structural_range: dict[str, Any],
    record_key_column: str | None = None,
) -> list[TablePatchCell]:
    resolved: list[TablePatchCell] = [TablePatchCell(cell_addr=patch.cell_addr, value=patch.value) for patch in direct_patches]
    if not row_patches and not record_patches:
        return resolved
    header_map = _extract_table_header_map(section_root_map, structural_range)
    if not header_map:
        raise TemplateEngineError('table patch shorthand requires a detectable header row in the target table')
    for row_patch in row_patches:
        for cell in row_patch.cells:
            col_letters = _resolve_table_column_ref(cell.column_ref, header_map)
            resolved.append(
                TablePatchCell(
                    cell_addr=f'{col_letters}{row_patch.row}',
                    value=cell.value,
                )
            )
    if record_patches:
        record_key_map = _extract_table_record_key_map(
            section_root_map,
            structural_range,
            record_key_column=record_key_column,
        )
        if not record_key_map:
            raise TemplateEngineError('table_record patch requires detectable data rows and record keys in the target table')
        for record_patch in record_patches:
            normalized = _normalize_text(record_patch.record_key)
            compact = _normalize_text(record_patch.record_key.replace(' ', ''))
            row_index = record_key_map.get(normalized) or record_key_map.get(compact)
            if row_index is None:
                raise TemplateEngineError(f'table_record key not found in target table: {record_patch.record_key!r}')
            for cell in record_patch.cells:
                col_letters = _resolve_table_column_ref(cell.column_ref, header_map)
                resolved.append(
                    TablePatchCell(
                        cell_addr=f'{col_letters}{row_index}',
                        value=cell.value,
                    )
                )
    return resolved


def _build_apply_scope_preview(
    *,
    section: ContentSection,
    edit_shape: str,
    exact_target_locked: bool,
    target_kind: str,
    resolved_target_type: str,
    resolved_target_id: str | None,
    resolved_via: str | None,
    before_preview_text: str,
    after_preview_text: str,
    heading_paragraph: TemplateParagraph | None = None,
    body_start_paragraph: TemplateParagraph | None = None,
    body_end_paragraph: TemplateParagraph | None = None,
    heading_before_preview_text: str = '',
    body_before_preview_text: str = '',
    apply_preview_text: str = '',
    warning_state: str = 'clear',
    blocking_warning_codes: list[str] | None = None,
    why_not_body_safe: str = '',
) -> ApplyScopePreview:
    effective_body_end = body_end_paragraph if body_end_paragraph else body_start_paragraph
    effective_body_before = body_before_preview_text or before_preview_text
    effective_apply_preview = apply_preview_text or after_preview_text
    return ApplyScopePreview(
        section_key=section.section_key,
        target_hint=section.target_hint,
        edit_shape=edit_shape,
        exact_target_locked=exact_target_locked,
        resolved_target_id=resolved_target_id,
        resolved_via=resolved_via,
        resolved_target_type=resolved_target_type,
        target_kind=target_kind,
        heading_paragraph_id=heading_paragraph.paragraph_id if heading_paragraph else None,
        heading_before_preview_text=_preview_text(heading_before_preview_text),
        body_start_paragraph_id=body_start_paragraph.paragraph_id if body_start_paragraph else None,
        body_end_paragraph_id=effective_body_end.paragraph_id if effective_body_end else None,
        body_before_preview_text=_preview_text(effective_body_before),
        apply_preview_text=_preview_text(effective_apply_preview),
        warning_state=warning_state,
        blocking_warning_codes=list(blocking_warning_codes or []),
        why_not_body_safe=why_not_body_safe,
        start_paragraph_id=body_start_paragraph.paragraph_id if body_start_paragraph else None,
        end_paragraph_id=effective_body_end.paragraph_id if effective_body_end else None,
        paragraph_count=_paragraph_range_count(body_start_paragraph, effective_body_end) if body_start_paragraph else 0,
        before_preview_text=_preview_text(effective_body_before),
        after_preview_text=_preview_text(effective_apply_preview),
        render_review_required=True,
    )


def _infer_resolved_target_type(
    *,
    target_paragraph: TemplateParagraph | None,
    target_end_paragraph: TemplateParagraph | None,
    has_table_patches: bool,
    is_placeholder: bool = False,
) -> str:
    if is_placeholder:
        return 'placeholder'
    if has_table_patches:
        return 'table-cell'
    if target_paragraph is None:
        return 'body'
    if target_paragraph.paragraph_class in {'heading', 'number'}:
        return 'heading'
    if target_end_paragraph is not None and target_end_paragraph.paragraph_id != target_paragraph.paragraph_id:
        return 'body-range'
    return 'body'


def _build_warning_badge(
    *,
    code: str,
    summary: str,
    detail: str = '',
    severity: str = 'warning',
    stage: str = 'compile',
    blocking: bool = False,
    section_key: str | None = None,
    resolved_target_id: str | None = None,
) -> WarningBadge:
    return WarningBadge(
        code=code,
        severity=severity,
        stage=stage,
        blocking=blocking,
        section_key=section_key,
        resolved_target_id=resolved_target_id,
        summary=summary,
        detail=detail,
    )


def _extract_expected_table_fingerprint_token(value: Any) -> str | None:
    fingerprint = value.get('fingerprint') if isinstance(value, dict) else value
    if fingerprint is None:
        return None
    normalized = str(fingerprint).strip()
    return normalized or None


def _build_shared_table_target_key(*, table_fingerprint: Any, entry_cell_addr: str | None) -> str | None:
    """Canonical physical shared-table target key used after structural normalization.

    Format: ``<table_fingerprint>@<normalized_entry_cell_addr>``.

    This is explicitly not a ``resolved_target_id`` and not a per-op identifier. We use it
    only to express ownership of one physical table cell after paragraph-range resolution has
    collapsed onto the actual normalized table fingerprint + entry cell.
    """
    fingerprint_token = _extract_expected_table_fingerprint_token(table_fingerprint)
    if not fingerprint_token or not entry_cell_addr:
        return None
    try:
        normalized_entry_cell_addr = normalize_table_cell_addr(entry_cell_addr)
    except TemplateEngineError:
        return None
    return f'{fingerprint_token}@{normalized_entry_cell_addr}'


def _build_shared_table_bundle_id(
    *,
    section_key: str | None,
    candidate_identity_key: str | None = None,
    action_identity_key: str | None = None,
    probe_identity_key: str | None = None,
    resolved_target_id: str | None = None,
    fallback: str,
) -> str:
    """Build the section/op-bundle identifier used for readiness collision counting.

    One bundle represents one section claim and may legally contain its own probe+action
    pair. Readiness collisions are judged on bundle count per canonical shared-table key,
    not on raw op count.
    """
    section_token = str(section_key or '').strip() or '_'
    identity_token = (
        str(candidate_identity_key or '').strip()
        or str(action_identity_key or '').strip()
        or str(probe_identity_key or '').strip()
        or str(resolved_target_id or '').strip()
        or fallback
    )
    return f'{section_token}::{identity_token}'


def _stable_shared_table_bundle_signature(operations: list[dict[str, Any]]) -> str:
    comparable_ops: list[dict[str, Any]] = []
    for operation in operations:
        comparable = dict(operation)
        for key in (
            'section_key',
            'resolved_target_id',
            'candidate_identity_key',
            'probe_identity_key',
            'action_identity_key',
            'shared_table_target_key',
        ):
            comparable.pop(key, None)
        comparable_ops.append(comparable)
    return _stable_structural_candidate_digest({'operations': comparable_ops})


def _summarize_shared_table_bundle_operations(operations: list[dict[str, Any]]) -> str:
    previews: list[str] = []
    for operation in operations:
        op_type = str(operation.get('op'))
        if op_type == 'table_cell_replace_text':
            replace_text = operation.get('replace')
            if replace_text is not None:
                previews.append(str(replace_text))
        elif op_type == 'table_patch_cells':
            for patch in operation.get('patches') or []:
                if not isinstance(patch, dict):
                    continue
                cell_addr = patch.get('cell_addr')
                replace_text = patch.get('replace')
                if cell_addr is None and replace_text is None:
                    continue
                previews.append(f'{cell_addr}={replace_text}')
    compact = ' | '.join(item for item in previews if item)
    return compact[:240]


def _scan_emitted_shared_table_identities(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Scan emitted ops for canonical shared-table claims.

    `_build_confirm_policy()` uses this to fail closed if more than one section/op bundle
    claims the same canonical physical key. The normal probe+action pair inside one section
    is allowed because both ops collapse into one bundle id under that key.
    """
    groups_by_key: dict[str, dict[str, Any]] = {}
    for index, operation in enumerate(operations, start=1):
        op_type = str(operation.get('op'))
        shared_table_target_key = str(operation.get('shared_table_target_key') or '').strip() or None
        if not shared_table_target_key and op_type == 'cursor_snapshot':
            shared_table_target_key = _build_shared_table_target_key(
                table_fingerprint=operation.get('expected_table_fingerprint'),
                entry_cell_addr=operation.get('expected_cell_addr'),
            )
        elif not shared_table_target_key and op_type == 'table_patch_cells':
            shared_table_target_key = _build_shared_table_target_key(
                table_fingerprint=operation.get('expected_table_fingerprint'),
                entry_cell_addr=operation.get('expected_entry_cell_addr'),
            )
        if not shared_table_target_key:
            continue

        group = groups_by_key.setdefault(
            shared_table_target_key,
            {
                'shared_table_target_key': shared_table_target_key,
                'claims': {},
            },
        )
        bundle_id = _build_shared_table_bundle_id(
            section_key=operation.get('section_key'),
            candidate_identity_key=operation.get('candidate_identity_key'),
            action_identity_key=operation.get('action_identity_key'),
            probe_identity_key=operation.get('probe_identity_key'),
            resolved_target_id=operation.get('resolved_target_id'),
            fallback=f'op#{index}',
        )
        claim = group['claims'].setdefault(
            bundle_id,
            {
                'bundle_id': bundle_id,
                'section_key': str(operation.get('section_key') or '').strip() or None,
                'resolved_target_id': str(operation.get('resolved_target_id') or '').strip() or None,
                'candidate_identity_key': str(operation.get('candidate_identity_key') or '').strip() or None,
                'op_types': [],
            },
        )
        if op_type not in claim['op_types']:
            claim['op_types'].append(op_type)

    emitted_shared_table_keys = sorted(groups_by_key)
    collision_groups: list[dict[str, Any]] = []
    for shared_table_target_key in emitted_shared_table_keys:
        group = groups_by_key[shared_table_target_key]
        claims = list(group['claims'].values())
        if len(claims) <= 1:
            continue
        section_keys = sorted({item['section_key'] for item in claims if item.get('section_key')})
        resolved_target_ids = sorted({item['resolved_target_id'] for item in claims if item.get('resolved_target_id')})
        op_types = sorted({op_type for item in claims for op_type in item.get('op_types', [])})
        collision_groups.append(
            {
                'shared_table_target_key': shared_table_target_key,
                'section_keys': section_keys,
                'bundle_ids': [item['bundle_id'] for item in claims],
                'bundle_count': len(claims),
                'claim_count': len(claims),
                'family_count': len(op_types),
                'op_types': op_types,
                'resolved_target_ids': resolved_target_ids,
                'summary': (
                    f"canonical shared-table key {shared_table_target_key} is claimed by "
                    f"{', '.join(section_keys) if section_keys else len(claims)}"
                ),
            }
        )

    return {
        'shared_table_target_keys': emitted_shared_table_keys,
        'collision_groups': collision_groups,
    }


def _build_runtime_find_anchor(text: str, *, max_len: int = 72) -> str | None:
    compact = ' '.join(text.split())
    if not compact:
        return None
    label_prefix_match = re.match(r'^(?:[■●•·▪▫◦\-]|[①-⑳]|\(?\d+\)|\d+[.)])?\s*([^:：]{1,24}[:：])', compact)
    if label_prefix_match:
        return label_prefix_match.group(0).strip()
    earliest_table_token: tuple[int, str] | None = None
    for token in TABLEISH_LABEL_TOKENS:
        index = compact.find(token)
        if index < 0:
            continue
        if earliest_table_token is None or index < earliest_table_token[0] or (index == earliest_table_token[0] and len(token) < len(earliest_table_token[1])):
            earliest_table_token = (index, token)
    if earliest_table_token is not None and earliest_table_token[0] <= 12:
        return earliest_table_token[1]
    if len(compact) <= max_len:
        return compact
    return compact[:max_len].rstrip()


def _build_table_identity_hint(
    *,
    structural_range: dict[str, Any],
    preferred_entry_paragraph_id: str | None,
) -> dict[str, Any]:
    table_paragraph_ids = list(structural_range.get('table_paragraph_ids') or [])
    paragraph_offset_in_range = None
    if preferred_entry_paragraph_id and preferred_entry_paragraph_id in table_paragraph_ids:
        paragraph_offset_in_range = table_paragraph_ids.index(preferred_entry_paragraph_id)
    return {
        'strategy': 'cursor_snapshot_then_runtime_identity',
        'table_index_hint_within_range': 0 if table_paragraph_ids else None,
        'entry_paragraph_id': preferred_entry_paragraph_id,
        'entry_paragraph_offset_in_table_paragraphs': paragraph_offset_in_range,
        'entry_cell_addr_hint': 'A1' if preferred_entry_paragraph_id else None,
        'entry_row_index_hint': 0 if preferred_entry_paragraph_id else None,
        'entry_col_index_hint': 0 if preferred_entry_paragraph_id else None,
        'entry_row_1based_hint': 1 if preferred_entry_paragraph_id else None,
        'entry_col_1based_hint': 1 if preferred_entry_paragraph_id else None,
        'entry_col_letters_hint': 'A' if preferred_entry_paragraph_id else None,
        'notes': [
            'Compiler hints describe the runtime entry identity shape, not a guaranteed final target cell.',
            'Actual cell identity should be confirmed from runtime cursor_snapshot output before edit.',
        ],
    }


def _stable_structural_candidate_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]


def _build_structural_candidate_identity(
    *,
    section_key: str,
    resolved_target_id: str,
    preferred_entry_paragraph_id: str | None,
    preferred_entry_find_text: str | None,
    table_fingerprint: dict[str, Any] | None,
    replacement_text: str | None = None,
    table_patches: list[TablePatchCell] | None = None,
) -> dict[str, Any]:
    probe_basis = {
        'section_key': section_key,
        'resolved_target_id': resolved_target_id,
        'preferred_entry_paragraph_id': preferred_entry_paragraph_id,
        'preferred_entry_find_text': preferred_entry_find_text,
        'table_fingerprint': table_fingerprint.get('fingerprint') if isinstance(table_fingerprint, dict) else None,
    }
    action_basis = {
        'section_key': section_key,
        'replacement_text': replacement_text,
        'table_patches': [
            {
                'cell_addr': normalize_table_cell_addr(patch.cell_addr),
                'value': patch.value,
            }
            for patch in (table_patches or [])
        ],
    }
    probe_identity_key = _stable_structural_candidate_digest({'kind': 'probe', **probe_basis})
    action_identity_key = _stable_structural_candidate_digest({'kind': 'action', **action_basis})
    return {
        'strategy': 'section_unique_probe_action_identity',
        'probe_basis': probe_basis,
        'action_basis': action_basis,
        'probe_identity_key': probe_identity_key,
        'action_identity_key': action_identity_key,
        'candidate_identity_key': _stable_structural_candidate_digest(
            {
                'kind': 'candidate',
                'probe_identity_key': probe_identity_key,
                'action_identity_key': action_identity_key,
            }
        ),
    }


def _build_search_first_proof_tokens(value: str | None, *, limit: int = 3) -> list[str]:
    """Build short proof tokens for entry and post-write checks.

    Purpose: carry a compact text witness into runtime so the worker can confirm it entered the
    intended carrier and later show changed content with nearby context.
    Risk prevented: silently editing the wrong paragraph/cell when structural search hits a lookalike.
    Next step intent: reuse these tokens in cursor entry and post-write proof hooks.
    """
    if limit <= 0:
        return []

    seen: set[str] = set()
    tokens: list[str] = []
    for raw_line in str(value or '').splitlines():
        compact = ' '.join(raw_line.split()).strip()
        if len(compact) < 4:
            continue
        token = compact[:96].rstrip()
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            return tokens

    compact_value = ' '.join(str(value or '').split()).strip()
    if compact_value:
        fallback = compact_value[:96].rstrip()
        if fallback and fallback not in seen:
            tokens.append(fallback)
    return tokens[:limit]


def _annotate_search_first_runtime_step(
    operation: dict[str, Any],
    *,
    step_id: str,
    step_role: str,
    purpose: str,
    risk_prevented: str,
    next_step_intent: str,
    verification_mode: str | None = None,
    needs_visual_verification: bool | None = None,
) -> dict[str, Any]:
    """Keep interactive runtime behavior explicit instead of hiding it inside generic ops.

    Purpose: tag each emitted runtime step with one behavioral owner role.
    Risk prevented: later tooling treating entry, verification, apply, and evidence capture as one opaque action.
    Next step intent: make edit summaries and GUI scaffolding reflect the intended search-first flow.
    """
    patched = dict(operation)
    metadata = dict(patched.get('metadata') or {})
    metadata.update(
        {
            'step_id': step_id,
            'step_role': step_role,
            'step_purpose': purpose,
            'risk_prevented': risk_prevented,
            'next_step_intent': next_step_intent,
        }
    )
    patched['metadata'] = metadata
    if verification_mode and not patched.get('verification_mode'):
        patched['verification_mode'] = verification_mode
    if needs_visual_verification is not None and 'needs_visual_verification' not in patched:
        patched['needs_visual_verification'] = needs_visual_verification
    return patched


def _apply_search_first_runtime_defaults(
    *,
    edit_fixture: dict[str, Any] | None,
    preferred_entry_source_text: str | None,
    replacement_text: str | None,
) -> dict[str, Any] | None:
    """Default interactive flow for writable structural targets.

    Purpose: separate runtime entry, pre-action verification, action apply, and post-action evidence
    as distinct helper-owned steps.
    Risk prevented: jumping from search to write without a cursor lock, proof text, or GUI verification hook.
    Next step intent: let the agent inspect candidate choice, enter the live target, verify, apply, and return context.
    """
    if not isinstance(edit_fixture, dict):
        return edit_fixture

    operations = edit_fixture.get('operations')
    if not isinstance(operations, list) or not operations:
        return edit_fixture

    entry_tokens = _build_search_first_proof_tokens(preferred_entry_source_text, limit=2)
    post_write_tokens = _build_search_first_proof_tokens(replacement_text, limit=3)

    annotated_operations: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            annotated_operations.append(operation)
            continue

        op_type = str(operation.get('op') or '')
        patched = dict(operation)
        if op_type == 'cursor_snapshot':
            patched = _annotate_search_first_runtime_step(
                patched,
                step_id='runtime-entry-lock',
                step_role='runtime_cursor_entry',
                purpose='Enter the agent-selected carrier and lock the live cursor to that identity before editing.',
                risk_prevented='Editing a structurally similar but wrong table/paragraph after search resolution.',
                next_step_intent='Hand the locked cursor snapshot to pre-action verification and then the write step.',
                verification_mode='image',
                needs_visual_verification=True,
            )
            if entry_tokens and not patched.get('expected_selected_text_present_any'):
                patched['capture_selected_text'] = True
                patched['expected_selected_text_present_any'] = entry_tokens
        elif op_type == 'table_cell_action':
            patched = _annotate_search_first_runtime_step(
                patched,
                step_id=f"pre-action-verify-{str(patched.get('action') or 'selection')}",
                step_role='pre_action_verification',
                purpose='Confirm the live selection shape before the agent commits a write.',
                risk_prevented='Applying cell edits while the GUI selection is on the wrong scope or carrier.',
                next_step_intent='Proceed to the write only after the live selection looks correct.',
                verification_mode='image',
                needs_visual_verification=True,
            )
        elif op_type == 'table_cell_replace_text':
            patched = _annotate_search_first_runtime_step(
                patched,
                step_id='apply-write',
                step_role='action_apply',
                purpose='Apply the confirmed write against the locked runtime cell.',
                risk_prevented='Blindly writing without returning proof of the changed content.',
                next_step_intent='Capture post-write text context and optional GUI evidence for review.',
                verification_mode='text-broader',
                needs_visual_verification=True,
            )
            if post_write_tokens and not patched.get('expected_selected_text_present_any_after'):
                patched['capture_selected_text_after'] = True
                patched['expected_selected_text_present_any_after'] = post_write_tokens
        elif op_type == 'table_patch_cells':
            patched = _annotate_search_first_runtime_step(
                patched,
                step_id='apply-table-patch-batch',
                step_role='action_apply',
                purpose='Apply a confirmed batch of cell writes after runtime entry verification.',
                risk_prevented='Large structural table edits running without an explicit GUI verification hook.',
                next_step_intent='Return patch-level results and follow with GUI review when needed.',
                verification_mode='image',
                needs_visual_verification=True,
            )
        annotated_operations.append(patched)

    metadata = dict(edit_fixture.get('metadata') or {})
    metadata['search_first_default_flow'] = {
        'candidate_search': 'surface_all_candidates',
        'candidate_selection': 'agent_choose_most_likely_then_normalize_to_writable_scope',
        'runtime_cursor_entry': 'cursor_snapshot_identity_lock',
        'pre_action_verification': 'gui_or_selection_shape_check_before_write',
        'action_apply': 'write_only_after_runtime_lock',
        'post_action_context_capture': 'return_changed_content_with_selected_text_context',
        'gui_verification': 'optional_before_and_after_when_visual_confirmation_matters',
    }
    return {
        **edit_fixture,
        'operations': annotated_operations,
        'metadata': metadata,
    }


def _build_search_first_candidate_review(recommendation: SectionTargetRecommendation) -> dict[str, Any]:
    """Surface full candidate info while still exposing the compiler's best pick.

    Purpose: keep search output inspectable instead of collapsing it into one hidden recommendation.
    Risk prevented: agents losing the alternative candidates that explain why a target was chosen.
    Next step intent: feed the chosen candidate into runtime entry and optional GUI verification.
    """

    def build_target_variants(candidate: TargetCandidate) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        variants: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_variant(*, variant_kind: str, resolved_target_id: str | None, target_kind: str | None, label: str, editable: bool) -> None:
            normalized_target_id = str(resolved_target_id or '').strip()
            if not normalized_target_id or normalized_target_id in seen:
                return
            seen.add(normalized_target_id)
            variants.append(
                {
                    'variant_kind': variant_kind,
                    'resolved_target_id': normalized_target_id,
                    'target_kind': target_kind,
                    'label': label,
                    'editable_preferred': editable,
                    'heading_paragraph_id': candidate.heading_paragraph_id,
                    'body_start_paragraph_id': candidate.body_start_paragraph_id,
                    'body_end_paragraph_id': candidate.body_end_paragraph_id,
                    'paragraph_range': _format_paragraph_range(candidate.body_start_paragraph_id, candidate.body_end_paragraph_id),
                }
            )

        add_variant(
            variant_kind='candidate_target',
            resolved_target_id=candidate.resolved_target_id,
            target_kind=candidate.target_kind,
            label='candidate target',
            editable=candidate.target_kind not in {'heading', 'number'},
        )
        add_variant(
            variant_kind='recommended_target',
            resolved_target_id=candidate.recommended_resolved_target_id,
            target_kind='paragraph_range' if candidate.body_start_paragraph_id != candidate.body_end_paragraph_id else 'body_paragraph',
            label='recommended writable scope',
            editable=True,
        )
        add_variant(
            variant_kind='body_scope',
            resolved_target_id=_format_paragraph_range(candidate.body_start_paragraph_id, candidate.body_end_paragraph_id),
            target_kind='paragraph_range' if candidate.body_start_paragraph_id != candidate.body_end_paragraph_id else 'body_paragraph',
            label='body scope variant',
            editable=True,
        )
        add_variant(
            variant_kind='heading_anchor',
            resolved_target_id=candidate.heading_paragraph_id,
            target_kind='heading',
            label='heading anchor',
            editable=False,
        )

        preferred = next((item for item in variants if item.get('editable_preferred')), None)
        if preferred is None and variants:
            preferred = variants[0]
        return variants, preferred

    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(recommendation.candidates, start=1):
        target_variants, preferred_target_variant = build_target_variants(candidate)
        candidates.append(
            {
                'rank': index,
                'resolved_target_id': candidate.resolved_target_id,
                'recommended_resolved_target_id': candidate.recommended_resolved_target_id,
                'target_kind': candidate.target_kind,
                'heading_text': candidate.heading_text,
                'heading_paragraph_id': candidate.heading_paragraph_id,
                'body_start_paragraph_id': candidate.body_start_paragraph_id,
                'body_end_paragraph_id': candidate.body_end_paragraph_id,
                'preview_text': candidate.preview_text,
                'selection_preview': candidate.selection_preview,
                'context_before': candidate.context_before,
                'context_after': candidate.context_after,
                'confidence': candidate.confidence,
                'confidence_label': candidate.confidence_label,
                'matched_signals': list(candidate.matched_signals or []),
                'why_recommended': candidate.why_recommended,
                'target_variants': target_variants,
                'preferred_target_variant': preferred_target_variant,
            }
        )

    selected = candidates[0] if candidates else None
    return {
        'section_key': recommendation.section_key,
        'target_hint': recommendation.target_hint,
        'selection_mode': recommendation.selection_mode,
        'selection_instruction': recommendation.selection_instruction,
        'is_ambiguous': recommendation.is_ambiguous,
        'candidate_count': recommendation.candidate_count,
        'showing_candidate_count': recommendation.showing_candidate_count,
        'candidate_state': recommendation.candidate_state,
        'candidate_state_label': recommendation.candidate_state_label,
        'candidate_state_reason': recommendation.candidate_state_reason,
        'agent_selected_candidate': None if selected is None else {
            'resolved_target_id': selected['resolved_target_id'],
            'recommended_resolved_target_id': selected['recommended_resolved_target_id'],
            'preferred_target_variant': selected.get('preferred_target_variant'),
            'selection_basis': recommendation.top_choice_summary or selected['why_recommended'] or recommendation.recommended_action,
            'confidence': selected['confidence'],
            'confidence_label': selected['confidence_label'],
        },
        'candidates': candidates,
    }


def _build_search_first_candidate_reviews(target_recommendations: list[SectionTargetRecommendation]) -> list[dict[str, Any]]:
    return [_build_search_first_candidate_review(recommendation) for recommendation in target_recommendations]


def _build_search_first_runtime_flow(
    *,
    candidate_reviews: list[dict[str, Any]],
    native_runtime_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not candidate_reviews and not native_runtime_candidates:
        return {}

    preferred_runtime_candidate = native_runtime_candidates[0] if native_runtime_candidates else {}
    return {
        'enabled': True,
        'candidate_search': {
            'surface_all_candidate_infos': bool(candidate_reviews),
            'section_count': len(candidate_reviews),
        },
        'candidate_selection': {
            'selection_owner': 'agent',
            'default_policy': 'choose_most_likely_candidate_then_normalize_to_writable_body_or_cell_scope',
            'selected_candidates': [
                review.get('agent_selected_candidate')
                for review in candidate_reviews
                if isinstance(review.get('agent_selected_candidate'), dict)
            ],
        },
        'runtime_cursor_entry': {
            'candidate_count': len(native_runtime_candidates),
            'preferred_entry_paragraph_id': preferred_runtime_candidate.get('preferred_entry_paragraph_id'),
            'preferred_entry_find_text': preferred_runtime_candidate.get('preferred_entry_find_text'),
            'expected_entry_cell_addr': preferred_runtime_candidate.get('table_identity', {}).get('entry_cell_addr_hint')
            if isinstance(preferred_runtime_candidate.get('table_identity'), dict)
            else None,
            'identity_strategy': preferred_runtime_candidate.get('reason') or preferred_runtime_candidate.get('identity_strategy'),
        },
        'pre_action_verification': {
            'uses_runtime_cursor_lock': bool(native_runtime_candidates),
            'gui_screenshot_optional': True,
        },
        'action_apply': {
            'requires_verified_runtime_entry': bool(native_runtime_candidates),
        },
        'post_action_context_capture': {
            'return_changed_content_with_context': True,
            'prefer_selected_text_context': True,
        },
        'gui_verification': {
            'before_action_optional': True,
            'after_action_optional': True,
        },
    }


def _build_native_runtime_candidates_for_structural_range(
    *,
    template_map: TemplateMap,
    section_root_map: dict[str, ET.Element],
    section_key: str,
    resolved_target_id: str,
    structural_range: dict[str, Any],
    replacement_text: str | None = None,
    table_patches: list[TablePatchCell] | None = None,
    table_entry_find: str | None = None,
    table_entry_cell_addr: str | None = None,
    table_entry_cursor_pos: list[int] | None = None,
    table_record_key_column: str | None = None,
) -> list[dict[str, Any]]:
    if int(structural_range.get('table_count', 0) or 0) <= 0:
        return []
    table_paragraph_ids = list(structural_range.get('table_paragraph_ids') or [])
    preferred_entry_paragraph = None
    preferred_entry_paragraph_id = None
    for paragraph_id in table_paragraph_ids:
        paragraph = _find_paragraph(template_map, paragraph_id)
        if paragraph is None:
            continue
        if preferred_entry_paragraph is None:
            preferred_entry_paragraph = paragraph
            preferred_entry_paragraph_id = paragraph_id
        if paragraph.text and paragraph.text.strip():
            preferred_entry_paragraph = paragraph
            preferred_entry_paragraph_id = paragraph_id
            break
    table_fingerprint = _extract_table_fingerprint(section_root_map, structural_range)
    inner_paragraph_entry_text = _extract_first_inner_table_paragraph_text(section_root_map, structural_range)
    fingerprint_header_row = list(table_fingerprint.get('header_row') or []) if table_fingerprint else []
    fingerprint_entry_text = None
    if len(fingerprint_header_row) == 1 and isinstance(fingerprint_header_row[0], str) and fingerprint_header_row[0].strip():
        fingerprint_entry_text = fingerprint_header_row[0].strip()
    paragraph_entry_text = preferred_entry_paragraph.text if preferred_entry_paragraph and preferred_entry_paragraph.text else None
    prefer_paragraph_anchor = bool(
        (inner_paragraph_entry_text or paragraph_entry_text)
        and int(table_fingerprint.get('row_count', 0) or 0) == 1
        and int(table_fingerprint.get('col_count', 0) or 0) == 1
    )
    preferred_entry_source_text = (
        (inner_paragraph_entry_text or paragraph_entry_text)
        if prefer_paragraph_anchor
        else (fingerprint_entry_text or inner_paragraph_entry_text or paragraph_entry_text)
    )
    runtime_entry_find_anchor = _build_runtime_find_anchor(preferred_entry_source_text or '')
    preferred_entry_find_text = table_entry_find or runtime_entry_find_anchor or preferred_entry_source_text
    preferred_entry_preview_text = None
    if preferred_entry_source_text:
        compact = ' '.join(preferred_entry_source_text.split())
        preferred_entry_preview_text = compact[:180] + ('...' if len(compact) > 180 else '')

    table_identity = _build_table_identity_hint(
        structural_range=structural_range,
        preferred_entry_paragraph_id=preferred_entry_paragraph_id,
    )
    candidate_identity = _build_structural_candidate_identity(
        section_key=section_key,
        resolved_target_id=resolved_target_id,
        preferred_entry_paragraph_id=preferred_entry_paragraph_id,
        preferred_entry_find_text=preferred_entry_find_text,
        table_fingerprint=table_fingerprint,
        replacement_text=replacement_text,
        table_patches=table_patches,
    )

    expected_selection_mode_by_action = {
        'block': 3,
        'block_row': 19,
        'block_col': 19,
    }

    normalized_entry_cell_addr = None
    if preferred_entry_find_text or table_entry_cursor_pos is not None:
        normalized_entry_cell_addr = normalize_table_cell_addr(table_entry_cell_addr or table_identity.get('entry_cell_addr_hint') or 'A1')
    # Derive canonical shared-table ownership only after structural normalization has
    # produced the actual fingerprint + entry-cell pair for this runtime candidate.
    shared_table_target_key = _build_shared_table_target_key(
        table_fingerprint=table_fingerprint,
        entry_cell_addr=normalized_entry_cell_addr,
    )

    candidate_ops: list[dict[str, Any]] = []
    verification_plan: list[dict[str, Any]] = []
    if table_entry_cursor_pos is not None:
        probe_op = {
            'op': 'cursor_snapshot',
            'cursor_pos': list(table_entry_cursor_pos),
            'expected_cell_addr': normalize_table_cell_addr(table_entry_cell_addr or table_identity.get('entry_cell_addr_hint') or 'A1'),
            'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
            'probe_identity_key': candidate_identity.get('probe_identity_key'),
            'action_identity_key': candidate_identity.get('action_identity_key'),
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
            'shared_table_target_key': shared_table_target_key,
        }
        if table_fingerprint:
            probe_op['expected_table_fingerprint'] = table_fingerprint
        candidate_ops.append(probe_op)
        verification_plan.append(
            {
                'stage': 'probe_before_action',
                'expected': {
                    'is_cell': True,
                },
                'op': probe_op,
            }
        )
        for action in ('block', 'block_row', 'block_col'):
            op = {
                'op': 'table_cell_action',
                'action': action,
                'cursor_pos': list(table_entry_cursor_pos),
                'expected_cell_addr': normalize_table_cell_addr(table_entry_cell_addr or table_identity.get('entry_cell_addr_hint') or 'A1'),
                'expected_selection_mode': expected_selection_mode_by_action[action],
                'expected_is_cell': True,
                'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
                'probe_identity_key': candidate_identity.get('probe_identity_key'),
                'action_identity_key': candidate_identity.get('action_identity_key'),
                'section_key': section_key,
                'resolved_target_id': resolved_target_id,
            }
            candidate_ops.append(op)
            verification_plan.append(
                {
                    'stage': f'verify_after_{action}',
                    'expected': {
                        'selection_mode': expected_selection_mode_by_action[action],
                        'is_cell': True,
                    },
                    'op': op,
                }
            )
    elif preferred_entry_find_text:
        probe_op = {
            'op': 'cursor_snapshot',
            'find': preferred_entry_find_text,
            'apply': 'first',
            'match_case': True,
            'whole_word': False,
            'select_paragraph': True,
            'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
            'probe_identity_key': candidate_identity.get('probe_identity_key'),
            'action_identity_key': candidate_identity.get('action_identity_key'),
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
        }
        candidate_ops.append(probe_op)
        verification_plan.append(
            {
                'stage': 'probe_before_action',
                'expected': {
                    'is_cell': True,
                },
                'op': probe_op,
            }
        )
        for action in ('block', 'block_row', 'block_col'):
            op = {
                'op': 'table_cell_action',
                'action': action,
                'find': preferred_entry_find_text,
                'apply': 'first',
                'match_case': True,
                'whole_word': False,
                'select_paragraph': True,
                'expected_selection_mode': expected_selection_mode_by_action[action],
                'expected_is_cell': True,
                'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
                'probe_identity_key': candidate_identity.get('probe_identity_key'),
                'action_identity_key': candidate_identity.get('action_identity_key'),
                'section_key': section_key,
                'resolved_target_id': resolved_target_id,
            }
            candidate_ops.append(op)
            verification_plan.append(
                {
                    'stage': f'verify_after_{action}',
                    'expected': {
                        'selection_mode': expected_selection_mode_by_action[action],
                        'is_cell': True,
                    },
                    'op': op,
                }
            )
    else:
        candidate_ops = [
            {'op': 'cursor_snapshot'},
            {'op': 'table_cell_action', 'action': 'block', 'expected_selection_mode': 3, 'expected_is_cell': True},
            {'op': 'table_cell_action', 'action': 'block_row', 'expected_selection_mode': 19, 'expected_is_cell': True},
            {'op': 'table_cell_action', 'action': 'block_col', 'expected_selection_mode': 19, 'expected_is_cell': True},
        ]
        verification_plan = [
            {'stage': 'probe_before_action', 'expected': {'is_cell': True}, 'op': {'op': 'cursor_snapshot'}},
            {'stage': 'verify_after_block', 'expected': {'selection_mode': 3, 'is_cell': True}, 'op': {'op': 'table_cell_action', 'action': 'block', 'expected_selection_mode': 3}},
            {'stage': 'verify_after_block_row', 'expected': {'selection_mode': 19, 'is_cell': True}, 'op': {'op': 'table_cell_action', 'action': 'block_row', 'expected_selection_mode': 19}},
            {'stage': 'verify_after_block_col', 'expected': {'selection_mode': 19, 'is_cell': True}, 'op': {'op': 'table_cell_action', 'action': 'block_col', 'expected_selection_mode': 19}},
        ]

    single_patch_replace_text = None
    if replacement_text is not None:
        single_patch_replace_text = replacement_text
    elif table_patches and len(table_patches) == 1 and normalized_entry_cell_addr is not None:
        only_patch = table_patches[0]
        if normalize_table_cell_addr(only_patch.cell_addr) == normalized_entry_cell_addr:
            single_patch_replace_text = only_patch.value

    edit_fixture = None
    if single_patch_replace_text is not None and (preferred_entry_find_text or table_entry_cursor_pos is not None):
        cursor_snapshot_op: dict[str, Any] = {
            'op': 'cursor_snapshot',
            'apply': 'first',
            'match_case': True,
            'whole_word': False,
            'select_paragraph': True,
            'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
            'probe_identity_key': candidate_identity.get('probe_identity_key'),
            'action_identity_key': candidate_identity.get('action_identity_key'),
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
        }
        if table_entry_cursor_pos is not None:
            cursor_snapshot_op['cursor_pos'] = list(table_entry_cursor_pos)
            cursor_snapshot_op['expected_cell_addr'] = normalized_entry_cell_addr
            if table_fingerprint:
                cursor_snapshot_op['expected_table_fingerprint'] = table_fingerprint
        else:
            cursor_snapshot_op['find'] = preferred_entry_find_text
            cursor_snapshot_op['expected_cell_addr'] = normalized_entry_cell_addr
            if table_fingerprint:
                cursor_snapshot_op['expected_table_fingerprint'] = table_fingerprint
        if shared_table_target_key:
            cursor_snapshot_op['shared_table_target_key'] = shared_table_target_key
        edit_fixture = {
            'operations': [
                cursor_snapshot_op,
                {
                    'op': 'table_cell_replace_text',
                    'find': preferred_entry_find_text,
                    'cursor_pos_from_snapshot': 'last',
                    'expected_cell_addr_from_snapshot': 'last',
                    'expected_selected_text': preferred_entry_source_text,
                    'replace': single_patch_replace_text,
                    'apply': 'first',
                    'match_case': True,
                    'whole_word': False,
                    'select_paragraph': False,
                    'expected_selection_mode': 3,
                    'expected_is_cell': True,
                    'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
                    'probe_identity_key': candidate_identity.get('probe_identity_key'),
                    'action_identity_key': candidate_identity.get('action_identity_key'),
                    'section_key': section_key,
                    'resolved_target_id': resolved_target_id,
                    'shared_table_target_key': shared_table_target_key,
                }
            ],
            'validation': {},
            'metadata': {
                'section_key': section_key,
                'resolved_target_id': resolved_target_id,
                'reason': 'table_structure_detected',
                'edit_shape': 'table_cell_replace_text',
                'identity_strategy': 'section_unique_probe_action_identity',
                'table_identity': table_identity,
                'table_fingerprint': table_fingerprint,
                'preferred_entry_paragraph_id': preferred_entry_paragraph_id,
                'preferred_entry_find_text': preferred_entry_find_text,
                'preferred_entry_source_text': preferred_entry_source_text,
                'candidate_identity': candidate_identity,
                'shared_table_target_key': shared_table_target_key,
            },
        }
        edit_fixture = _apply_search_first_runtime_defaults(
            edit_fixture=edit_fixture,
            preferred_entry_source_text=preferred_entry_source_text,
            replacement_text=single_patch_replace_text,
        )

    table_patch_fixture = None
    if table_patches and (preferred_entry_find_text or table_entry_cursor_pos is not None):
        table_patch_op: dict[str, Any] = {
            'op': 'table_patch_cells',
            'expected_entry_cell_addr': normalized_entry_cell_addr,
            'expected_table_fingerprint': table_fingerprint.get('fingerprint') if table_fingerprint else None,
            'match_case': True,
            'whole_word': False,
            'expected_selection_mode': 3,
            'expected_is_cell': True,
            'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
            'probe_identity_key': candidate_identity.get('probe_identity_key'),
            'action_identity_key': candidate_identity.get('action_identity_key'),
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
            'shared_table_target_key': shared_table_target_key,
            'patches': [
                {
                    'cell_addr': patch.cell_addr,
                    'replace': patch.value,
                }
                for patch in table_patches
            ],
        }
        if table_entry_cursor_pos is not None:
            table_patch_op['cursor_pos'] = list(table_entry_cursor_pos)
        else:
            table_patch_op['entry_find'] = preferred_entry_find_text
        table_patch_fixture = {
            'operations': [table_patch_op],
            'validation': {},
            'metadata': {
                'section_key': section_key,
                'resolved_target_id': resolved_target_id,
                'reason': 'table_structure_detected',
                'edit_shape': 'table_patch_cells',
                'identity_strategy': 'section_unique_probe_action_identity',
                'table_identity': table_identity,
                'table_fingerprint': table_fingerprint,
                'preferred_entry_paragraph_id': preferred_entry_paragraph_id,
                'preferred_entry_find_text': preferred_entry_find_text,
                'preferred_entry_source_text': preferred_entry_source_text,
                'expected_entry_cell_addr': normalized_entry_cell_addr,
                'entry_cursor_pos': list(table_entry_cursor_pos) if table_entry_cursor_pos is not None else None,
                'patch_count': len(table_patches),
                'record_key_column': table_record_key_column,
                'candidate_identity': candidate_identity,
                'shared_table_target_key': shared_table_target_key,
            },
        }
        table_patch_fixture = _apply_search_first_runtime_defaults(
            edit_fixture=table_patch_fixture,
            preferred_entry_source_text=preferred_entry_source_text,
            replacement_text=None,
        )

    verification_fixture = {
        'operations': candidate_ops,
        'validation': {},
        'metadata': {
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
            'reason': 'table_structure_detected',
            'verification_plan': verification_plan,
            'table_identity': table_identity,
            'table_fingerprint': table_fingerprint,
            'preferred_entry_paragraph_id': preferred_entry_paragraph_id,
            'preferred_entry_find_text': preferred_entry_find_text,
            'preferred_entry_source_text': preferred_entry_source_text,
            'candidate_identity': candidate_identity,
            'shared_table_target_key': shared_table_target_key,
        },
    }
    verification_fixture = _apply_search_first_runtime_defaults(
        edit_fixture=verification_fixture,
        preferred_entry_source_text=preferred_entry_source_text,
        replacement_text=None,
    )
    candidate_ops = list(verification_fixture.get('operations') or candidate_ops)

    return [
        {
            'section_key': section_key,
            'resolved_target_id': resolved_target_id,
            'reason': 'table_structure_detected',
            'table_paragraph_ids': table_paragraph_ids,
            'preferred_entry_paragraph_id': preferred_entry_paragraph_id,
            'preferred_entry_find_text': preferred_entry_find_text,
            'preferred_entry_source_text': preferred_entry_source_text,
            'preferred_entry_preview_text': preferred_entry_preview_text,
            'table_identity': table_identity,
            'table_fingerprint': table_fingerprint,
            'candidate_identity': candidate_identity,
            'candidate_identity_key': candidate_identity.get('candidate_identity_key'),
            'probe_identity_key': candidate_identity.get('probe_identity_key'),
            'action_identity_key': candidate_identity.get('action_identity_key'),
            'shared_table_target_key': shared_table_target_key,
            'candidate_ops': candidate_ops,
            'verification_plan': verification_plan,
            'verification_fixture': verification_fixture,
            'edit_fixture': edit_fixture,
            'table_patch_fixture': table_patch_fixture,
            'notes': [
                'Use cursor_snapshot/GetPos/get_cell_addr to confirm live table cursor location first.',
                'Address-field table jumps are now preferred for multi-cell batch edits on proven tables.',
                'preferred_entry_paragraph_id points to the first paragraph in the structural range that actually contains a table.',
                'candidate_ops are prefilled with the preferred entry paragraph text when available so runtime probing can anchor to the table paragraph directly.',
                'expected_selection_mode is based on live matrix verification on this Hancom build: block=3, block_row=19, block_col=19.',
                'verification_fixture is a ready-to-run instruction payload for runtime probe/verify execution.',
                'edit_fixture is a ready-to-run instruction payload for native-first single-cell text replacement when authored content is available.',
                'table_patch_fixture is a ready-to-run instruction payload for multi-cell batch table fills when explicit cell patches are authored.',
            ],
        }
    ]


def _make_native_list_op(*, find: str, kind: str, level: int, source_find: str | None = None) -> dict[str, Any]:
    op: dict[str, Any] = {
        'op': 'list_paragraph',
        'find': find,
        'kind': kind,
        'level': level,
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
    }
    if source_find:
        op['source_find'] = source_find
    return op


def _make_style_text_op(*, find: str, text_style: dict[str, Any]) -> dict[str, Any] | None:
    if not text_style:
        return None
    op: dict[str, Any] = {
        'op': 'style_text',
        'find': find,
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
    }
    if 'bold' in text_style:
        op['bold'] = bool(text_style['bold'])
    if 'face_name' in text_style:
        op['face_name'] = text_style['face_name']
    if 'height_pt' in text_style:
        op['height_pt'] = float(text_style['height_pt'])
    if len(op) == 5:
        return None
    return op


def _make_style_text_in_paragraph_op(*, paragraph_find: str, inline_style: InlineStyleToken) -> dict[str, Any] | None:
    if not inline_style.text_style or not inline_style.text:
        return None
    op: dict[str, Any] = {
        'op': 'style_text_in_paragraph',
        'paragraph_find': paragraph_find,
        'text': inline_style.text,
        'occurrence': int(inline_style.occurrence),
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
    }
    if 'bold' in inline_style.text_style:
        op['bold'] = bool(inline_style.text_style['bold'])
    if 'face_name' in inline_style.text_style:
        op['face_name'] = inline_style.text_style['face_name']
    if 'height_pt' in inline_style.text_style:
        op['height_pt'] = float(inline_style.text_style['height_pt'])
    if len(op) == 7:
        return None
    return op


def _make_paragraph_shape_op(*, find: str, native_style: dict[str, Any]) -> dict[str, Any] | None:
    if not native_style:
        return None
    op: dict[str, Any] = {
        'op': 'paragraph_shape',
        'find': find,
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
    }
    allowed_fields = {
        'align',
        'line_spacing',
        'indentation',
        'left_margin',
        'right_margin',
        'prev_spacing',
        'next_spacing',
        'pagebreak_before',
        'keep_lines_together',
        'keep_with_next',
        'widow_orphan',
    }
    for key, value in native_style.items():
        if key in allowed_fields:
            op[key] = value
    if len(op) == 5:
        return None
    return op


def _make_list_level_indent_op(*, find: str, level: int) -> dict[str, Any] | None:
    left_margin = NATIVE_BULLET_LEVEL_LEFT_MARGINS.get(level)
    if left_margin is None:
        return None
    op: dict[str, Any] = {
        'op': 'paragraph_shape',
        'find': find,
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
        'left_margin': left_margin,
    }
    op['indentation'] = 0.0 if level <= 1 else -left_margin
    return op


def _make_post_list_paragraph_spacing_op(*, find: str, native_style: dict[str, Any]) -> dict[str, Any] | None:
    if native_style and 'prev_spacing' in native_style:
        return None
    return {
        'op': 'paragraph_shape',
        'find': find,
        'apply': 'first',
        'match_case': True,
        'whole_word': False,
        'prev_spacing': LIST_TO_PARAGRAPH_PREV_SPACING,
    }
    


def _native_intent_key(op: dict[str, Any]) -> str | None:
    op_type = str(op.get('op') or '').strip()
    if op_type == 'native_action':
        return str(op.get('action') or '').strip() or 'unknown'
    if op_type == 'table_patch_cells':
        return 'table_patch_cells'
    if op_type == 'table_cell_replace_text':
        return 'table_cell_replace_text'
    if op_type == 'table_cell_action':
        action = str(op.get('action') or '').strip() or 'unknown'
        return f'table_cell_action:{action}'
    if op_type == 'cursor_snapshot':
        return 'cursor_snapshot'
    if op_type in {'paragraph_replace_native', 'paragraph_range_replace_native'}:
        return op_type
    if op_type == 'list_paragraph':
        kind = str(op.get('kind') or '').strip() or 'unknown'
        level = op.get('level')
        return f'list_paragraph:{kind}:level_{level}'
    if op_type in {'paragraph_shape', 'align_paragraph', 'style_text'}:
        return op_type
    return None


def _is_native_intent_op(op: dict[str, Any]) -> bool:
    return _native_intent_key(op) is not None


def _summarize_native_action_usage(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for op in operations:
        action = _native_intent_key(op)
        if action is None:
            continue
        counts[action] = counts.get(action, 0) + 1
    return [
        {'action': action, 'count': count}
        for action, count in sorted(counts.items())
    ]


def _build_touched_ranges(apply_scope_previews: list[ApplyScopePreview]) -> list[dict[str, Any]]:
    touched: list[dict[str, Any]] = []
    for preview in apply_scope_previews:
        touched.append(
            {
                'section_key': preview.section_key,
                'resolved_target_id': preview.resolved_target_id,
                'resolved_via': preview.resolved_via,
                'target_kind': preview.target_kind,
                'edit_shape': preview.edit_shape,
                'start_paragraph_id': preview.start_paragraph_id,
                'end_paragraph_id': preview.end_paragraph_id,
                'paragraph_count': preview.paragraph_count,
            }
        )
    return touched


def _build_unresolved_targets(recommendations: list[SectionTargetRecommendation]) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for item in recommendations:
        unresolved.append(
            {
                'section_key': item.section_key,
                'target_hint': item.target_hint,
                'recommended_resolved_target_id': item.recommended_resolved_target_id,
                'selection_mode': item.selection_mode,
                'is_ambiguous': item.is_ambiguous,
                'candidate_count': item.candidate_count,
            }
        )
    return unresolved


def _infer_execution_mode(*, operations: list[dict[str, Any]], unresolved_targets: list[dict[str, Any]]) -> str:
    if unresolved_targets:
        return 'blocked'
    has_native = any(_is_native_intent_op(op) for op in operations)
    has_non_native = any(not _is_native_intent_op(op) for op in operations)
    if has_native and has_non_native:
        return 'mixed'
    if has_native:
        return 'native'
    return 'fallback'


def _infer_fallback_reasons(*, operations: list[dict[str, Any]], unresolved_targets: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if unresolved_targets:
        reasons.append('exact_target_confirmation_required')
    if any(op.get('op') in {'replace_text_safe', 'replace_paragraph_safe', 'replace_paragraph_range_safe', 'insert_after_text', 'insert_before_text'} for op in operations):
        reasons.append('text_anchor_replace_path_still_present')
    if any(op.get('op') in {'clone_paragraph_shape', 'clone_text_style'} for op in operations):
        reasons.append('exemplar_or_legacy_style_ops_still_present')
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def normalize_policy_override(raw: Any) -> dict[str, Any]:
    if raw in (None, '', {}):
        return {}
    if not isinstance(raw, dict):
        raise TemplateEngineError('policy_override must be an object when provided')
    normalized: dict[str, Any] = {}
    decision_override = raw.get('decision_override')
    if decision_override is not None:
        if not isinstance(decision_override, str):
            raise TemplateEngineError('policy_override.decision_override must be a string when provided')
        decision = decision_override.strip().lower()
        if decision not in {'auto', 'confirm', 'manual'}:
            raise TemplateEngineError('policy_override.decision_override must be one of auto, confirm, manual')
        normalized['decision_override'] = decision
    shape_key = raw.get('shape_key')
    if shape_key is not None:
        if not isinstance(shape_key, str) or not shape_key.strip():
            raise TemplateEngineError('policy_override.shape_key must be a non-empty string when provided')
        normalized['shape_key'] = shape_key.strip()
    source = raw.get('source')
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            raise TemplateEngineError('policy_override.source must be a non-empty string when provided')
        normalized['source'] = source.strip()
    note = raw.get('note')
    if note is not None:
        if not isinstance(note, str):
            raise TemplateEngineError('policy_override.note must be a string when provided')
        normalized['note'] = note
    return normalized


def _summarize_intent_summary(operations: list[dict[str, Any]]) -> str:
    if not operations:
        return 'No edit operations compiled'
    op_counts: dict[str, int] = {}
    for op in operations:
        op_name = str(op.get('op', 'unknown'))
        op_counts[op_name] = op_counts.get(op_name, 0) + 1
    ordered = sorted(op_counts.items(), key=lambda item: (-item[1], item[0]))
    parts = [f'{name} x{count}' for name, count in ordered[:3]]
    suffix = ' + more' if len(ordered) > 3 else ''
    return ', '.join(parts) + suffix


def _summarize_target_summary(
    *,
    apply_scope_previews: list[ApplyScopePreview],
    touched_ranges: list[dict[str, Any]],
    unresolved_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    sections = [item.section_key for item in apply_scope_previews if item.section_key]
    unique_sections: list[str] = []
    for section in sections:
        if section not in unique_sections:
            unique_sections.append(section)
    resolved_targets = [item.resolved_target_id for item in apply_scope_previews if item.resolved_target_id]
    unique_targets: list[str] = []
    for target in resolved_targets:
        if target not in unique_targets:
            unique_targets.append(target)
    table_roles = [item.section_key for item in apply_scope_previews if item.edit_shape == 'table_patch_cells']
    return {
        'section_keys': unique_sections,
        'resolved_target_count': len(unique_targets),
        'resolved_target_ids': unique_targets[:5],
        'touched_range_count': len(touched_ranges),
        'table_role_count': len(table_roles),
        'unresolved_target_count': len(unresolved_targets),
    }


def _infer_confirm_unit(operations: list[dict[str, Any]], apply_scope_previews: list[ApplyScopePreview]) -> dict[str, Any]:
    if not operations:
        return {'kind': 'none', 'summary': 'No confirm unit'}
    op_names = {str(op.get('op', 'unknown')) for op in operations}
    if op_names == {'table_patch_cells'} and len(operations) == 1:
        patch_count = len(operations[0].get('patches', [])) if isinstance(operations[0].get('patches'), list) else None
        section_key = apply_scope_previews[0].section_key if apply_scope_previews else None
        return {
            'kind': 'single_table_batch',
            'summary': f'single table batch{f" in {section_key}" if section_key else ""}',
            'patch_count': patch_count,
        }
    if op_names.issubset({'cursor_insert_text', 'cursor_replace_text', 'cursor_delete_range'}) and len(operations) >= 1:
        return {
            'kind': 'cursor_scope_batch',
            'summary': f'cursor-scope batch with {len(operations)} op(s)',
            'operation_count': len(operations),
        }
    return {
        'kind': 'mixed_batch',
        'summary': f'mixed batch with {len(operations)} op(s)',
        'operation_count': len(operations),
    }


def _build_change_preview(apply_scope_previews: list[ApplyScopePreview], operations: list[dict[str, Any]]) -> dict[str, Any]:
    previews: list[dict[str, Any]] = []
    for item in apply_scope_previews[:3]:
        previews.append(
            {
                'section_key': item.section_key,
                'edit_shape': item.edit_shape,
                'resolved_target_id': item.resolved_target_id,
                'resolved_target_type': item.resolved_target_type,
                'heading_paragraph_id': item.heading_paragraph_id,
                'body_start_paragraph_id': item.body_start_paragraph_id,
                'body_end_paragraph_id': item.body_end_paragraph_id,
                'warning_state': item.warning_state,
                'blocking_warning_codes': item.blocking_warning_codes,
                'why_not_body_safe': item.why_not_body_safe[:200],
                'heading_before_preview_text': item.heading_before_preview_text[:120],
                'before_preview_text': item.body_before_preview_text[:160],
                'after_preview_text': item.apply_preview_text[:160],
            }
        )
    table_patch_cell_count = sum(
        len(op.get('patches', []))
        for op in operations
        if str(op.get('op')) == 'table_patch_cells' and isinstance(op.get('patches'), list)
    )
    return {
        'preview_count': len(previews),
        'previews': previews,
        'table_patch_cell_count': table_patch_cell_count,
    }


def _build_interactive_choices(
    *,
    warning_badges: list[WarningBadge],
    risk_flags: list[str],
    next_action: str | None,
) -> list[dict[str, str]]:
    choices: list[dict[str, str]] = []
    badge_codes = {str(item.code) for item in warning_badges}

    def add(action: str, label: str, reason: str) -> None:
        if any(existing.get('action') == action for existing in choices):
            return
        choices.append({'action': action, 'label': label, 'reason': reason})

    if 'heading_preserved_body_only' in badge_codes:
        add('review_heading_scope', 'heading 보호 상태 검토', 'resolved_target_id가 heading까지 포함되어 body-only로 자동 조정되었습니다.')
    if 'empty_target_body_range' in badge_codes:
        add('choose_anchor_or_insert_mode', 'anchor 또는 삽입 위치 재선택', '본문 범위를 찾지 못해 heading 뒤 삽입으로 degrade될 수 있습니다.')
    if 'style_drift' in badge_codes:
        add('recompile_without_list_lowering', 'list/style lowering 재검토', '스타일 상속이 본문과 다를 수 있습니다.')
        add('continue_with_render_review', '렌더 diff를 보고 진행', '현재 컴파일은 가능하지만 스타일 drift 검토가 필요합니다.')
    if 'exact_target_text_mismatch' in badge_codes:
        add('narrow_target_range', '대상 범위 축소', '현재 target이 의도한 문단과 다를 가능성이 있습니다.')
    if any(flag.startswith('failure_reason:') for flag in risk_flags) and next_action:
        add(next_action, '권장 다음 동작 실행', f'컴파일이 자동 적용 대신 {next_action} 단계를 요구합니다.')
    if not choices:
        add('approve_or_regenerate', '적용 또는 재생성 선택', '현재 상태를 확인한 뒤 승인하거나 재생성할 수 있습니다.')
    return choices


def _build_target_conflict_groups(target_recommendations: list[SectionTargetRecommendation]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for recommendation in target_recommendations:
        section_key = recommendation.section_key
        for candidate in recommendation.candidates[:3]:
            target_id = str(candidate.resolved_target_id or '').strip()
            if not target_id:
                continue
            groups.setdefault(target_id, []).append(
                {
                    'section_key': section_key,
                    'heading_text': candidate.heading_text,
                    'selection_preview': candidate.selection_preview or candidate.preview_text,
                    'confidence_label': candidate.confidence_label,
                }
            )
    conflict_groups: list[dict[str, Any]] = []
    for target_id, items in groups.items():
        section_keys = sorted({item['section_key'] for item in items})
        if len(section_keys) < 2:
            continue
        conflict_groups.append(
            {
                'resolved_target_id': target_id,
                'section_keys': section_keys,
                'items': items,
                'summary': f"{target_id} is recommended for multiple sections: {', '.join(section_keys)}",
            }
        )
    return conflict_groups


def _candidate_wrong_domain(section_key: str, candidate: TargetCandidate) -> bool:
    section_tokens = _token_set(section_key)
    heading_tokens = _token_set(candidate.heading_text)
    overlap = len(section_tokens & heading_tokens)
    strong_match = any(signal in {'exact_heading', 'normalized_contains', 'body_contains'} for signal in candidate.matched_signals)
    return overlap == 0 and not strong_match and candidate.confidence < 0.9


def _candidate_brief_summary(candidate: TargetCandidate | None) -> str:
    if candidate is None:
        return '후보 없음'
    parts: list[str] = [f"{candidate.confidence_label} confidence"]
    body_signal = any(
        signal in {'body_contains'}
        or str(signal).startswith('authored_preview_')
        or str(signal).startswith('token_overlap:')
        for signal in candidate.matched_signals
    )
    if any(signal == 'exact_heading' for signal in candidate.matched_signals):
        parts.append('heading 일치')
    elif any(signal == 'normalized_contains' or str(signal).startswith('section_key_token_overlap:') for signal in candidate.matched_signals):
        parts.append('heading 유사')
    else:
        parts.append('heading 약함')
    if body_signal:
        parts.append('body 문맥 강함')
    if candidate.is_conflicting:
        parts.append('충돌')
    else:
        parts.append('비충돌')
    if candidate.domain_note:
        parts.append('domain mismatch')
    return ' / '.join(parts)


def _build_candidate_comparison_summary(recommendation: SectionTargetRecommendation) -> str:
    candidates = recommendation.candidates[:2]
    if not candidates:
        return ''
    top = candidates[0]
    if len(candidates) == 1:
        return f"top-1은 {_candidate_brief_summary(top)}"
    second = candidates[1]
    return f"top-1은 {_candidate_brief_summary(top)}, top-2는 {_candidate_brief_summary(second)}"


def _heading_strength(candidate: TargetCandidate | None) -> int:
    if candidate is None:
        return 0
    if any(signal == 'exact_heading' for signal in candidate.matched_signals):
        return 3
    if any(signal == 'normalized_contains' or str(signal).startswith('section_key_token_overlap:') for signal in candidate.matched_signals):
        return 2
    return 1


def _has_body_strength(candidate: TargetCandidate | None) -> bool:
    if candidate is None:
        return False
    return any(
        signal in {'body_contains'}
        or str(signal).startswith('authored_preview_')
        or str(signal).startswith('token_overlap:')
        for signal in candidate.matched_signals
    )


def _build_top_choice_summary(recommendation: SectionTargetRecommendation) -> str:
    candidates = recommendation.candidates[:2]
    if not candidates:
        return ''
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    if recommendation.candidate_state == 'retarget_required' or top.domain_note:
        return '그나마 top-1이 낫지만 재탐색이 우선입니다.'
    if recommendation.candidate_state == 'needs_conflict_resolution':
        return 'top-1은 유력하지만 먼저 충돌 해소가 우선입니다.'
    if second is None:
        if _has_body_strength(top) and _heading_strength(top) <= 1:
            return 'top-1이 body 문맥 기준으로 가장 그럴듯합니다.'
        return 'top-1이 현재 기준에서 가장 그럴듯한 후보입니다.'

    confidence_gap = float(top.confidence) - float(second.confidence)
    same_heading = _heading_strength(top) == _heading_strength(second)
    same_conflict = bool(top.is_conflicting) == bool(second.is_conflicting)
    same_domain = bool(top.domain_note) == bool(second.domain_note)
    if abs(confidence_gap) < 0.08 and same_heading and same_conflict and same_domain:
        return 'top-1과 top-2는 사실상 동점입니다. 수동 선택 또는 재탐색이 안전합니다.'
    if not top.is_conflicting and second.is_conflicting:
        return 'top-1이 비충돌 후보라 우선순위가 높습니다.'
    if not top.domain_note and second.domain_note:
        return 'top-1이 wrong-domain 가능성이 더 낮습니다.'
    if _heading_strength(top) > _heading_strength(second):
        return 'top-1이 heading 정합성이 더 좋습니다.'
    if _has_body_strength(top) and not _has_body_strength(second):
        return 'top-1이 body 문맥 더 적합합니다.'
    if top.confidence_label == 'low' and second.confidence_label == 'low':
        return 'top-1이 상대적으로 더 낫습니다.'
    if confidence_gap >= 0.12:
        return 'top-1 confidence가 충분히 더 높습니다.'
    if _has_body_strength(top) and _heading_strength(top) <= 1:
        return 'top-1은 heading보다 body 문맥 근거가 더 강합니다.'
    return 'top-1이 현재 기준에서 조금 더 우세합니다.'


def _needs_advanced_style_action(recommendation: SectionTargetRecommendation) -> bool:
    top = recommendation.candidates[0] if recommendation.candidates else None
    if top is None:
        return False
    if recommendation.candidate_state in {'retarget_required', 'needs_conflict_resolution'}:
        return False
    if top.confidence_label == 'low' and _has_body_strength(top):
        return True
    if _heading_strength(top) <= 1 and _has_body_strength(top):
        return True
    return False


def _summarize_ownership_note(shared_sections: list[str], current_section_key: str) -> str:
    others = [item for item in shared_sections if item != current_section_key]
    if not others:
        return '여러 section이 같은 target을 공유함'
    if len(others) == 1:
        return f'이미 사용 중/경합: {others[0]}'
    return f'이미 사용 중/경합: {others[0]} 외 {len(others) - 1}개'


def _annotate_target_recommendations(
    target_recommendations: list[SectionTargetRecommendation],
    target_conflict_groups: list[dict[str, Any]],
) -> None:
    conflict_map = {
        str(group.get('resolved_target_id')): sorted(group.get('section_keys', []))
        for group in target_conflict_groups
    }

    for recommendation in target_recommendations:
        top_candidate = recommendation.candidates[0] if recommendation.candidates else None
        top_conflict = conflict_map.get(str(top_candidate.resolved_target_id)) if top_candidate else None

        for candidate in recommendation.candidates:
            shared_sections = conflict_map.get(str(candidate.resolved_target_id))
            if shared_sections and len(shared_sections) > 1:
                candidate.is_conflicting = True
                candidate.ownership_note = _summarize_ownership_note(shared_sections, recommendation.section_key)
            if _candidate_wrong_domain(recommendation.section_key, candidate):
                candidate.domain_note = '현재 section과 의미 영역이 다를 가능성 높음'

        if not top_candidate:
            recommendation.candidate_state = 'retarget_required'
            recommendation.candidate_state_label = '재탐색 필요'
            recommendation.candidate_state_reason = '후보가 생성되지 않았습니다.'
        elif top_candidate.domain_note:
            recommendation.candidate_state = 'retarget_required'
            recommendation.candidate_state_label = '재탐색 필요'
            recommendation.candidate_state_reason = top_candidate.domain_note
        elif top_conflict and len(top_conflict) > 1:
            recommendation.candidate_state = 'needs_conflict_resolution'
            recommendation.candidate_state_label = '중복 후보 조정 필요'
            recommendation.candidate_state_reason = f"top candidate가 다른 section과 충돌합니다: {', '.join(item for item in top_conflict if item != recommendation.section_key)}"
        elif not recommendation.is_ambiguous and top_candidate.confidence >= 0.9:
            recommendation.candidate_state = 'easy_pick'
            recommendation.candidate_state_label = '고르기 쉬운 후보'
            recommendation.candidate_state_reason = 'heading/body 문맥이 잘 맞고 top candidate가 명확합니다.'
        else:
            recommendation.candidate_state = 'pickable'
            recommendation.candidate_state_label = '애매하지만 선택 가능'
            recommendation.candidate_state_reason = '후보는 있으나 다른 후보와 함께 비교 확인이 필요합니다.'
            if top_candidate and top_candidate.confidence_label == 'low':
                recommendation.candidate_state_reason = '후보는 있으나 모두 약한 편이라 수동 선택 또는 재탐색이 안전합니다.'

        recommendation.section_domain_note = top_candidate.domain_note if top_candidate and top_candidate.domain_note else ''
        if top_conflict and top_candidate:
            others = [item for item in top_conflict if item != recommendation.section_key]
            recommendation.conflict_priority_summary = (
                f"먼저 {others[0]}와의 충돌을 풀어야 합니다. 이 target을 공유하는 section {len(top_conflict)}개"
                if others else f"이 target을 공유하는 section {len(top_conflict)}개"
            )
        recommendation.candidate_comparison_summary = _build_candidate_comparison_summary(recommendation)
        recommendation.top_choice_summary = _build_top_choice_summary(recommendation)

        actions: list[dict[str, str]] = []
        advanced_actions: list[dict[str, str]] = []
        def add_action(action: str, label: str, reason: str) -> None:
            if any(existing.get('action') == action for existing in actions):
                return
            actions.append({'action': action, 'label': label, 'reason': reason})

        def add_advanced_action(action: str, label: str, reason: str) -> None:
            if any(existing.get('action') == action for existing in advanced_actions):
                return
            advanced_actions.append({'action': action, 'label': label, 'reason': reason})

        if recommendation.candidate_state == 'needs_conflict_resolution':
            add_action('resolve_conflict_for_section', '중복 target 충돌 해소', recommendation.conflict_priority_summary or '같은 target을 공유하는 section들 중 하나를 재배치합니다.')
            if recommendation.is_ambiguous:
                add_action('choose_candidate_for_section', '이 section만 exact target 재선택', '충돌 없는 다른 후보가 있으면 그쪽으로 바로 옮깁니다.')
            recommendation.recommended_action = 'resolve_conflict_for_section'
        elif recommendation.candidate_state == 'retarget_required':
            add_action('retry_section_search', '이 section만 다시 찾기', '현재 후보 품질이 낮거나 wrong-domain이라 재탐색이 필요합니다.')
            if recommendation.is_ambiguous:
                add_action('choose_candidate_for_section', '이 section만 exact target 재선택', '현재 후보 중 문맥이 가장 맞는 target이 있으면 수동으로 고릅니다.')
            recommendation.recommended_action = 'retry_section_search'
        elif recommendation.is_ambiguous:
            add_action('choose_candidate_for_section', '이 section만 exact target 재선택', '후보 여러 개 중 문맥이 맞는 target을 고릅니다.')
            if top_candidate and top_candidate.confidence_label != 'low' and not top_candidate.domain_note:
                add_action('confirm_top_candidate', '이 section의 top candidate 확정', 'top-1이 충분히 그럴듯하면 그대로 사용할 수 있습니다.')
            recommendation.recommended_action = 'choose_candidate_for_section'
        else:
            if top_candidate and top_candidate.confidence_label != 'low' and not top_candidate.domain_note:
                add_action('confirm_top_candidate', '이 section의 top candidate 확정', '현재 추천 1순위를 그대로 사용할 수 있습니다.')
                recommendation.recommended_action = 'confirm_top_candidate'
            else:
                add_action('choose_candidate_for_section', '이 section만 exact target 재선택', 'low confidence 후보는 직접 확인 후 고르는 편이 안전합니다.')
                recommendation.recommended_action = 'choose_candidate_for_section'

        if _needs_advanced_style_action(recommendation):
            add_advanced_action('recompile_section_without_list_lowering', '스타일 상속 끄고 다시 compile', 'style drift가 걱정되면 이 section만 본문형으로 다시 컴파일합니다.')
        recommendation.section_actions = actions
        recommendation.advanced_section_actions = advanced_actions


def _build_confirm_policy(
    *,
    template_version: str | None,
    operations: list[dict[str, Any]],
    execution_mode: str,
    unresolved_targets: list[dict[str, Any]],
    target_recommendations: list[SectionTargetRecommendation],
    fallback_reason: list[str],
    failure_reason: str | None,
    apply_scope_previews: list[ApplyScopePreview],
    touched_ranges: list[dict[str, Any]],
    warning_badges: list[WarningBadge],
    compile_readiness: dict[str, Any],
    policy_override: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    risk_flags: list[str] = []
    blocking_flags: list[str] = []

    if execution_mode != 'native':
        blocking_flags.append('execution_mode_not_native')
    if unresolved_targets:
        blocking_flags.append('unresolved_targets_present')
    if failure_reason:
        blocking_flags.append(f'failure_reason:{failure_reason}')
    if any(bool(item.blocking) for item in warning_badges):
        blocking_flags.append('blocking_warning_badges_present')
    precondition = compile_readiness.get('precondition') if isinstance(compile_readiness, dict) else None
    if not isinstance(precondition, dict) or not precondition.get('inspect_snapshot_id'):
        blocking_flags.append('inspect_snapshot_guard_missing')

    has_table_ops = any(str(op.get('op')) in {'table_patch_cells', 'table_cell_replace_text', 'table_cell_action'} for op in operations)
    # Recommendation-level conflict checks can miss collisions after exact targets have been
    # normalized into emitted native ops, so confirm policy re-scans emitted operations and
    # fails closed on duplicate canonical shared-table claims.
    emitted_shared_table_scan = _scan_emitted_shared_table_identities(operations) if has_table_ops else {
        'shared_table_target_keys': [],
        'collision_groups': [],
    }
    emitted_shared_table_keys = list(emitted_shared_table_scan.get('shared_table_target_keys') or [])
    emitted_shared_table_collision_groups = list(emitted_shared_table_scan.get('collision_groups') or [])
    emitted_shared_table_bundle_collisions = [
        group for group in emitted_shared_table_collision_groups if int(group.get('bundle_count') or group.get('claim_count') or 0) > 1
    ]
    compile_shared_table_conflict_groups = list(compile_readiness.get('shared_table_conflict_groups') or []) if isinstance(compile_readiness, dict) else []
    if has_table_ops:
        if any(str(op.get('op')) == 'table_patch_cells' and not op.get('expected_table_fingerprint') for op in operations):
            blocking_flags.append('table_fingerprint_guard_missing')
        if emitted_shared_table_bundle_collisions:
            blocking_flags.append('duplicate_emitted_shared_table_identity')
        risk_flags.append('table_edit_present')

    if any(str(op.get('op')) == 'list_paragraph' for op in operations):
        blocking_flags.append('list_structure_present')

    if any(str(op.get('op')) in {'cursor_replace_text', 'cursor_delete_range'} and not op.get('expected_selected_text') for op in operations):
        risk_flags.append('cursor_expected_selected_text_missing')

    if any(item.edit_shape == 'table_patch_cells' for item in apply_scope_previews):
        risk_flags.append('table_batch_scope')
    if len(touched_ranges) > 1:
        risk_flags.append('multiple_touched_ranges')
    if any(str(op.get('op')) in {'replace_paragraph_range_safe', 'insert_after_text', 'insert_before_text'} for op in operations):
        risk_flags.append('wide_or_anchor_based_replace')
    if fallback_reason:
        risk_flags.extend(f'fallback_reason:{item}' for item in fallback_reason)

    risk_flags.append('prior_evidence_missing_for_auto')
    risk_flags.append('render_qa_evidence_missing_for_auto')

    deduped_blocking: list[str] = []
    for item in blocking_flags:
        if item not in deduped_blocking:
            deduped_blocking.append(item)
    deduped_risk: list[str] = []
    for item in risk_flags:
        if item not in deduped_risk and item not in deduped_blocking:
            deduped_risk.append(item)

    auto_eligible = False
    decision = 'confirm'
    decision_reason = 'default_confirm_policy'
    override_applied = False
    override_rejected_reason = None
    if deduped_blocking:
        decision = 'confirm'
        decision_reason = 'force_confirm_conditions_present'
    elif auto_eligible:
        decision = 'auto'
        decision_reason = 'fully_guarded_native_repeat_shape'

    requested_override = policy_override.get('decision_override') if isinstance(policy_override, dict) else None
    if requested_override == 'auto':
        if deduped_blocking:
            override_rejected_reason = 'cannot_force_auto_through_blocking_flags'
        else:
            decision = 'auto'
            decision_reason = 'user_agent_override:auto'
            override_applied = True
    elif requested_override == 'confirm':
        decision = 'confirm'
        decision_reason = 'user_agent_override:confirm'
        override_applied = True
    elif requested_override == 'manual':
        decision = 'manual'
        decision_reason = 'user_agent_override:manual'
        override_applied = True

    risk_score = 0
    risk_score += 80 if deduped_blocking else 0
    risk_score += min(20, len(deduped_risk) * 4)
    if has_table_ops:
        risk_score += 5
    risk_score = min(risk_score, 100)

    confirm_unit = _infer_confirm_unit(operations, apply_scope_previews)
    target_summary = _summarize_target_summary(
        apply_scope_previews=apply_scope_previews,
        touched_ranges=touched_ranges,
        unresolved_targets=unresolved_targets,
    )
    target_conflict_groups = _build_target_conflict_groups(target_recommendations)
    _annotate_target_recommendations(target_recommendations, target_conflict_groups)
    if target_conflict_groups:
        for group in target_conflict_groups:
            warning_badges.append(
                _build_warning_badge(
                    code='duplicate_target_candidates',
                    severity='warning',
                    stage='compile',
                    blocking=False,
                    resolved_target_id=group.get('resolved_target_id'),
                    summary='Multiple sections share the same target candidate.',
                    detail=group.get('summary', ''),
                )
            )
    if emitted_shared_table_bundle_collisions:
        for group in emitted_shared_table_bundle_collisions:
            warning_badges.append(
                _build_warning_badge(
                    code='duplicate_emitted_shared_table_identity',
                    severity='error',
                    stage='compile',
                    blocking=True,
                    summary='Multiple emitted section/op bundles claim the same canonical shared-table target.',
                    detail=group.get('summary', ''),
                )
            )
    interactive_choices = _build_interactive_choices(
        warning_badges=warning_badges,
        risk_flags=deduped_blocking + deduped_risk,
        next_action=compile_readiness.get('next_action') if isinstance(compile_readiness, dict) else None,
    )
    approval_packet = {
        'schema_version': 'approval-packet/v1',
        'intent_summary': _summarize_intent_summary(operations),
        'target_summary': target_summary,
        'execution_mode': execution_mode,
        'guards': {
            # `approval_packet.guards` captures the exact compile-time identities the
            # confirm/approve path must trust: snapshot, resolved targets, table fingerprints,
            # and canonical emitted shared-table keys.
            'template_version': template_version,
            'inspect_snapshot_id': compile_readiness.get('precondition', {}).get('inspect_snapshot_id') if isinstance(compile_readiness.get('precondition'), dict) else None,
            'resolved_target_ids': target_summary.get('resolved_target_ids', []),
            'table_fingerprints': [
                op.get('expected_table_fingerprint')
                for op in operations
                if str(op.get('op')) == 'table_patch_cells' and op.get('expected_table_fingerprint')
            ],
            'emitted_shared_table_keys': emitted_shared_table_keys,
        },
        'change_preview': _build_change_preview(apply_scope_previews, operations),
        'risk_flags': deduped_blocking + deduped_risk,
        'confirm_unit': confirm_unit,
        'target_conflict_groups': target_conflict_groups,
        'shared_table_conflict_groups': compile_shared_table_conflict_groups,
        'emitted_shared_table_collision_groups': emitted_shared_table_bundle_collisions,
        'interactive_choices': interactive_choices,
        'policy_override': policy_override,
    }
    policy = {
        'schema_version': 'confirm-policy/v1',
        'default_mode': 'confirm',
        'manual_mode': 'debug_audit_only',
        'decision_axes': [
            'execution_mode',
            'guard_completeness',
            'structural_risk',
            'prior_evidence',
        ],
        'decision': decision,
        'decision_reason': decision_reason,
        'auto_eligible': auto_eligible,
        'force_confirm': bool(deduped_blocking),
        'blocking_flags': deduped_blocking,
        'risk_flags': deduped_risk,
        'risk_score': risk_score,
        'confirm_unit': confirm_unit,
        'target_conflict_groups': target_conflict_groups,
        'shared_table_conflict_groups': compile_shared_table_conflict_groups,
        'emitted_shared_table_collision_groups': emitted_shared_table_bundle_collisions,
        'interactive_choices': interactive_choices,
        'policy_override': policy_override,
        'override_applied': override_applied,
        'override_rejected_reason': override_rejected_reason,
    }
    return policy, approval_packet


def compile_markdown_to_edit_plan(
    input_path: str | Path,
    markdown: str,
    *,
    validation: dict[str, Any] | None = None,
    cleanup_policy: str = 'none',
    policy_override: dict[str, Any] | None = None,
    allowed_section_keys: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
) -> tuple[TemplateMap, StyleRoleCatalog, ContentSpec, EditPlan]:
    template_map = build_template_map(input_path)
    section_root_map = {section_name: root for section_name, root in _parse_section_roots(Path(input_path))}
    style_catalog = build_style_role_catalog(template_map)
    content_spec = parse_markdown_content(markdown)
    normalized_policy_override = normalize_policy_override(policy_override)
    normalized_allowed_section_keys = _normalize_section_key_list(allowed_section_keys)
    requested_snapshot_id = content_spec.inspect_snapshot_id or template_map.inspect_snapshot_id
    compile_readiness = {
        'precondition': {
            'inspect_snapshot_id': requested_snapshot_id,
            'current_snapshot_id': template_map.inspect_snapshot_id,
            'rule': 'precondition.inspect_snapshot_id == current_snapshot_id',
            'on_mismatch': 'hard_fail_before_apply',
            'mismatch_reason_code': 'snapshot_mismatch',
        }
    }
    if requested_snapshot_id != template_map.inspect_snapshot_id:
        raise TemplateEngineError(
            'snapshot_mismatch: '
            f'expected inspect_snapshot_id={requested_snapshot_id!r} but current_snapshot_id={template_map.inspect_snapshot_id!r}'
        )
    if content_spec.template_fingerprint and content_spec.template_fingerprint != template_map.template_fingerprint:
        raise TemplateEngineError(
            'template_fingerprint_mismatch: '
            f'expected template_fingerprint={content_spec.template_fingerprint!r} but current_template_fingerprint={template_map.template_fingerprint!r}'
        )
    warnings: list[str] = []
    warning_badges: list[WarningBadge] = []
    resolutions: list[AnchorResolution] = []
    target_recommendations: list[SectionTargetRecommendation] = []
    target_conflict_groups: list[dict[str, Any]] = []
    placeholder_resolutions: list[PlaceholderResolution] = []
    operations: list[dict[str, Any]] = []
    instruction_metadata: dict[str, Any] = {}
    apply_scope_previews: list[ApplyScopePreview] = []
    used_placeholders: set[str] = set()
    used_resolved_target_sections: dict[str, str] = {}
    used_shared_table_targets: dict[str, dict[str, Any]] = {}
    shared_table_conflict_groups_map: dict[str, dict[str, Any]] = {}

    requires_exact_target_confirmation = False
    requires_structural_target_rework = False
    unsafe_structural_ranges: list[dict[str, Any]] = []
    native_runtime_candidates: list[dict[str, Any]] = []
    authored_table_patch_present = False
    authored_table_patch_section_count = 0
    skipped_shared_table_patch_section_count = 0

    _raise_if_section_scope_widened(
        stage='parsed_content_spec',
        observed_section_keys=[section.section_key for section in content_spec.sections],
        allowed_section_keys=normalized_allowed_section_keys,
    )
    if normalized_allowed_section_keys:
        instruction_metadata['allowed_section_keys'] = normalized_allowed_section_keys

    for section in content_spec.sections:
        _raise_if_section_scope_widened(
            stage=f'section_iteration:{section.section_key}',
            observed_section_keys=[section.section_key],
            allowed_section_keys=normalized_allowed_section_keys,
        )
        if section.placeholder and (section.table_patches or section.table_row_patches or section.table_record_patches):
            raise TemplateEngineError('table patch sections cannot also use @placeholder')
        if (section.table_patches or section.table_row_patches or section.table_record_patches) and section.blocks:
            raise TemplateEngineError('table patch sections cannot mix @table_cell directives with paragraph or list blocks')

        rendered_lines: list[str] = []
        block_renderings: list[tuple[ContentBlock, list[str]]] = []
        for block in section.blocks:
            block_lines = _render_block_lines(block)
            block_renderings.append((block, block_lines))
            rendered_lines.extend(block_lines)

        has_table_patches = bool(section.table_patches or section.table_row_patches or section.table_record_patches)
        if has_table_patches:
            authored_table_patch_present = True
            authored_table_patch_section_count += 1
        authored_preview_text = (
            ' | '.join(
                part for part in [
                    _table_patch_preview(section.table_patches) if section.table_patches else '',
                    _table_row_patch_preview(section.table_row_patches) if section.table_row_patches else '',
                    _table_record_patch_preview(section.table_record_patches) if section.table_record_patches else '',
                ]
                if part
            )
            if has_table_patches
            else _authored_preview_text(rendered_lines)
        )

        if not rendered_lines and not has_table_patches:
            warnings.append(f'section {section.section_key!r} produced no rendered lines and was skipped')
            continue

        if section.placeholder:
            placeholder_resolution = resolve_placeholder(
                template_map,
                section.placeholder,
                section_key=section.section_key,
                raw_input=section.placeholder,
            )
            placeholder_resolutions.append(placeholder_resolution)
            used_placeholders.add(section.placeholder)
            replace_payload = {
                'op': 'replace_text_safe',
                'find': section.placeholder,
                'replace': _join_rendered_lines(rendered_lines),
                'apply': 'first',
                'match_case': True,
                'whole_word': False,
                'allow_packed_paragraph': True,
            }
            apply_scope_previews.append(
                _build_apply_scope_preview(
                    section=section,
                    edit_shape='replace_text_safe',
                    exact_target_locked=True,
                    resolved_target_type='placeholder',
                    target_kind='placeholder',
                    resolved_target_id=section.placeholder,
                    resolved_via='exact_placeholder_token',
                    before_preview_text=section.placeholder,
                    after_preview_text=_join_rendered_lines(rendered_lines),
                )
            )
        else:
            if section.resolved_target_id:
                anchor, target_paragraph, target_end_paragraph, resolution = resolve_exact_target(
                    template_map,
                    section.resolved_target_id,
                    section_key=section.section_key,
                    target_hint=section.target_hint,
                )
            else:
                recommendation = build_section_target_recommendation(
                    template_map,
                    section_key=section.section_key,
                    target_hint=section.target_hint,
                    authored_preview_text=authored_preview_text,
                    authored_blocks=section.blocks,
                    prefer_non_structural=not has_table_patches,
                )
                target_recommendations.append(recommendation)
                requires_exact_target_confirmation = True
                warnings.append(
                    f'section {section.section_key!r} requires exact target confirmation before apply; '
                    f'recommended_resolved_target_id={recommendation.recommended_resolved_target_id!r}'
                )
                warning_badges.append(
                    _build_warning_badge(
                        code='target_ambiguous' if recommendation.is_ambiguous else 'exact_target_confirmation_required',
                        severity='error' if recommendation.is_ambiguous else 'warning',
                        blocking=True,
                        section_key=section.section_key,
                        resolved_target_id=recommendation.recommended_resolved_target_id,
                        summary=(
                            'Exact target confirmation is required before apply.'
                            if not recommendation.is_ambiguous
                            else 'Target is ambiguous. Choose one exact candidate before apply.'
                        ),
                        detail=recommendation.selection_instruction,
                    )
                )
                top_candidate = recommendation.candidates[0]
                anchor = next(item for item in template_map.anchors if item.anchor_id == top_candidate.matched_anchor_id)
                target_paragraph = _find_paragraph(template_map, top_candidate.body_start_paragraph_id)
                target_end_paragraph = (
                    _find_paragraph(template_map, top_candidate.body_end_paragraph_id)
                    if top_candidate.body_end_paragraph_id != top_candidate.body_start_paragraph_id
                    else None
                )
                resolution = AnchorResolution(
                    section_key=section.section_key,
                    target_hint=section.target_hint,
                    inspect_snapshot_id=template_map.inspect_snapshot_id,
                    resolved_via='target_hint_recommendation',
                    resolved_target_id=top_candidate.resolved_target_id,
                    matched_anchor_id=top_candidate.matched_anchor_id,
                    matched_heading=top_candidate.heading_text,
                    body_anchor_text=top_candidate.preview_text or top_candidate.body_anchor_text,
                    confidence=top_candidate.confidence,
                    matched_signals=top_candidate.matched_signals,
                    alternatives=[candidate.heading_text for candidate in recommendation.candidates[1:]],
                )
            resolutions.append(resolution)
            prior_section_key = used_resolved_target_sections.get(resolution.resolved_target_id)
            if prior_section_key is not None:
                requires_exact_target_confirmation = True
                warnings.append(
                    f'section {section.section_key!r} reuses resolved_target_id {resolution.resolved_target_id!r} already assigned to '
                    f'section {prior_section_key!r}; duplicate target ranges are blocked until reassigned'
                )
                warning_badges.append(
                    _build_warning_badge(
                        code='duplicate_resolved_target_id',
                        severity='error',
                        stage='compile',
                        blocking=True,
                        section_key=section.section_key,
                        resolved_target_id=resolution.resolved_target_id,
                        summary='Resolved target is already assigned to another section.',
                        detail=(
                            f'current_section={section.section_key!r}; '
                            f'prior_section={prior_section_key!r}; '
                            f'resolved_target_id={resolution.resolved_target_id!r}. '
                            'Each paragraph range may be assigned to only one section in a compile pass.'
                        ),
                    )
                )
                continue
            used_resolved_target_sections[resolution.resolved_target_id] = section.section_key
            rendered_text = _join_rendered_lines(rendered_lines)
            rendered_text_for_replace = rendered_text
            if len(rendered_lines) > 1 and not rendered_text_for_replace.endswith('\r\n'):
                rendered_text_for_replace += '\r\n'

            boundary_start_paragraph, boundary_end_paragraph = _find_nonempty_range_boundaries(
                template_map,
                target_paragraph,
                target_end_paragraph,
            )

            heading_preserved = False
            if (
                boundary_start_paragraph is not None
                and boundary_end_paragraph is not None
                and target_end_paragraph is not None
                and _paragraph_matches_section_heading(target_paragraph.text, section.section_key)
                and boundary_start_paragraph.paragraph_id == target_paragraph.paragraph_id
            ):
                next_body_paragraph = None
                for para in template_map.paragraphs:
                    if para.section != target_paragraph.section:
                        continue
                    if target_paragraph.section_paragraph_index < para.section_paragraph_index <= target_end_paragraph.section_paragraph_index and _is_body_anchor_candidate(para):
                        next_body_paragraph = para
                        break
                if next_body_paragraph is not None:
                    boundary_start_paragraph = next_body_paragraph
                    heading_preserved = True
                else:
                    boundary_start_paragraph = None
                    boundary_end_paragraph = None
                    heading_preserved = True

            if heading_preserved:
                warnings.append(
                    f"section {section.section_key!r} includes its own heading in resolved_target_id; compiler preserved the heading and limited replacement to body paragraphs"
                )
                warning_badges.append(
                    _build_warning_badge(
                        code='heading_preserved_body_only',
                        severity='warning',
                        stage='compile',
                        blocking=False,
                        section_key=section.section_key,
                        resolved_target_id=resolution.resolved_target_id,
                        summary='Resolved target included the section heading, so compile preserved the heading and shifted replacement to body-only.',
                        detail=f'heading_text={target_paragraph.text!r}',
                    )
                )

            if has_table_patches and boundary_start_paragraph is None:
                boundary_start_paragraph = target_paragraph
            if has_table_patches and boundary_end_paragraph is None:
                boundary_end_paragraph = target_end_paragraph or target_paragraph

            if boundary_start_paragraph is not None and boundary_end_paragraph is not None:
                preview_start_paragraph = boundary_start_paragraph
                preview_end_paragraph = boundary_end_paragraph if boundary_end_paragraph.paragraph_id != boundary_start_paragraph.paragraph_id else None
            else:
                preview_start_paragraph = None
                preview_end_paragraph = None

            if (
                not has_table_patches
                and target_paragraph is not None
                and target_paragraph.paragraph_class in {'heading', 'number'}
                and not heading_preserved
            ):
                preview_start_paragraph = None
                preview_end_paragraph = None

            scope_preview = _build_apply_scope_preview(
                section=section,
                edit_shape='table_patch_cells' if has_table_patches else 'replace_body_only',
                exact_target_locked=bool(section.resolved_target_id),
                resolved_target_type=_infer_resolved_target_type(
                    target_paragraph=target_paragraph,
                    target_end_paragraph=target_end_paragraph,
                    has_table_patches=has_table_patches,
                ),
                target_kind='paragraph_range' if target_end_paragraph is not None else 'body_paragraph',
                resolved_target_id=resolution.resolved_target_id,
                resolved_via=resolution.resolved_via,
                before_preview_text=(
                    f'{preview_start_paragraph.text} {preview_end_paragraph.text}'
                    if preview_start_paragraph is not None and preview_end_paragraph is not None
                    else (preview_start_paragraph.text if preview_start_paragraph is not None else '')
                ),
                after_preview_text=authored_preview_text if has_table_patches else rendered_text,
                heading_paragraph=target_paragraph,
                body_start_paragraph=preview_start_paragraph,
                body_end_paragraph=preview_end_paragraph,
                heading_before_preview_text=target_paragraph.text or anchor.heading_text or '',
                body_before_preview_text=(
                    f'{preview_start_paragraph.text} {preview_end_paragraph.text}'
                    if preview_start_paragraph is not None and preview_end_paragraph is not None
                    else (preview_start_paragraph.text if preview_start_paragraph is not None else '')
                ),
                apply_preview_text=authored_preview_text if has_table_patches else rendered_text,
                why_not_body_safe=(
                    'No editable body paragraph was resolved from this target.'
                    if preview_start_paragraph is None
                    else ''
                ),
            )
            apply_scope_previews.append(scope_preview)

            if scope_preview.exact_target_locked and scope_preview.resolved_target_type == 'heading':
                requires_exact_target_confirmation = True
                scope_preview.warning_state = 'blocking'
                if 'exact_target_heading_like' not in scope_preview.blocking_warning_codes:
                    scope_preview.blocking_warning_codes.append('exact_target_heading_like')
                scope_preview.why_not_body_safe = (
                    f"Resolved target {resolution.resolved_target_id!r} is a heading paragraph "
                    f"{target_paragraph.paragraph_id!r}; editable body scope is "
                    f"{scope_preview.body_start_paragraph_id or 'none'}..{scope_preview.body_end_paragraph_id or 'none'}."
                )
                if not any(
                    badge.code == 'exact_target_heading_like'
                    and badge.section_key == section.section_key
                    and badge.resolved_target_id == resolution.resolved_target_id
                    for badge in warning_badges
                ):
                    warning_badges.append(
                        _build_warning_badge(
                            code='exact_target_heading_like',
                            severity='error',
                            stage='compile',
                            blocking=True,
                            section_key=section.section_key,
                            resolved_target_id=resolution.resolved_target_id,
                            summary='Exact target resolves to a heading paragraph, not an editable body paragraph.',
                            detail=(
                                f"heading_paragraph_id={target_paragraph.paragraph_id!r}; "
                                f"body_start_paragraph_id={scope_preview.body_start_paragraph_id!r}; "
                                f"body_end_paragraph_id={scope_preview.body_end_paragraph_id!r}; "
                                'choose a body paragraph or body range before apply'
                            ),
                        )
                    )

            if heading_preserved and scope_preview.warning_state != 'blocking':
                scope_preview.warning_state = 'warning'
                scope_preview.why_not_body_safe = (
                    f"Resolved target {resolution.resolved_target_id!r} includes heading paragraph "
                    f"{target_paragraph.paragraph_id!r}, so compile shifted preview to body-only scope "
                    f"{scope_preview.body_start_paragraph_id or 'none'}..{scope_preview.body_end_paragraph_id or 'none'}."
                )

            if boundary_start_paragraph is not None and boundary_end_paragraph is not None:
                structural_range = _assess_structural_paragraph_range(
                    section_root_map,
                    boundary_start_paragraph.paragraph_id,
                    boundary_end_paragraph.paragraph_id,
                )
                if has_table_patches:
                    if int(structural_range.get('table_count', 0) or 0) <= 0:
                        raise TemplateEngineError(
                            f"section {section.section_key!r} authored table patches, but resolved target {resolution.resolved_target_id!r} does not contain a table"
                        )
                    effective_table_patches = _resolve_effective_table_patches(
                        direct_patches=section.table_patches,
                        row_patches=section.table_row_patches,
                        record_patches=section.table_record_patches,
                        section_root_map=section_root_map,
                        structural_range=structural_range,
                        record_key_column=section.table_record_key_column,
                    )
                    candidates = _build_native_runtime_candidates_for_structural_range(
                        template_map=template_map,
                        section_root_map=section_root_map,
                        section_key=section.section_key,
                        resolved_target_id=resolution.resolved_target_id,
                        structural_range=structural_range,
                        table_patches=effective_table_patches,
                        table_entry_find=section.table_entry_find,
                        table_entry_cell_addr=section.table_entry_cell_addr,
                        table_entry_cursor_pos=section.table_entry_cursor_pos,
                        table_record_key_column=section.table_record_key_column,
                    )
                    first_candidate = candidates[0] if candidates else None
                    edit_fixture = first_candidate.get('edit_fixture') if isinstance(first_candidate, dict) else None
                    table_patch_fixture = first_candidate.get('table_patch_fixture') if isinstance(first_candidate, dict) else None
                    effective_table_patch_count = len(effective_table_patches)
                    candidate_operations = None
                    if effective_table_patch_count == 1 and edit_fixture:
                        candidate_operations = list(edit_fixture.get('operations') or [])
                    elif table_patch_fixture:
                        candidate_operations = list(table_patch_fixture.get('operations') or [])

                    shared_table_target_key = first_candidate.get('shared_table_target_key') if isinstance(first_candidate, dict) else None
                    if shared_table_target_key and candidate_operations:
                        # Ownership is enforced per section/op bundle. This still allows the
                        # normal probe+action pair inside one section, but blocks a second
                        # section from claiming the same canonical table cell.
                        bundle_id = _build_shared_table_bundle_id(
                            section_key=section.section_key,
                            candidate_identity_key=first_candidate.get('candidate_identity_key') if isinstance(first_candidate, dict) else None,
                            action_identity_key=first_candidate.get('action_identity_key') if isinstance(first_candidate, dict) else None,
                            probe_identity_key=first_candidate.get('probe_identity_key') if isinstance(first_candidate, dict) else None,
                            resolved_target_id=resolution.resolved_target_id,
                            fallback=section.section_key,
                        )
                        current_claim = {
                            'bundle_id': bundle_id,
                            'section_key': section.section_key,
                            'resolved_target_id': resolution.resolved_target_id,
                            'payload_signature': _stable_shared_table_bundle_signature(candidate_operations),
                            'preview': _summarize_shared_table_bundle_operations(candidate_operations),
                        }
                        owner_claim = used_shared_table_targets.get(shared_table_target_key)
                        if owner_claim is None:
                            used_shared_table_targets[shared_table_target_key] = current_claim
                        elif owner_claim.get('bundle_id') != bundle_id:
                            skipped_shared_table_patch_section_count += 1
                            conflict_group = shared_table_conflict_groups_map.setdefault(
                                shared_table_target_key,
                                {
                                    'shared_table_target_key': shared_table_target_key,
                                    'claims': {},
                                },
                            )
                            conflict_group['claims'][owner_claim['bundle_id']] = dict(owner_claim)
                            conflict_group['claims'][current_claim['bundle_id']] = dict(current_claim)
                            distinct_payload = owner_claim.get('payload_signature') != current_claim.get('payload_signature')
                            warnings.append(
                                f"section {section.section_key!r} was skipped because canonical shared-table target {shared_table_target_key!r} is already owned by {owner_claim.get('section_key')!r}"
                            )
                            warning_badges.append(
                                _build_warning_badge(
                                    code='shared_table_cell_conflict',
                                    severity='error',
                                    stage='compile',
                                    blocking=True,
                                    section_key=section.section_key,
                                    resolved_target_id=resolution.resolved_target_id,
                                    summary='Multiple sections claim the same canonical shared-table cell.',
                                    detail=(
                                        f"shared_table_target_key={shared_table_target_key!r}; owner_section={owner_claim.get('section_key')!r}; "
                                        f"contender_section={section.section_key!r}; distinct_payload={distinct_payload}."
                                    ),
                                )
                            )
                            continue

                    native_runtime_candidates.extend(candidates)
                    if effective_table_patch_count == 1 and edit_fixture:
                        replace_payload = edit_fixture['operations']
                        operations.extend(replace_payload)
                        instruction_metadata = _merge_instruction_proof_metadata(
                            instruction_metadata,
                            edit_fixture.get('metadata') if isinstance(edit_fixture, dict) else None,
                        )
                        if not section.table_entry_find and isinstance(edit_fixture.get('metadata'), dict) and edit_fixture['metadata'].get('preferred_entry_find_text'):
                            warnings.append(
                                f"section {section.section_key!r} auto-selected table entry anchor {edit_fixture['metadata'].get('preferred_entry_find_text')!r} for single-cell table replace"
                            )
                        continue
                    if not table_patch_fixture:
                        requires_structural_target_rework = True
                        unsafe_structural_ranges.append(
                            {
                                'section_key': section.section_key,
                                'resolved_target_id': resolution.resolved_target_id,
                                **structural_range,
                            }
                        )
                        warnings.append(
                            f'section {section.section_key!r} authored table patches, but compiler could not derive a stable table entry anchor for '
                            f'{resolution.resolved_target_id!r}; resend with @table_entry_find or use a narrower target'
                        )
                        warning_badges.append(
                            _build_warning_badge(
                                code='table_patch_entry_anchor_required',
                                severity='error',
                                stage='compile',
                                blocking=True,
                                section_key=section.section_key,
                                resolved_target_id=resolution.resolved_target_id,
                                summary='Table patch requires a stable entry anchor before apply.',
                                detail='Compiler could not derive preferred_entry_find_text. Resend with @table_entry_find and optionally @table_entry_cell.',
                            )
                        )
                        continue
                    replace_payload = table_patch_fixture['operations'][0]
                    if not section.table_entry_find and replace_payload.get('entry_find'):
                        warnings.append(
                            f"section {section.section_key!r} auto-selected table entry anchor {replace_payload.get('entry_find')!r} for batch cell patch"
                        )
                    operations.append(replace_payload)
                    continue
                using_structural_edit_fixture = False
                if structural_range['is_complex']:
                    candidates = _build_native_runtime_candidates_for_structural_range(
                        template_map=template_map,
                        section_root_map=section_root_map,
                        section_key=section.section_key,
                        resolved_target_id=resolution.resolved_target_id,
                        structural_range=structural_range,
                        replacement_text=rendered_text,
                        table_entry_find=section.table_entry_find,
                        table_entry_cell_addr=section.table_entry_cell_addr,
                        table_entry_cursor_pos=section.table_entry_cursor_pos,
                    )
                    native_runtime_candidates.extend(candidates)
                    first_candidate = candidates[0] if candidates else None
                    edit_fixture = first_candidate.get('edit_fixture') if isinstance(first_candidate, dict) else None
                    if edit_fixture:
                        replace_payload = edit_fixture['operations']
                        using_structural_edit_fixture = True
                        instruction_metadata = _merge_instruction_proof_metadata(
                            instruction_metadata,
                            edit_fixture.get('metadata') if isinstance(edit_fixture, dict) else None,
                        )
                        if not section.table_entry_find and isinstance(edit_fixture.get('metadata'), dict) and edit_fixture['metadata'].get('preferred_entry_find_text'):
                            warnings.append(
                                f"section {section.section_key!r} auto-selected table entry anchor {edit_fixture['metadata'].get('preferred_entry_find_text')!r} for structured single-cell table replace"
                            )
                    else:
                        requires_structural_target_rework = True
                        unsafe_structural_ranges.append(
                            {
                                'section_key': section.section_key,
                                'resolved_target_id': resolution.resolved_target_id,
                                **structural_range,
                            }
                        )
                        warnings.append(
                            f'section {section.section_key!r} targets a structurally complex paragraph range '
                            f'({boundary_start_paragraph.paragraph_id}..{boundary_end_paragraph.paragraph_id}); '
                            'plain paragraph-range replacement is blocked and placeholder/table-cell mode is required'
                        )
                        warning_badges.append(
                            _build_warning_badge(
                                code='structural_target_rework_required',
                                severity='error',
                                stage='compile',
                                blocking=True,
                                section_key=section.section_key,
                                resolved_target_id=resolution.resolved_target_id,
                                summary='Target range contains tables, pictures, controls, or page-break structure.',
                                detail=(
                                    f"paragraph_count={structural_range['paragraph_count']}, "
                                    f"tables={structural_range['table_count']}, "
                                    f"pictures={structural_range['picture_count']}, "
                                    f"controls={structural_range['control_count']}, "
                                    f"fields={structural_range['field_count']}, "
                                    f"page_breaks={structural_range['page_break_count']}. "
                                    'Use placeholders, table-cell fills, or a narrower plain-text target.'
                                ),
                            )
                        )
                        continue
                if not using_structural_edit_fixture:
                    if boundary_start_paragraph.paragraph_id == boundary_end_paragraph.paragraph_id:
                        target_mismatch = _build_exact_target_text_mismatch_warning(boundary_start_paragraph.text, rendered_text)
                        if target_mismatch:
                            exact_target_text = str(boundary_start_paragraph.text or '').strip()
                            anchor_heading_text = str(anchor.heading_text or '').strip() if anchor is not None else ''
                            anchor_body_text = str(anchor.body_anchor_text or '').strip() if anchor is not None else ''
                            heading_like_exact_target = (
                                boundary_start_paragraph.paragraph_class in {'heading', 'number'}
                                and bool(exact_target_text)
                                and exact_target_text in {anchor_heading_text, anchor_body_text}
                            )
                            warnings.append(
                                f"section {section.section_key!r} exact target {boundary_start_paragraph.paragraph_id!r} may be the wrong paragraph; "
                                f"target/rendered token overlap={target_mismatch['overlap_count']} shared_ratio={target_mismatch['shared_ratio']:.2f} "
                                f"rendered_coverage={target_mismatch['rendered_coverage']:.2f}"
                            )
                            if heading_like_exact_target:
                                scope_preview.warning_state = 'blocking'
                                if 'exact_target_heading_like' not in scope_preview.blocking_warning_codes:
                                    scope_preview.blocking_warning_codes.append('exact_target_heading_like')
                                scope_preview.why_not_body_safe = (
                                    f"Resolved target {resolution.resolved_target_id!r} still points at heading paragraph "
                                    f"{boundary_start_paragraph.paragraph_id!r}. Apply would anchor on heading-like text before runtime."
                                )
                                requires_exact_target_confirmation = True
                                if not any(
                                    badge.code == 'exact_target_heading_like'
                                    and badge.section_key == section.section_key
                                    and badge.resolved_target_id == resolution.resolved_target_id
                                    for badge in warning_badges
                                ):
                                    warning_badges.append(
                                        _build_warning_badge(
                                            code='exact_target_heading_like',
                                            severity='error',
                                            stage='compile',
                                            blocking=True,
                                            section_key=section.section_key,
                                            resolved_target_id=resolution.resolved_target_id,
                                            summary='Exact target looks like a heading or anchor paragraph, not a body paragraph.',
                                            detail=(
                                                f"target_preview={target_mismatch['target_preview']!r}; "
                                                f"rendered_preview={target_mismatch['rendered_preview']!r}; "
                                                f"token_overlap={target_mismatch['overlap_count']}; "
                                                f"shared_ratio={target_mismatch['shared_ratio']:.2f}; "
                                                f"rendered_coverage={target_mismatch['rendered_coverage']:.2f}; "
                                                f"short_target_long_render={target_mismatch['short_target_long_render']}; "
                                                'choose a different resolved_target_id or switch to a table-aware path before apply'
                                            ),
                                        )
                                    )
                            else:
                                warning_badges.append(
                                    _build_warning_badge(
                                        code='exact_target_text_mismatch',
                                        severity='warning',
                                        stage='compile',
                                        blocking=False,
                                        section_key=section.section_key,
                                        resolved_target_id=resolution.resolved_target_id,
                                        summary='Exact target text looks materially different from the authored replacement.',
                                        detail=(
                                            f"target_preview={target_mismatch['target_preview']!r}; "
                                            f"rendered_preview={target_mismatch['rendered_preview']!r}; "
                                            f"token_overlap={target_mismatch['overlap_count']}; "
                                            f"shared_ratio={target_mismatch['shared_ratio']:.2f}; "
                                            f"rendered_coverage={target_mismatch['rendered_coverage']:.2f}; "
                                            f"bullet_prefix_mismatch={target_mismatch['bullet_prefix_mismatch']}; "
                                            f"short_target_long_render={target_mismatch['short_target_long_render']}"
                                        ),
                                    )
                                )
                    if boundary_start_paragraph.paragraph_id != boundary_end_paragraph.paragraph_id:
                        replace_payload = {
                            'op': 'paragraph_range_replace_native',
                            'start_find': boundary_start_paragraph.text,
                            'end_find': boundary_end_paragraph.text,
                            'replace': rendered_text,
                            'apply': 'first',
                            'match_case': True,
                            'whole_word': False,
                            'allow_packed_paragraph': True,
                        }
                    else:
                        replace_payload = {
                            'op': 'paragraph_replace_native',
                            'find': boundary_start_paragraph.text,
                            'replace': rendered_text_for_replace,
                            'expected_present_after': rendered_text,
                            'expected_absent_after': boundary_start_paragraph.text,
                            'apply': 'first',
                            'match_case': True,
                            'whole_word': False,
                            'allow_packed_paragraph': True,
                        }
            else:
                if has_table_patches:
                    raise TemplateEngineError(
                        f"section {section.section_key!r} authored table patches, but resolved target {resolution.resolved_target_id!r} has no non-empty boundary paragraphs"
                    )
                anchor_find = anchor.heading_text or anchor.body_anchor_text or target_paragraph.text
                replace_payload = {
                    'op': 'insert_after_text',
                    'find': anchor_find,
                    'insert': '\r\n' + rendered_text,
                    'apply': 'first',
                    'match_case': True,
                    'whole_word': False,
                    'allow_packed_paragraph': True,
                }
                warnings.append(
                    f'section {section.section_key!r} resolved to an empty target body range; compiled as insert_after_text anchored to {anchor_find!r}'
                )
                warning_badges.append(
                    _build_warning_badge(
                        code='empty_target_body_range',
                        severity='warning',
                        stage='compile',
                        blocking=False,
                        section_key=section.section_key,
                        resolved_target_id=resolution.resolved_target_id,
                        summary='Target body range is empty; compile degraded to insert-after-heading.',
                        detail=f'anchor_find={anchor_find!r}',
                    )
                )

        section_style_catalog = style_catalog
        if not section.placeholder and anchor is not None:
            excluded_paragraph_ids = {target_paragraph.paragraph_id}
            if target_end_paragraph is not None:
                for para in template_map.paragraphs:
                    if para.section != target_paragraph.section:
                        continue
                    if target_paragraph.section_paragraph_index <= para.section_paragraph_index <= target_end_paragraph.section_paragraph_index:
                        excluded_paragraph_ids.add(para.paragraph_id)
            section_style_catalog = build_style_role_catalog(
                template_map,
                scope_section=anchor.section,
                scope_paragraph=target_paragraph,
                exclude_paragraph_ids=excluded_paragraph_ids,
            )

        body_role = section_style_catalog.get('body') or style_catalog.get('body')
        bullet_role = section_style_catalog.get('bullet') or style_catalog.get('bullet')
        number_role = section_style_catalog.get('number') or style_catalog.get('number')
        bullet_level_roles = {
            1: section_style_catalog.get('bullet_level_1') or style_catalog.get('bullet_level_1') or bullet_role,
            2: section_style_catalog.get('bullet_level_2') or style_catalog.get('bullet_level_2') or section_style_catalog.get('bullet_level_1') or style_catalog.get('bullet_level_1') or bullet_role,
            3: section_style_catalog.get('bullet_level_3') or style_catalog.get('bullet_level_3') or section_style_catalog.get('bullet_level_2') or style_catalog.get('bullet_level_2') or section_style_catalog.get('bullet_level_1') or style_catalog.get('bullet_level_1') or bullet_role,
        }
        number_level_roles = {
            1: section_style_catalog.get('number_level_1') or style_catalog.get('number_level_1') or number_role,
            2: section_style_catalog.get('number_level_2') or style_catalog.get('number_level_2') or section_style_catalog.get('number_level_1') or style_catalog.get('number_level_1') or number_role,
            3: section_style_catalog.get('number_level_3') or style_catalog.get('number_level_3') or section_style_catalog.get('number_level_2') or style_catalog.get('number_level_2') or section_style_catalog.get('number_level_1') or style_catalog.get('number_level_1') or number_role,
        }
        style_ops: list[dict[str, Any]] = []
        suppress_exemplar_clone_ops = False
        suppress_post_replace_style_ops = False
        exact_target_body_replace = False
        if isinstance(replace_payload, list):
            suppress_exemplar_clone_ops = any(str(item.get('op')) in {'replace_paragraph_range_safe', 'paragraph_range_replace_native'} for item in replace_payload if isinstance(item, dict))
            suppress_post_replace_style_ops = any(
                str(item.get('op')) in {'table_cell_replace_text', 'table_patch_cells'}
                for item in replace_payload
                if isinstance(item, dict)
            )
        else:
            suppress_exemplar_clone_ops = str(replace_payload.get('op')) in {'replace_paragraph_range_safe', 'paragraph_range_replace_native'}
            exact_target_body_replace = (
                not section.placeholder
                and bool(resolution.resolved_target_id)
                and str(replace_payload.get('op')) in {'replace_paragraph_safe', 'replace_paragraph_range_safe', 'paragraph_replace_native', 'paragraph_range_replace_native'}
            )
        if exact_target_body_replace:
            suppress_post_replace_style_ops = True
        if not suppress_post_replace_style_ops:
            for block_index, (block, block_lines) in enumerate(block_renderings):
                if block.type == 'paragraph':
                    previous_block = block_renderings[block_index - 1][0] if block_index > 0 else None
                    paragraph_exemplar = body_role
                    if block_lines:
                        first_compact = (block_lines[0] or '').strip()
                        if _looks_like_section_heading_text(first_compact):
                            paragraph_exemplar = body_role
                        elif first_compact.startswith(('■', '●', '○', '▪')):
                            paragraph_exemplar = bullet_level_roles.get(1) or bullet_role or body_role
                        elif first_compact.startswith(('-', '•', '*')):
                            paragraph_exemplar = bullet_level_roles.get(2) or bullet_level_roles.get(1) or bullet_role or body_role
                        elif NUMBERED_RE.match(first_compact):
                            paragraph_exemplar = number_level_roles.get(1) or number_role or body_role
                    if block_lines and paragraph_exemplar and not block.native_only and not suppress_exemplar_clone_ops:
                        style_ops.append(
                            {
                                'op': 'clone_paragraph_shape',
                                'find': block_lines[0],
                                'source_find': paragraph_exemplar.source_find,
                                'apply': 'first',
                                'match_case': True,
                                'whole_word': False,
                                'allow_zero_match': True,
                            }
                        )
                        style_ops.append(
                            {
                                'op': 'clone_text_style',
                                'find': block_lines[0],
                                'source_find': paragraph_exemplar.source_find,
                                'apply': 'first',
                                'match_case': True,
                                'whole_word': False,
                                'allow_zero_match': True,
                            }
                        )
                    style_text_op = _make_style_text_op(find=block_lines[0], text_style=block.text_style) if block_lines else None
                    if style_text_op:
                        style_ops.append(style_text_op)
                    if block_lines:
                        for inline_style in block.inline_styles:
                            inline_style_op = _make_style_text_in_paragraph_op(
                                paragraph_find=block_lines[0],
                                inline_style=inline_style,
                            )
                            if inline_style_op:
                                style_ops.append(inline_style_op)
                    paragraph_shape_op = _make_paragraph_shape_op(find=block_lines[0], native_style=block.native_style) if block_lines else None
                    if paragraph_shape_op:
                        style_ops.append(paragraph_shape_op)
                    if block_lines and previous_block and previous_block.type in {'bullet_list', 'numbered_list'}:
                        post_list_spacing_op = _make_post_list_paragraph_spacing_op(find=block_lines[0], native_style=block.native_style)
                        if post_list_spacing_op:
                            style_ops.append(post_list_spacing_op)
                elif block.type == 'bullet_list':
                    for index, item in enumerate(block_lines):
                        raw_level = block.item_depths[index] if index < len(block.item_depths) else 1
                        base_level = block.list_level or 1
                        level = max(1, base_level + raw_level - 1)
                        resolved_kind = block.native_list_kind or 'bullet'
                        exemplar = bullet_level_roles.get(min(level, 3)) or bullet_role
                        list_source_find = block.list_source_find or (exemplar.source_find if exemplar else None)
                        style_ops.append(
                            _make_native_list_op(
                                find=item,
                                kind=resolved_kind,
                                level=level,
                                source_find=list_source_find,
                            )
                        )
                        if exemplar and not block.native_only and not suppress_exemplar_clone_ops:
                            style_ops.append(
                            {
                                'op': 'clone_text_style',
                                'find': item,
                                'source_find': exemplar.source_find,
                                'apply': 'first',
                                'match_case': True,
                                'whole_word': False,
                                'allow_zero_match': True,
                            }
                        )
                        style_text_op = _make_style_text_op(find=item, text_style=block.text_style)
                        if style_text_op:
                            style_ops.append(style_text_op)
                        level_indent_op = _make_list_level_indent_op(find=item, level=level)
                        if level_indent_op:
                            style_ops.append(level_indent_op)
                        inline_styles = block.item_inline_styles[index] if index < len(block.item_inline_styles) else []
                        for inline_style in inline_styles:
                            inline_style_op = _make_style_text_in_paragraph_op(
                                paragraph_find=item,
                                inline_style=inline_style,
                            )
                            if inline_style_op:
                                style_ops.append(inline_style_op)
                        paragraph_shape_op = _make_paragraph_shape_op(find=item, native_style=block.native_style)
                        if paragraph_shape_op:
                            style_ops.append(paragraph_shape_op)
                elif block.type == 'numbered_list':
                    for index, item in enumerate(block_lines):
                        raw_level = block.item_depths[index] if index < len(block.item_depths) else 1
                        base_level = block.list_level or 1
                        level = max(1, base_level + raw_level - 1)
                        resolved_kind = block.native_list_kind or 'number'
                        exemplar = number_level_roles.get(min(level, 3)) or number_role
                        list_source_find = block.list_source_find or (exemplar.source_find if exemplar else None)
                        style_ops.append(
                            _make_native_list_op(
                                find=item,
                                kind=resolved_kind,
                                level=level,
                                source_find=list_source_find,
                            )
                        )
                        if exemplar and not block.native_only and not suppress_exemplar_clone_ops:
                            style_ops.append(
                            {
                                'op': 'clone_text_style',
                                'find': item,
                                'source_find': exemplar.source_find,
                                'apply': 'first',
                                'match_case': True,
                                'whole_word': False,
                                'allow_zero_match': True,
                            }
                        )
                        style_text_op = _make_style_text_op(find=item, text_style=block.text_style)
                        if style_text_op:
                            style_ops.append(style_text_op)
                        level_indent_op = _make_list_level_indent_op(find=item, level=level)
                        if level_indent_op:
                            style_ops.append(level_indent_op)
                        inline_styles = block.item_inline_styles[index] if index < len(block.item_inline_styles) else []
                        for inline_style in inline_styles:
                            inline_style_op = _make_style_text_in_paragraph_op(
                                paragraph_find=item,
                                inline_style=inline_style,
                            )
                            if inline_style_op:
                                style_ops.append(inline_style_op)
                        paragraph_shape_op = _make_paragraph_shape_op(find=item, native_style=block.native_style)
                        if paragraph_shape_op:
                            style_ops.append(paragraph_shape_op)

        if isinstance(replace_payload, list):
            operations.extend(replace_payload)
        else:
            operations.append(replace_payload)

        first_line = rendered_lines[0]
        if body_role and first_line and not section.placeholder:
            warnings.append(
                f'section {section.section_key!r} uses deterministic body paragraph inheritance, but list/body text-style edge cases may still drift'
            )
            warning_badges.append(
                _build_warning_badge(
                    code='style_drift',
                    section_key=section.section_key,
                    resolved_target_id=resolution.resolved_target_id if not section.placeholder else None,
                    summary='Body paragraph shape/text inheritance is applied, but full list and edge-case text-style inheritance is not fully guaranteed yet.',
                    detail='Review render diff for residual text-style drift. Body paragraph spacing should be more stable, while list hierarchy remains deterministic.',
                )
            )
        operations.extend(style_ops)

        if not section.placeholder and resolution.confidence < 0.75:
            warnings.append(
                f'section {section.section_key!r} resolved with moderate confidence {resolution.confidence:.2f} to {resolution.matched_heading!r}'
            )
            warning_badges.append(
                _build_warning_badge(
                    code='target_confidence_low',
                    section_key=section.section_key,
                    resolved_target_id=resolution.resolved_target_id,
                    summary='Resolved target confidence is lower than preferred.',
                    detail=f'confidence={resolution.confidence:.2f}, matched_heading={resolution.matched_heading!r}',
                )
            )

    cleanup_operations: list[dict[str, Any]] = []
    if cleanup_policy not in {'none', 'unused', 'all'}:
        raise TemplateEngineError(f'unsupported cleanup policy: {cleanup_policy!r}')

    if cleanup_policy != 'none':
        for placeholder in template_map.placeholders:
            if cleanup_policy == 'unused' and placeholder in used_placeholders:
                continue
            cleanup_operations.append(
                {
                    'op': 'replace_all',
                    'find': placeholder,
                    'replace': '',
                    'apply': 'all',
                }
            )
        operations.extend(cleanup_operations)
        if cleanup_operations:
            warnings.append(
                f'cleanup policy {cleanup_policy!r} scheduled removal of {len(cleanup_operations)} placeholder token(s)'
            )

    normalized_validation = normalize_validation(validation) if validation is not None else {}
    if operations:
        normalized_operations = normalize_instruction_payload({'operations': operations, 'validation': normalized_validation})['operations']
    else:
        normalized_operations = []
    unresolved_targets = _build_unresolved_targets(target_recommendations)
    touched_ranges = _build_touched_ranges(apply_scope_previews)
    native_action_used = _summarize_native_action_usage(normalized_operations)
    native_action_count = sum(item['count'] for item in native_action_used)
    execution_mode = _infer_execution_mode(operations=normalized_operations, unresolved_targets=unresolved_targets)
    fallback_reason = _infer_fallback_reasons(operations=normalized_operations, unresolved_targets=unresolved_targets)
    shared_table_conflict_groups: list[dict[str, Any]] = []
    for shared_table_target_key in sorted(shared_table_conflict_groups_map):
        claims = list(shared_table_conflict_groups_map[shared_table_target_key].get('claims', {}).values())
        if len(claims) <= 1:
            continue
        shared_table_conflict_groups.append(
            {
                'shared_table_target_key': shared_table_target_key,
                'section_keys': sorted({item.get('section_key') for item in claims if item.get('section_key')}),
                'bundle_ids': [item.get('bundle_id') for item in claims],
                'resolved_target_ids': sorted({item.get('resolved_target_id') for item in claims if item.get('resolved_target_id')}),
                'claim_count': len(claims),
                'payload_signatures': sorted({item.get('payload_signature') for item in claims if item.get('payload_signature')}),
                'summary': (
                    f"canonical shared-table key {shared_table_target_key} is claimed by "
                    f"{', '.join(item.get('section_key') for item in claims if item.get('section_key'))}"
                ),
            }
        )
    emitted_shared_table_scan = _scan_emitted_shared_table_identities(normalized_operations)
    emitted_shared_table_keys = list(emitted_shared_table_scan.get('shared_table_target_keys') or [])
    emitted_shared_table_collision_groups = list(emitted_shared_table_scan.get('collision_groups') or [])
    shared_table_conflict_detected = bool(shared_table_conflict_groups or emitted_shared_table_collision_groups)
    compiled_table_patch_count = sum(1 for op in normalized_operations if str(op.get('op')) == 'table_patch_cells')
    compiled_table_edit_count = sum(
        1
        for op in normalized_operations
        if str(op.get('op')) in {'table_patch_cells', 'table_cell_replace_text'}
    )
    expected_table_patch_section_count = max(0, authored_table_patch_section_count - skipped_shared_table_patch_section_count)

    if authored_table_patch_present and not shared_table_conflict_detected and (
        compiled_table_edit_count <= 0
        or compiled_table_edit_count < expected_table_patch_section_count
    ):
        requires_structural_target_rework = True
        if 'table_patch_native_runtime_unavailable' not in fallback_reason:
            fallback_reason.append('table_patch_native_runtime_unavailable')
        warning_badges.append(
            _build_warning_badge(
                code='table_patch_native_runtime_required',
                severity='error',
                stage='compile',
                blocking=True,
                summary='Table patch must compile to native table actions before apply.',
                detail=(
                    'Compiler did not produce enough native table edit actions for authored table patch sections, '
                    'so apply is blocked fail-closed. '
                    f'authored_table_patch_sections={authored_table_patch_section_count}, '
                    f'expected_table_patch_sections={expected_table_patch_section_count}, '
                    f'skipped_shared_table_patch_sections={skipped_shared_table_patch_section_count}, '
                    f'compiled_table_patch_ops={compiled_table_patch_count}, '
                    f'compiled_table_edit_ops={compiled_table_edit_count}, '
                    f'native_action_count={native_action_count}.'
                ),
            )
        )

    if shared_table_conflict_detected and 'shared_table_cell_conflict' not in fallback_reason:
        fallback_reason.append('shared_table_cell_conflict')

    failure_reason = (
        'exact_target_confirmation_required'
        if requires_exact_target_confirmation
        else (
            'shared_table_cell_conflict'
            if shared_table_conflict_detected
            else ('structural_target_rework_required' if requires_structural_target_rework else None)
        )
    )

    compile_status = (
        'needs_exact_target_confirmation'
        if requires_exact_target_confirmation
        else (
            'blocked_shared_table_cell_conflict'
            if shared_table_conflict_detected
            else ('blocked_structural_target' if requires_structural_target_rework else 'ready')
        )
    )
    next_action = (
        'choose_or_confirm_resolved_target_id_and_resend'
        if requires_exact_target_confirmation
        else (
            'resolve_shared_table_cell_conflict_and_resend'
            if shared_table_conflict_detected
            else ('use_placeholders_or_table_cell_mode' if requires_structural_target_rework else 'apply_safe')
        )
    )
    search_first_candidate_reviews = _build_search_first_candidate_reviews(target_recommendations)
    search_first_runtime_flow = _build_search_first_runtime_flow(
        candidate_reviews=search_first_candidate_reviews,
        native_runtime_candidates=native_runtime_candidates,
    )
    compile_readiness = {
        **compile_readiness,
        'status': compile_status,
        'requires_exact_target_confirmation': requires_exact_target_confirmation,
        'requires_structural_target_rework': requires_structural_target_rework,
        'unsafe_structural_ranges': unsafe_structural_ranges,
        'native_runtime_candidates': native_runtime_candidates,
        'next_action': next_action,
        'failure_reason': failure_reason,
        'shared_table_conflict_groups': shared_table_conflict_groups,
        'emitted_shared_table_keys': emitted_shared_table_keys,
        'emitted_shared_table_collision_groups': emitted_shared_table_collision_groups,
        'resend_field': 'resolved_target_id' if requires_exact_target_confirmation else None,
        'search_first_candidate_reviews': search_first_candidate_reviews,
        'search_first_runtime_flow': search_first_runtime_flow,
    }
    if search_first_candidate_reviews:
        instruction_metadata['search_first_candidate_reviews'] = search_first_candidate_reviews
    if search_first_runtime_flow:
        instruction_metadata['search_first_runtime_flow'] = search_first_runtime_flow
        explicit_modes = list(instruction_metadata.get('verification_modes') or [])
        for mode in ('image', 'text-broader'):
            if mode not in explicit_modes:
                explicit_modes.append(mode)
        instruction_metadata['verification_modes'] = explicit_modes
    compiled_section_scope = _normalize_section_key_list(
        [
            *[section.section_key for section in content_spec.sections],
            *[item.section_key for item in apply_scope_previews],
            *[item.section_key for item in resolutions],
            *[item.section_key for item in target_recommendations],
        ]
    )
    _raise_if_section_scope_widened(
        stage='compiled_plan',
        observed_section_keys=compiled_section_scope,
        allowed_section_keys=normalized_allowed_section_keys,
    )
    confirm_policy, approval_packet = _build_confirm_policy(
        template_version=template_map.template_version,
        operations=normalized_operations,
        execution_mode=execution_mode,
        unresolved_targets=unresolved_targets,
        target_recommendations=target_recommendations,
        fallback_reason=fallback_reason,
        failure_reason=failure_reason,
        apply_scope_previews=apply_scope_previews,
        touched_ranges=touched_ranges,
        warning_badges=warning_badges,
        compile_readiness=compile_readiness,
        policy_override=normalized_policy_override,
    )
    _raise_if_section_scope_widened(
        stage='approval_packet',
        observed_section_keys=((approval_packet.get('target_summary') or {}).get('section_keys') or []),
        allowed_section_keys=normalized_allowed_section_keys,
    )

    payload = EditPlan(
        operations=normalized_operations,
        validation=normalized_validation,
        instruction_metadata=instruction_metadata,
        compile_readiness={
            **compile_readiness,
            'confirm_policy': confirm_policy,
            'approval_packet': approval_packet,
            'target_conflict_groups': confirm_policy.get('target_conflict_groups', []),
            'interactive_choices': confirm_policy.get('interactive_choices', []),
        },
        warnings=warnings,
        resolutions=resolutions,
        target_recommendations=target_recommendations,
        placeholder_resolutions=placeholder_resolutions,
        cleanup_operations=cleanup_operations,
        apply_scope_previews=apply_scope_previews,
        warning_badges=warning_badges,
        precondition_ok=True,
        execution_mode=execution_mode,
        native_action_count=native_action_count,
        native_action_used=native_action_used,
        fallback_reason=fallback_reason,
        failure_reason=failure_reason,
        touched_ranges=touched_ranges,
        unresolved_targets=unresolved_targets,
        confirm_policy=confirm_policy,
        approval_packet=approval_packet,
        policy_override=normalized_policy_override,
    )
    return template_map, style_catalog, content_spec, payload


def compile_authoring_payload(
    input_path: str | Path,
    markdown: str,
    *,
    validation: dict[str, Any] | None = None,
    cleanup_policy: str = 'none',
    policy_override: dict[str, Any] | None = None,
    allowed_section_keys: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    template_map, style_catalog, content_spec, plan = compile_markdown_to_edit_plan(
        input_path,
        markdown,
        validation=validation,
        cleanup_policy=cleanup_policy,
        policy_override=policy_override,
        allowed_section_keys=allowed_section_keys,
    )
    return {
        'schema_version': 'compile-authoring/v1',
        'template_version': template_map.template_version,
        'inspect_snapshot_id': template_map.inspect_snapshot_id,
        'template_fingerprint': template_map.template_fingerprint,
        'template_map': template_map.model_dump(),
        'style_roles': style_catalog.model_dump(),
        'content_spec': content_spec.model_dump(),
        'compile_readiness': plan.compile_readiness,
        'precondition_ok': plan.precondition_ok,
        'execution_mode': plan.execution_mode,
        'native_action_count': plan.native_action_count,
        'native_action_used': plan.native_action_used,
        'fallback_reason': plan.fallback_reason,
        'failure_reason': plan.failure_reason,
        'touched_ranges': plan.touched_ranges,
        'unresolved_targets': plan.unresolved_targets,
        'confirm_policy': plan.confirm_policy,
        'approval_packet': plan.approval_packet,
        'interactive_choices': plan.confirm_policy.get('interactive_choices', []),
        'policy_override': plan.policy_override,
        'edit_plan': {
            'operations': plan.operations,
            'validation': plan.validation,
            'instruction_metadata': plan.instruction_metadata,
            'precondition_ok': plan.precondition_ok,
            'execution_mode': plan.execution_mode,
            'native_action_count': plan.native_action_count,
            'native_action_used': plan.native_action_used,
            'fallback_reason': plan.fallback_reason,
            'failure_reason': plan.failure_reason,
            'touched_ranges': plan.touched_ranges,
            'unresolved_targets': plan.unresolved_targets,
            'confirm_policy': plan.confirm_policy,
            'approval_packet': plan.approval_packet,
            'policy_override': plan.policy_override,
        },
        'warnings': plan.warnings,
        'warning_badges': [item.model_dump() for item in plan.warning_badges],
        'resolutions': [item.model_dump() for item in plan.resolutions],
        'target_recommendations': [item.model_dump() for item in plan.target_recommendations],
        'apply_scope_previews': [item.model_dump() for item in plan.apply_scope_previews],
        'placeholder_resolutions': [item.model_dump() for item in plan.placeholder_resolutions],
        'cleanup_operations': plan.cleanup_operations,
        'placeholder_naming': placeholder_naming_rules(),
    }


def compile_placeholder_fill_payload(
    input_path: str | Path,
    placeholder_fills_json: str,
    *,
    validation: dict[str, Any] | None = None,
    cleanup_policy: str = 'none',
) -> dict[str, Any]:
    template_map = build_template_map(input_path)
    fill_spec = parse_placeholder_fill_json(placeholder_fills_json)
    placeholder_resolutions: list[PlaceholderResolution] = []
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    used_placeholders: set[str] = set()

    for item in fill_spec.items:
        placeholder_resolutions.append(
            resolve_placeholder(
                template_map,
                item.placeholder,
                section_key=item.input_key,
                raw_input=item.input_key,
            )
        )
        used_placeholders.add(item.placeholder)
        operations.append(
            {
                'op': 'replace_text_safe',
                'find': item.placeholder,
                'replace': item.value,
                'apply': 'first',
                'match_case': True,
                'whole_word': False,
                'allow_packed_paragraph': True,
            }
        )

    cleanup_operations: list[dict[str, Any]] = []
    if cleanup_policy not in {'none', 'unused', 'all'}:
        raise TemplateEngineError(f'unsupported cleanup policy: {cleanup_policy!r}')
    if cleanup_policy != 'none':
        for placeholder in template_map.placeholders:
            if cleanup_policy == 'unused' and placeholder in used_placeholders:
                continue
            cleanup_operations.append(
                {
                    'op': 'replace_all',
                    'find': placeholder,
                    'replace': '',
                    'apply': 'all',
                }
            )
        operations.extend(cleanup_operations)
        if cleanup_operations:
            warnings.append(
                f'cleanup policy {cleanup_policy!r} scheduled removal of {len(cleanup_operations)} placeholder token(s)'
            )

    payload = normalize_instruction_payload({'operations': operations, 'validation': normalize_validation(validation) if validation is not None else {}})
    return {
        'template_map': template_map.model_dump(),
        'placeholder_naming': placeholder_naming_rules(),
        'placeholder_fill_spec': fill_spec.model_dump(),
        'edit_plan': payload,
        'warnings': warnings,
        'warning_badges': [],
        'apply_scope_previews': [],
        'placeholder_resolutions': [item.model_dump() for item in placeholder_resolutions],
        'cleanup_operations': cleanup_operations,
    }


def build_clear_placeholders_payload(
    input_path: str | Path,
    *,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template_map = build_template_map(input_path)
    operations = [
        {
            'op': 'replace_all',
            'find': placeholder,
            'replace': '',
            'apply': 'all',
        }
        for placeholder in template_map.placeholders
    ]
    payload = normalize_instruction_payload({'operations': operations, 'validation': normalize_validation(validation) if validation is not None else {}})
    return {
        'template_map': template_map.model_dump(),
        'placeholder_naming': placeholder_naming_rules(),
        'edit_plan': payload,
        'placeholder_count': len(template_map.placeholders),
    }


def parse_validation_json(text: str | None) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditOperationError(f'invalid validation_json: {exc}') from exc
    return normalize_validation(raw)


def parse_cleanup_placeholders(value: str | None) -> str:
    if value is None:
        return 'none'
    normalized = str(value).strip().lower()
    if normalized in {'', '0', 'false', 'no', 'off', 'none'}:
        return 'none'
    if normalized in {'1', 'true', 'yes', 'on', 'unused'}:
        return 'unused'
    if normalized == 'all':
        return 'all'
    raise EditOperationError(f'unsupported cleanup_placeholders value: {value!r}. Use none, unused, or all')
