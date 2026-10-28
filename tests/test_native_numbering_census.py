"""Behavioral regressions for the HWPML2X protection census reader and its wiring.

The HWPML2X samples are SYNTHETIC: element and attribute names follow the public HWPML 2.x shape as best
understood, were not produced by Hancom, and are not native evidence. The reader must stay tolerant and
fail closed; anything it cannot resolve is reported as unproven (None) rather than guessed.
"""
from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from typing import Any

from app import native_numbering as nn
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import FakeCtrl, FakeHwp

HEADINGS = {0: 'None', 1: 'Outline', 2: 'Number', 3: 'Bullet'}


def hwpml(
    paragraphs: list[tuple[str, int, int]],
    *,
    headers: tuple[str, ...] = (),
    footers: tuple[str, ...] = (),
    bindata: str = 'AAA',
    style: str = 'Normal',
    numbering: str = '^1.',
    sections: int = 1,
    char_height: int = 1000,
    margins: dict[str, int] | None = None,
    picture_width: int | None = None,
    table_width: int | None = None,
    pagenum: bool = False,
    outline_heads: tuple[str, ...] | None = None,
    tail_head: str = '(^5)',
    extra_numberings: tuple[str, ...] = (),
    extra_styles: tuple[str, ...] = (),
    extra_shapes: tuple[tuple[int, int, int], ...] = (),
    bad_charshape_ref: bool = False,
) -> str:
    """SYNTHETIC HWPML2X: element/attribute names are not verified against Hancom output."""
    margins = margins or {}
    keyed = [(heading, level, margins.get(text, -1)) for text, heading, level in paragraphs]
    pairs = sorted(set(keyed) | {(0, 0, -1)} | set(extra_shapes))
    shape_ids = {pair: index for index, pair in enumerate(pairs)}
    shapes = ''.join(
        f'<PARASHAPE Id="{index}" HeadingType="{HEADINGS[heading]}" Level="{level}"'
        + (f' LeftMargin="{margin}"' if margin >= 0 else '')
        + '/>'
        for (heading, level, margin), index in shape_ids.items()
    )
    charshape_ref = '99' if bad_charshape_ref else '1'
    body = []
    for index, (text, heading, level) in enumerate(paragraphs):
        controls = ''
        if index == 0:
            controls = '<SECDEF OutlineShape="1"/>'
            controls += ''.join(
                f'<{tag} ApplyPageType="Both"><PARALIST><P ParaShape="0"><TEXT><CHAR>{content}</CHAR></TEXT></P></PARALIST></{tag}>'
                for tag, group in (('HEADER', headers), ('FOOTER', footers))
                for content in group
            )
            if picture_width is not None:
                controls += f'<PICTURE BinItem="1"><SIZE Width="{picture_width}" Height="300"/></PICTURE>'
            if table_width is not None:
                controls += (
                    f'<TABLE><SIZE Width="{table_width}" Height="300"/><ROW><CELL ColAddr="0" RowAddr="0"><PARALIST>'
                    f'<P ParaShape="0"><TEXT CharShape="{charshape_ref}"><CHAR>cell</CHAR></TEXT></P></PARALIST></CELL></ROW></TABLE>'
                )
            if pagenum:
                controls += '<PAGENUM Pos="BottomCenter" FormatType="Digit" SideChar="0"/>'
        body.append(
            f'<P ParaShape="{shape_ids[keyed[index]]}" Style="0"><TEXT CharShape="{charshape_ref}">{controls}<CHAR>{text}</CHAR></TEXT></P>'
        )
    section_xml = ''.join(f'<SECTION Id="{n}">{"".join(body) if n == 0 else ""}</SECTION>' for n in range(sections))
    heads = outline_heads if outline_heads is not None else (numbering,)
    parahead = ''.join(f'<PARAHEAD Level="{n}">{text}</PARAHEAD>' for n, text in enumerate(heads, start=1))
    parahead += f'<PARAHEAD Level="5">{tail_head}</PARAHEAD>'
    extra_numbering_xml = ''.join(
        f'<NUMBERING Id="{n}"><PARAHEAD Level="1">{text}</PARAHEAD></NUMBERING>' for n, text in enumerate(extra_numberings, start=2)
    )
    extra_style_xml = ''.join(f'<STYLE Id="{n}" Name="{name}" ParaShape="0" CharShape="1"/>' for n, name in enumerate(extra_styles, start=1))
    return (
        '<?xml version="1.0" encoding="UTF-8"?><HWPML Version="2.8"><HEAD SecCnt="1"><MAPPINGTABLE>'
        f'<BINDATALIST Count="1"><BINDATA Id="1" Type="Embedding" Format="png">{bindata}</BINDATA></BINDATALIST>'
        f'<CHARSHAPELIST Count="1"><CHARSHAPE Id="1" Height="{char_height}"/></CHARSHAPELIST>'
        f'<NUMBERINGLIST Count="1"><NUMBERING Id="1" Name="{numbering}">{parahead}</NUMBERING>{extra_numbering_xml}</NUMBERINGLIST>'
        f'<PARASHAPELIST Count="1">{shapes}</PARASHAPELIST>'
        f'<STYLELIST Count="1"><STYLE Id="0" Name="{style}" ParaShape="0" CharShape="1"/>{extra_style_xml}</STYLELIST>'
        f'</MAPPINGTABLE></HEAD><BODY>{section_xml}</BODY></HWPML>'
    )


def _census(text: str) -> dict[str, Any]:
    from app import native_numbering_census

    return native_numbering_census.parse_hwpml2x_census(text)


class ParserTests(unittest.TestCase):
    PARAS = [('Title', 0, 0), ('Item', 1, 1), ('Numbered', 2, 0)]

    def test_paragraphs_headings_levels_and_sections_are_read(self) -> None:
        census = _census(hwpml(self.PARAS))
        self.assertEqual(
            [(p['text'], p['heading'], p['level'], p['section']) for p in census['paragraphs']],
            [('Title', 'NONE', 0, 0), ('Item', 'OUTLINE', 1, 0), ('Numbered', 'NUMBER', 0, 0)],
        )
        self.assertEqual(census['paragraphs'][1]['text_sha256'], hashlib.sha256(b'Item').hexdigest())

    def test_header_and_footer_content_is_fingerprinted_separately_from_body_paragraphs(self) -> None:
        census = _census(hwpml(self.PARAS, headers=('H1',), footers=('F1',)))
        self.assertEqual(len(census['header_footer']), 2)
        self.assertEqual([p['text'] for p in census['paragraphs']], ['Title', 'Item', 'Numbered'])

    def test_header_footer_digest_changes_when_content_changes_but_body_digest_does_not(self) -> None:
        left = _census(hwpml(self.PARAS, headers=('H1',)))
        right = _census(hwpml(self.PARAS, headers=('ALTERED',)))
        self.assertNotEqual(left['header_footer'], right['header_footer'])
        self.assertEqual(left['body_sha256'], right['body_sha256'])

    def test_each_protected_area_has_its_own_digest(self) -> None:
        base = _census(hwpml(self.PARAS))
        self.assertNotEqual(base['bindata_sha256'], _census(hwpml(self.PARAS, bindata='BBB'))['bindata_sha256'])
        self.assertNotEqual(base['styles_sha256'], _census(hwpml(self.PARAS, style='Other'))['styles_sha256'])
        self.assertNotEqual(base['numberings_sha256'], _census(hwpml(self.PARAS, numbering='X'))['numberings_sha256'])
        self.assertNotEqual(base['body_sha256'], _census(hwpml([('Title', 0, 0), ('Changed', 1, 1), ('Numbered', 2, 0)]))['body_sha256'])

    def test_multiple_sections_are_numbered(self) -> None:
        census = _census(hwpml(self.PARAS, sections=2))
        self.assertEqual({p['section'] for p in census['paragraphs']}, {0})

    def test_names_are_matched_case_insensitively(self) -> None:
        lowered = hwpml(self.PARAS).replace('<P ', '<p ').replace('</P>', '</p>').replace('<CHAR>', '<char>').replace('</CHAR>', '</char>')
        self.assertEqual([p['text'] for p in _census(lowered)['paragraphs']], ['Title', 'Item', 'Numbered'])

    def test_unresolvable_paragraph_heading_makes_the_paragraph_list_unproven_not_guessed(self) -> None:
        broken = hwpml(self.PARAS).replace('HeadingType="Outline"', 'HeadingType="Weird"')
        census = _census(broken)
        self.assertIsNone(census['paragraphs'])
        self.assertIsNotNone(census['header_footer'])

    def test_paragraph_without_a_resolvable_shape_makes_the_list_unproven(self) -> None:
        census = _census(hwpml(self.PARAS).replace('ParaShape="1"', 'ParaShape="99"', 1))
        self.assertIsNone(census['paragraphs'])

    def test_unrecognized_style_numbering_and_bindata_lists_are_unproven_not_empty_digests(self) -> None:
        text = hwpml(self.PARAS)
        for tag, field in (('STYLELIST', 'styles_sha256'), ('NUMBERINGLIST', 'numberings_sha256'), ('BINDATALIST', 'bindata_sha256')):
            renamed = text.replace(tag, 'X' + tag)
            self.assertIsNone(_census(renamed)[field], msg=tag)

    def test_page_number_count_is_not_claimed_from_unverified_element_names(self) -> None:
        self.assertIsNone(_census(hwpml(self.PARAS))['page_number_count'])

    def test_the_census_is_accepted_by_the_numbering_validator(self) -> None:
        validated = nn._validated_census(_census(hwpml(self.PARAS, headers=('H',))))  # noqa: SLF001
        self.assertEqual(len(validated['paragraphs']), 3)

    def test_malformed_missing_body_dtd_and_oversized_input_fail_closed(self) -> None:
        from app import native_numbering_census as module

        for label, text in (
            ('malformed', '<HWPML><BODY>'),
            ('no-body', '<HWPML><HEAD/></HWPML>'),
            ('empty', ''),
            ('dtd', '<!DOCTYPE x [<!ENTITY a "b">]><HWPML><BODY/></HWPML>'),
            ('not-text', None),
            ('oversized', '<HWPML><BODY>' + ' ' * (module.MAX_HWPML_CHARS + 1) + '</BODY></HWPML>'),
        ):
            with self.assertRaises(module.CensusError, msg=label):
                module.parse_hwpml2x_census(text)


class _HwpmlHwp(FakeHwp):
    """FakeHwp whose in-memory HWPML2X text mirrors its paragraph, header and style state."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.saved = False
        self.hwpml_calls = 0
        self.hwpml_text: Any = None
        self.hwpml_extras: dict[str, Any] = {}
        self.seen_shapes: set[tuple[int, int, int]] = set()

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        if fmt != 'HWPML2X':
            return super().get_text_file(fmt, option)
        self.hwpml_calls += 1
        if self.hwpml_text is not None:
            return self.hwpml_text
        extras = dict(self.hwpml_extras)
        margins = extras.get('margins') or {}
        self.seen_shapes |= {(p['heading_type'], p['level'], margins.get(str(p['text']), -1)) for p in self.paragraphs}
        shape = self.outline_shape
        return hwpml(
            [(str(p['text']), p['heading_type'], p['level']) for p in self.paragraphs],
            headers=tuple(str(item) for item in self.package_state['header_footer']),
            style=str(self.package_state['styles']),
            numbering=str(self.package_state['numberings']),
            bindata=''.join(self.package_state['bindata']),
            outline_heads=tuple(str(shape[f'StrFormatLevel{i}']) for i in range(4)),
            tail_head=str(shape['StrFormatLevel4']),
            extra_shapes=tuple(sorted(self.seen_shapes)),
            **extras,
        )

    def save_as(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError('the protection reader must not rebind or save the live document')

    def save(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError('the protection reader must not save the live document')


class _Service(LocalCliBundleNumberingMixin):
    def _find_text_on_expected_page(self, hwp, *, match, expected_page, numbering_guard=False):
        for index, para in enumerate(hwp.paragraphs):
            if match in str(para['text']):
                hwp.cursor = (0, index, 0)
                return {'page': expected_page, 'pos': [0, index, 0]}
        raise LocalCliRuntimeError('target not found')


def _paras(*specs):
    return [{'text': t, 'heading_type': h, 'level': l, 'section': 0} for t, h, l in specs]


class ReaderTests(unittest.TestCase):
    def test_reader_reads_the_live_document_in_memory_without_saving(self) -> None:
        hwp = _HwpmlHwp(paragraphs=_paras(('Title', 0, 0), ('Item', 0, 0)))
        reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))  # noqa: SLF001
        self.assertEqual(hwp.hwpml_calls, 0)
        census = reader()
        self.assertEqual(hwp.hwpml_calls, 1)
        self.assertEqual([p['text'] for p in census['paragraphs']], ['Title', 'Item'])
        reader()
        self.assertEqual(hwp.hwpml_calls, 2)

    def test_reader_failures_propagate_so_callers_refuse_or_report_unknown(self) -> None:
        for label, value in (('malformed', '<HWPML><BODY>'), ('non-text', 42)):
            hwp = _HwpmlHwp(paragraphs=_paras(('Item', 0, 0)))
            hwp.hwpml_text = value
            reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))  # noqa: SLF001
            with self.assertRaises(Exception, msg=label):
                reader()
        bare = SimpleNamespace(hwp=SimpleNamespace())
        with self.assertRaises(Exception):
            _Service()._numbering_protection_reader(bare)()  # noqa: SLF001


class EndToEndThroughTheRealReaderTests(unittest.TestCase):
    STEP = {'position': 'bottom-center', 'start_number': 1, 'side_char': 'none', 'confirm_layout': True}
    NUMBERING = {'match': 'Item', 'expected_page': 1, 'level': 2, 'confirm_layout': True}

    def _doc(self, *, header: bool) -> _HwpmlHwp:
        controls = [FakeCtrl('secd')] + ([FakeCtrl('head')] if header else [])
        hwp = _HwpmlHwp(paragraphs=_paras(('Title', 0, 0), ('Item', 0, 0), ('Tail', 0, 0)), controls=controls)
        if header:
            hwp.package_state['header_footer'] = ['HEADER']
        return hwp

    def _reader(self, hwp):
        return _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))  # noqa: SLF001

    def test_page_number_with_header_inserts_when_the_header_is_unchanged(self) -> None:
        hwp = self._doc(header=True)
        result = _Service()._bundle_page_number_apply(hwp, dict(self.STEP), protection=self._reader(hwp))  # noqa: SLF001
        self.assertEqual(result['result']['status'], 'inserted')
        self.assertTrue(result['result']['protection']['checked'])
        self.assertIn('header_footer', result['result']['protection']['compared'])

    def test_header_altered_during_page_number_is_an_unknown_outcome_through_the_real_reader(self) -> None:
        hwp = self._doc(header=True)
        original = hwp.HAction.Execute

        def alter(name, hset):
            result = original(name, hset)
            hwp.package_state['header_footer'] = ['ALTERED']
            return result

        hwp.HAction.Execute = alter
        with self.assertRaises(LocalCliMutationError) as ctx:
            _Service()._bundle_page_number_apply(hwp, dict(self.STEP), protection=self._reader(hwp))  # noqa: SLF001
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(hwp.package_state['header_footer'], ['ALTERED'])
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)

    def test_header_without_a_matching_fingerprint_is_refused_before_mutation(self) -> None:
        hwp = self._doc(header=True)
        hwp.package_state['header_footer'] = []
        with self.assertRaises(LocalCliRuntimeError):
            _Service()._bundle_page_number_apply(hwp, dict(self.STEP), protection=self._reader(hwp))  # noqa: SLF001
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run'}], [])

    def test_official_numbering_through_the_real_reader_numbers_only_the_target(self) -> None:
        hwp = self._doc(header=True)
        result = _Service()._bundle_official_numbering_apply(hwp, dict(self.NUMBERING), protection=self._reader(hwp))  # noqa: SLF001
        self.assertIs(result['numbering_format_enforced'], True)
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertIn('paragraphs', result['result']['protection']['compared'])

    def test_unreadable_reader_output_before_official_numbering_refuses(self) -> None:
        hwp = self._doc(header=False)
        hwp.hwpml_text = '<HWPML><BODY>'
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _Service()._bundle_official_numbering_apply(hwp, dict(self.NUMBERING), protection=self._reader(hwp))  # noqa: SLF001
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run'}], [])


class RunStepWiringTests(unittest.TestCase):
    def _service(self):
        recorded: dict[str, Any] = {}

        class Service:
            def _numbering_protection_reader(self, handle):
                recorded['reader_handle'] = handle
                return lambda: {}

            def _bundle_page_number_apply(self, hwp, step, protection=None, on_mutating=None):
                recorded['page'] = protection
                return {'warnings': []}

            def _bundle_official_numbering_apply(self, hwp, step, protection=None, on_mutating=None):
                recorded['numbering'] = protection
                return {'warnings': []}

        return Service(), recorded

    def test_both_numbering_operations_receive_a_protection_reader(self) -> None:
        from app.command_packages.common import run_step_for_op

        for op, key in (('page_number_apply', 'page'), ('official_numbering_apply', 'numbering')):
            service, recorded = self._service()
            handle = SimpleNamespace(hwp=object(), mark_mutating_intent=lambda: None)
            run_step_for_op(op, service=service, handle=handle, step={}, binding=None, manifest={})
            self.assertTrue(callable(recorded[key]), msg=op)
            self.assertIs(recorded['reader_handle'], handle, msg=op)

    def test_protection_dependent_operations_are_not_given_a_snapshot_reader(self) -> None:
        import inspect

        from app import local_cli_bundle_numbering

        source = inspect.getsource(local_cli_bundle_numbering)
        for forbidden in ('_snapshot_temp_hwpx', 'save_as(', 'SaveAs'):
            self.assertNotIn(forbidden, source)


class NativeBinItemIdentityTests(unittest.TestCase):
    """SYNTHETIC native-shaped metadata, not a claim of native execution.

    The separate saved native raw witness is replayed by the offline release harness.
    These mutants exercise identity refusal and preservation through the real reader.
    """

    @staticmethod
    def native_xml(text: str, item: str = '<BINITEM BinData="1" Type="Embedding" Format="bmp"/>') -> str:
        import re
        text = re.sub(r'<BINDATA Id="1"[^>]*>.*?</BINDATA>', lambda _m: item, text)
        return text.replace('</HWPML>', '<BINDATASTORAGE><BINDATA Id="1">PAYLOAD</BINDATA></BINDATASTORAGE></HWPML>')

    def sample(self, item: str = '<BINITEM BinData="1" Type="Embedding" Format="bmp"/>') -> str:
        return self.native_xml(hwpml([('Title', 0, 0), ('Item', 0, 0)], picture_width=400), item)

    def refused(self, text: str) -> None:
        from app.native_numbering_census import CensusError
        try:
            result = _census(text)
        except CensusError:
            return
        self.assertIsNone(result['paragraphs'], 'ambiguous media must never be proven')

    def test_native_bindata_identity_resolves_and_preserves_all_fingerprints(self) -> None:
        before = _census(self.sample())
        self.assertIsInstance(before['paragraphs'], list)
        self.assertEqual(len(before['paragraphs']), 2)
        for old, new in [('Format="bmp"', 'Format="png"'), ('Type="Embedding"', 'Type="Link"'),
                         ('Width="400"', 'Width="401"'), ('PAYLOAD', 'LOST'),
                         ('BinData="1"', 'BinData="2"')]:
            with self.subTest(attribute=old):
                after = _census(self.sample().replace(old, new))
                self.assertNotEqual(before, after)
                if old != 'Width="400"':
                    self.assertNotEqual(before['media_sha256'], after['media_sha256'])

    def test_historic_id_and_consistent_dual_identity_remain_supported(self) -> None:
        for attrs in ('Id="1"', 'Id="1" BinData="1"', 'bindata="1"'):
            with self.subTest(attrs=attrs):
                self.assertIsInstance(_census(self.sample(f'<BINITEM {attrs} Format="bmp"/>'))['paragraphs'], list)
        self.assertIsInstance(_census(hwpml([('Title', 0, 0)], picture_width=400))['paragraphs'], list)

    def test_missing_blank_contradictory_and_duplicate_identity_attributes_refuse(self) -> None:
        for attrs in ('', 'BinData=""', 'BinData=" "', 'Id="1" BinData="2"',
                      'Id="" BinData="1"', 'BinData="1" bindata="2"',
                      'Id="1" id="1" BinData="1"'):
            with self.subTest(attrs=attrs):
                self.refused(self.sample(f'<BINITEM {attrs} Format="bmp"/>'))

    def test_duplicate_native_and_mixed_historic_entries_refuse(self) -> None:
        first = '<BINITEM BinData="1" Format="bmp"/>'
        for other in (first, '<BINITEM Id="1" Format="png"/>', '<BINDATA Id="1"/>'):
            with self.subTest(other=other):
                self.refused(self.sample(first + other))
                self.refused(self.sample(other + first))

    def test_unknown_empty_and_non_native_references_do_not_fall_back(self) -> None:
        for ref in ('', '2', 'not-an-id'):
            with self.subTest(ref=ref):
                self.refused(self.sample().replace('BinItem="1"', f'BinItem="{ref}"'))
        # BinData is not a general alternative Id for arbitrary list members.
        self.refused(self.sample('<BINDATA BinData="1"/>'))
        self.refused(self.sample().replace('<CHARSHAPE Id="1"', '<CHARSHAPE BinData="1"'))

    def public_doc(self):
        class NativeMediaHwp(_HwpmlHwp):
            def get_text_file(inner, fmt='UNICODE', option=''):
                text = super().get_text_file(fmt, option)
                if fmt != 'HWPML2X':
                    return text
                text = self.native_xml(text, getattr(inner, 'media_item', '<BINITEM BinData="1" Type="Embedding" Format="bmp"/>'))
                return text.replace('PAYLOAD', getattr(inner, 'media_payload', 'PAYLOAD'))
        hwp = NativeMediaHwp(paragraphs=_paras(('Title', 0, 0), ('Item', 0, 0), ('Tail', 0, 0)), controls=[FakeCtrl('secd')])
        hwp.hwpml_extras['picture_width'] = 400
        return hwp

    def test_public_official_apply_accepts_native_identity_through_live_reader(self) -> None:
        hwp = self.public_doc()
        reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))
        result = _Service()._bundle_official_numbering_apply(hwp, dict(EndToEndThroughTheRealReaderTests.NUMBERING), protection=reader)
        self.assertTrue(result['numbering_format_enforced'])
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertGreater(hwp.hwpml_calls, 1)

    def test_public_official_apply_refuses_protected_media_loss_after_mutation(self) -> None:
        for field, value in (('media_payload', ''), ('media_item', ''),
                             ('media_item', '<BINITEM BinData="1" Type="Link" Format="bmp"/>'),
                             ('media_item', '<BINITEM BinData="1" Type="Embedding" Format="png"/>')):
            with self.subTest(field=field, value=value):
                hwp = self.public_doc()
                reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))
                original = hwp.HAction.Execute
                def damage(name, hset):
                    result = original(name, hset)
                    setattr(hwp, field, value)
                    return result
                hwp.HAction.Execute = damage
                with self.assertRaises(LocalCliMutationError) as ctx:
                    _Service()._bundle_official_numbering_apply(hwp, dict(EndToEndThroughTheRealReaderTests.NUMBERING), protection=reader)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertGreater(hwp.hwpml_calls, 1)


class NativeBinDataStorageSizeTests(unittest.TestCase):
    """SYNTHETIC native-shaped storage, not a claim of native execution.

    Retained native HWPML2X reads of one unchanged embedded picture report BINDATA Size values 121, 125,
    126, 127 and 129 for a byte-identical payload; the offline release harness replays those reads.
    """

    @staticmethod
    def stored(text: str, *, size: str | None = '126', payload: str = 'PAYLOAD',
               attrs: str = 'Compress="true" Encoding="Base64" Id="1"') -> str:
        text = NativeBinItemIdentityTests.native_xml(text)
        sized = f' Size="{size}"' if size is not None else ''
        return text.replace('<BINDATA Id="1">PAYLOAD</BINDATA>', f'<BINDATA {attrs}{sized}>{payload}</BINDATA>')

    def sample(self, **kw: Any) -> str:
        return self.stored(hwpml([('Title', 0, 0), ('Item', 0, 0)], picture_width=400), **kw)

    def test_storage_size_alone_changes_no_fingerprint(self) -> None:
        before = _census(self.sample())
        self.assertIsInstance(before['paragraphs'], list)
        for size in ('121', '125', '127', '129'):
            with self.subTest(size=size):
                self.assertEqual(_census(self.sample(size=size)), before)

    def test_payload_storage_attributes_and_size_presence_stay_bound(self) -> None:
        before = _census(self.sample())
        for name, kw in {'payload': dict(payload='LOST'), 'payload with another size': dict(payload='LOST', size='127'),
                         'compress': dict(attrs='Compress="false" Encoding="Base64" Id="1"'),
                         'encoding': dict(attrs='Compress="true" Encoding="Hex" Id="1"'),
                         'storage id': dict(attrs='Compress="true" Encoding="Base64" Id="2"'),
                         'size absent': dict(size=None)}.items():
            with self.subTest(name=name):
                self.assertNotEqual(_census(self.sample(**kw))['media_sha256'], before['media_sha256'])
        extra = self.sample().replace('</BINDATASTORAGE>', '<BINDATA Compress="true" Encoding="Base64" Id="2" Size="1">X</BINDATA></BINDATASTORAGE>')
        self.assertNotEqual(_census(extra)['media_sha256'], before['media_sha256'])

    def test_size_outside_storage_stays_bound(self) -> None:
        before = _census(self.sample())
        after = _census(self.sample().replace('<BINITEM BinData="1"', '<BINITEM Size="5" BinData="1"'))
        self.assertNotEqual(after['media_sha256'], before['media_sha256'])
        self.assertNotEqual(after['bindata_sha256'], before['bindata_sha256'])

    def public_doc(self) -> Any:
        sizes = iter(('126', '129', '127', '121', '125') * 8)

        class SizeVaryingHwp(_HwpmlHwp):
            def get_text_file(inner, fmt='UNICODE', option=''):  # noqa: N805
                text = super().get_text_file(fmt, option)
                if fmt != 'HWPML2X':
                    return text
                return NativeBinDataStorageSizeTests.stored(text, size=next(sizes), payload=getattr(inner, 'media_payload', 'PAYLOAD'))

        hwp = SizeVaryingHwp(paragraphs=_paras(('Title', 0, 0), ('Item', 0, 0), ('Tail', 0, 0)), controls=[FakeCtrl('secd')])
        hwp.hwpml_extras['picture_width'] = 400
        return hwp

    def test_public_official_apply_accepts_a_storage_size_that_differs_between_reads(self) -> None:
        hwp = self.public_doc()
        reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))
        result = _Service()._bundle_official_numbering_apply(hwp, dict(EndToEndThroughTheRealReaderTests.NUMBERING), protection=reader)
        self.assertTrue(result['numbering_format_enforced'])
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertGreater(hwp.hwpml_calls, 1)

    def test_public_official_apply_still_refuses_payload_loss_when_size_also_differs(self) -> None:
        hwp = self.public_doc()
        reader = _Service()._numbering_protection_reader(SimpleNamespace(hwp=hwp))
        original = hwp.HAction.Execute

        def damage(name, hset):
            result = original(name, hset)
            hwp.media_payload = ''
            return result

        hwp.HAction.Execute = damage
        with self.assertRaises(LocalCliMutationError) as ctx:
            _Service()._bundle_official_numbering_apply(hwp, dict(EndToEndThroughTheRealReaderTests.NUMBERING), protection=reader)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)


if __name__ == '__main__':
    unittest.main()
