from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from app.command_packages.commands.object_insert_exact.run import validate_step
from app.local_cli_object_insert import LocalCliObjectInsertMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from app.object_insert import (
    KINDS,
    PARAM_KEYS,
    ObjectInsertError,
    check_before,
    evaluate_insert,
    mm_to_hwpunit,
    normalize_step,
    parse_hwpml,
)

MANIFEST_PATH = Path(__file__).resolve().parents[1] / 'app' / 'command_packages' / 'commands' / 'object_insert_exact' / 'manifest.json'
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))


def _doc(*paragraphs: str, declaration: bool = False) -> str:
    body = ''.join(f'<P><TEXT>{inner}</TEXT></P>' for inner in paragraphs)
    prefix = '<?xml version="1.0" encoding="UTF-16" standalone="no"?>' if declaration else ''
    return f'{prefix}<HWPML><HEAD/><BODY><SECTION>{body}</SECTION></BODY><TAIL><BINDATASTORAGE>QUJD</BINDATASTORAGE></TAIL></HWPML>'


def _char(text: str) -> str:
    return f'<CHAR>{escape(text)}</CHAR>'


def _footnote(text: str) -> str:
    return f'<FOOTNOTE><PARALIST><P><TEXT><AUTONUM Number="1" Type="Footnote"/>{_char(text)}</TEXT></P></PARALIST></FOOTNOTE>'


def _shape(tag: str, width: int, height: int, treat: bool = True) -> str:
    return (f'<{tag}><SHAPEOBJECT><SIZE Width="{width}" Height="{height}"/>'
            f'<POSITION TreatAsChar="{"true" if treat else "false"}"/></SHAPEOBJECT></{tag}>')


def _plan(**fields: Any) -> dict[str, Any]:
    step = {'kind': 'footnote', 'text': 'note', 'expected_pos': [0, 0, 3], 'confirm_mutation': True}
    step.update(fields)
    step = {key: value for key, value in step.items() if value is not None}
    return normalize_step(step)


def _evaluate(plan: dict[str, Any], before: str, after: str) -> dict[str, Any]:
    return evaluate_insert(plan, parse_hwpml(before), parse_hwpml(after))


BEFORE = _doc(_char('Hello world'), _char('Second'))


class VerifierTests(unittest.TestCase):
    def test_footnote_added_with_nested_autonum_is_accepted(self) -> None:
        after = _doc(_char('Hello') + _footnote('note') + _char(' world'), _char('Second'))
        result = _evaluate(_plan(), BEFORE, after)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['observed_delta'], {'AUTONUM': 1, 'FOOTNOTE': 1})

    def test_accepts_xml_declaration_and_bom(self) -> None:
        after = _doc(_char('Hello') + _footnote('note') + _char(' world'), _char('Second'), declaration=True)
        self.assertTrue(_evaluate(_plan(), '\ufeff' + BEFORE, after)['ok'])

    def test_rejects_no_op_wrong_tag_and_extra_controls(self) -> None:
        cases = (
            (BEFORE, 'FOOTNOTE count changed by 0'),
            (_doc(_char('Hello') + _footnote('note').replace('FOOTNOTE', 'ENDNOTE') + _char(' world'), _char('Second')), 'FOOTNOTE count changed by 0'),
            (_doc(_char('Hello') + _footnote('a') + _footnote('note') + _char(' world'), _char('Second')), 'changed by 2'),
            (_doc(_char('Hello') + _footnote('note') + '<BOOKMARK Name="x"/>' + _char(' world'), _char('Second')), "other controls changed: {'BOOKMARK': 1}"),
        )
        for after, reason in cases:
            with self.subTest(reason=reason):
                result = _evaluate(_plan(), BEFORE, after)
                self.assertFalse(result['ok'])
                self.assertIn(reason, ' '.join(result['reasons']))

    def test_rejects_changed_body_text_or_missing_note_text(self) -> None:
        after = _doc(_char('Hello') + _footnote('note') + _char(' wor'), _char('Second'))
        self.assertIn('document text outside the new object changed', _evaluate(_plan(), BEFORE, after)['reasons'])
        after = _doc(_char('Hello') + _footnote('other') + _char(' world'), _char('Second'))
        self.assertIn('text is not exactly the requested text', ' '.join(_evaluate(_plan(), BEFORE, after)['reasons']))

    def test_hyperlink_keeps_display_text_and_needs_url_in_command(self) -> None:
        plan = _plan(kind='hyperlink', text=None, url='https://example.com/a', display_text='world')
        link = '<FIELDBEGIN Type="Hyperlink" Command="https://example.com/a;1;0;0;"/>'
        after = _doc(_char('Hello ') + link + _char('world') + '<FIELDEND Type="Hyperlink"/>', _char('Second'))
        self.assertTrue(_evaluate(plan, BEFORE, after)['ok'])
        wrong = after.replace('example.com/a', 'example.org/a')
        self.assertIn('Command does not name exactly the url', ' '.join(_evaluate(plan, BEFORE, wrong)['reasons']))
        no_end = after.replace('<FIELDEND Type="Hyperlink"/>', '')
        self.assertIn('FIELDEND count changed by 0', ' '.join(_evaluate(plan, BEFORE, no_end)['reasons']))
        memo_type = after.replace('Type="Hyperlink" Command', 'Type="Memo" Command')
        self.assertIn('FIELDBEGIN:Hyperlink count changed by 0', ' '.join(_evaluate(plan, BEFORE, memo_type)['reasons']))

    def test_memo_bookmark_equation_shapes_and_header(self) -> None:
        memo = '<FIELDBEGIN Type="Memo"><SUBLIST><P><TEXT>' + _char('check') + '</TEXT></P></SUBLIST></FIELDBEGIN>'
        cases = (
            (_plan(kind='memo', text='check'), _char('Hello') + memo + _char(' world') + '<FIELDEND/>', 'Memo">', 'Memo2">'),
            (_plan(kind='bookmark', text=None, name='bm1'), '<BOOKMARK Name="bm1"/>' + _char('Hello world'), 'bm1', 'bm2'),
            (_plan(kind='equation', text=None, script='a over b'), _char('Hello') + '<EQUATION><SCRIPT>a over b</SCRIPT></EQUATION>' + _char(' world'), 'a over b', 'a over c'),
            (_plan(kind='rectangle', text=None, width_mm=20, height_mm=10), _shape('RECTANGLE', mm_to_hwpunit(20), mm_to_hwpunit(10)) + _char('Hello world'), 'Width="5669"', 'Width="5000"'),
            (_plan(kind='ellipse', text=None, width_mm=20, height_mm=10, treat_as_char=False), _shape('ELLIPSE', 5669, 2835, treat=False) + _char('Hello world'), 'false', 'true'),
            (_plan(kind='line', text=None, width_mm=20, height_mm=0), _shape('LINE', 5669, 0) + _char('Hello world'), 'Width="5669"', 'Width="1"'),
            (_plan(kind='header', text='Top', apply_to='even'),
             '<HEADER ApplyPageType="Even"><PARALIST><P><TEXT>' + _char('Top') + '</TEXT></P></PARALIST></HEADER>' + _char('Hello world'), 'Even', 'Odd'),
            (_plan(kind='footer', text='Bottom'),
             '<FOOTER ApplyPageType="Both"><PARALIST><P><TEXT>' + _char('Bottom') + '</TEXT></P></PARALIST></FOOTER>' + _char('Hello world'), 'Bottom<', 'Other<'),
        )
        for plan, first, good, bad in cases:
            with self.subTest(kind=plan['kind']):
                after = _doc(first, _char('Second'))
                self.assertTrue(_evaluate(plan, BEFORE, after)['ok'], _evaluate(plan, BEFORE, after))
                self.assertIn(good, after)
                self.assertFalse(_evaluate(plan, BEFORE, after.replace(good, bad, 1))['ok'])

    def test_proof_is_bound_to_the_new_control_not_a_preexisting_twin(self) -> None:
        eq = lambda script: f'<EQUATION><SCRIPT>{script}</SCRIPT></EQUATION>'
        link = lambda command: f'<FIELDBEGIN Type="Hyperlink" Command="{command}"/>'
        good_link = 'https://example.com/a;1;0;0;'
        rect = lambda w: _shape('RECTANGLE', w, mm_to_hwpunit(10))
        cases = (
            (_plan(kind='equation', text=None, script='a over b'),
             _doc(eq('a over b') + _char('Hello world'), _char('Second')),
             _doc(eq('a over b') + _char('Hello') + eq('a over c') + _char(' world'), _char('Second')),
             'script differs'),
            (_plan(kind='hyperlink', text=None, url='https://example.com/a', display_text='world'),
             _doc(link(good_link) + _char('Hello') + '<FIELDEND Type="Hyperlink"/>' + _char(' world'), _char('Second')),
             _doc(link(good_link) + _char('Hello') + '<FIELDEND Type="Hyperlink"/>' + _char(' ') + link('') + _char('world') + '<FIELDEND Type="Hyperlink"/>', _char('Second')),
             'does not name exactly the url'),
            (_plan(kind='rectangle', text=None, width_mm=20, height_mm=10),
             _doc(rect(mm_to_hwpunit(20)) + _char('Hello world'), _char('Second')),
             _doc(rect(mm_to_hwpunit(20)) + _char('Hello') + rect(1000) + _char(' world'), _char('Second')),
             'Width is'),
        )
        for plan, before, after, reason in cases:
            with self.subTest(kind=plan['kind']):
                result = _evaluate(plan, before, after)
                self.assertFalse(result['ok'], result)
                self.assertIn(reason, ' '.join(result['reasons']))
                twin_ok = after.replace('a over c', 'a over b').replace('Command=""', f'Command="{good_link}"').replace('Width="1000"', f'Width="{mm_to_hwpunit(20)}"')
                self.assertTrue(_evaluate(plan, before, twin_ok)['ok'], _evaluate(plan, before, twin_ok))

    def test_hyperlink_must_wrap_exactly_display_text(self) -> None:
        plan = _plan(kind='hyperlink', text=None, url='https://example.com/a', display_text='world')
        link = '<FIELDBEGIN Type="Hyperlink" Command="https\\://example.com/a;1;0;0;"/>'
        after = _doc(_char('Hello ') + link + _char('world') + '<FIELDEND Type="Hyperlink"/>', _char('Second'))
        self.assertTrue(_evaluate(plan, BEFORE, after)['ok'], _evaluate(plan, BEFORE, after))
        short = _doc(_char('Hello ') + link + _char('wor') + '<FIELDEND Type="Hyperlink"/>' + _char('ld'), _char('Second'))
        self.assertIn('does not wrap exactly display_text', ' '.join(_evaluate(plan, BEFORE, short)['reasons']))

    def test_paragraph_structure_char_shape_and_bindata_are_preserved(self) -> None:
        after_ok = _doc(_char('Hello') + _footnote('note') + _char(' world'), _char('Second'))
        split = _doc(_char('Hello') + _footnote('note'), _char(' world'), _char('Second'))
        self.assertIn('paragraph structure changed', ' '.join(_evaluate(_plan(), BEFORE, split)['reasons']))
        restyled = after_ok.replace('<TEXT><CHAR>Second', '<TEXT CharShape="7"><CHAR>Second')
        self.assertIn('document text outside the new object changed', ' '.join(_evaluate(_plan(), BEFORE, restyled)['reasons']))
        para_shape = after_ok.replace('<P><TEXT><CHAR>Second', '<P ParaShape="3"><TEXT><CHAR>Second')
        self.assertFalse(_evaluate(_plan(), BEFORE, para_shape)['ok'])
        bindata = after_ok.replace('QUJD', 'QUJE')
        self.assertIn('TAIL changed', ' '.join(_evaluate(_plan(), BEFORE, bindata)['reasons']))
        pic_before = _doc('<PICTURE><IMAGE BinItem="1"/></PICTURE>' + _char('Hello world'), _char('Second'))
        pic_after = _doc('<PICTURE><IMAGE BinItem="2"/></PICTURE>' + _char('Hello') + _footnote('note') + _char(' world'), _char('Second'))
        self.assertFalse(_evaluate(_plan(), pic_before, pic_after)['ok'])
        self.assertTrue(_evaluate(_plan(), pic_before, pic_after.replace('BinItem="2"', 'BinItem="1"'))['ok'])

    def test_insert_inside_a_table_cell_descends_into_that_table_only(self) -> None:
        def table(cell_a: str, cell_b: str, width: int = 1000, border: str = '3', span: str = '1') -> str:
            return (f'<TABLE><SHAPEOBJECT><SIZE Width="{width}" Height="500"/></SHAPEOBJECT><ROW>'
                    f'<CELL BorderFill="{border}" ColSpan="{span}"><PARALIST><P><TEXT>{cell_a}</TEXT></P></PARALIST></CELL>'
                    f'<CELL BorderFill="3" ColSpan="1"><PARALIST><P><TEXT>{cell_b}</TEXT></P></PARALIST></CELL></ROW></TABLE>')
        before = _doc(table(_char('ab'), _char('cd')) + _char('Hello'), _char('Second'))
        noted = _char('a') + _footnote('note') + _char('b')
        result = _evaluate(_plan(), before, _doc(table(noted, _char('cd')) + _char('Hello'), _char('Second')))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['inserted_at'][0], 'TABLE')
        for label, changed in (
            ('other cell text', table(noted, _char('cX'))),
            ('table SIZE Width', table(noted, _char('cd'), width=2000)),
            ('cell BorderFill', table(noted, _char('cd'), border='4')),
            ('cell ColSpan', table(noted, _char('cd'), span='2')),
        ):
            with self.subTest(label=label):
                result = _evaluate(_plan(), before, _doc(changed + _char('Hello'), _char('Second')))
                self.assertFalse(result['ok'], result)

    def test_head_may_only_grow_and_tail_must_not_change(self) -> None:
        def doc(head: str, *paragraphs: str) -> str:
            return _doc(*paragraphs).replace('<HEAD/>', head)
        head = '<HEAD SecCnt="1"><MAPPINGTABLE><CHARSHAPELIST Count="1"><CHARSHAPE Id="0" Height="1000"/></CHARSHAPELIST></MAPPINGTABLE></HEAD>'
        grown = head.replace('Count="1"><CHARSHAPE Id="0" Height="1000"/>', 'Count="2"><CHARSHAPE Id="0" Height="1000"/><CHARSHAPE Id="1" Height="900"/>')
        inserted = (_char('Hello') + _footnote('note') + _char(' world'), _char('Second'))
        before = doc(head, _char('Hello world'), _char('Second'))
        self.assertTrue(_evaluate(_plan(), before, doc(grown, *inserted))['ok'])
        for tag, entry, child in (('TABDEF', 'TABITEM', 'TABDEFLIST'), ('NUMBERING', 'PARAHEAD', 'NUMBERINGLIST'), ('STYLE', 'STYLEPR', 'STYLELIST')):
            with self.subTest(grow_inside=tag):
                listed = head.replace('</MAPPINGTABLE>', f'<{child} Count="1"><{tag} Id="0"/></{child}></MAPPINGTABLE>')
                changed = listed.replace(f'<{tag} Id="0"/>', f'<{tag} Id="0"><{entry}/></{tag}>')
                result = _evaluate(_plan(), doc(listed, _char('Hello world'), _char('Second')), doc(changed, *inserted))
                self.assertFalse(result['ok'], result)
        for label, after_head in (
            ('existing shape changed', head.replace('Height="1000"', 'Height="1200"')),
            ('entry dropped', head.replace('Count="1"><CHARSHAPE Id="0" Height="1000"/>', 'Count="0">')),
            ('section count', head.replace('SecCnt="1"', 'SecCnt="2"')),
            ('non-list growth', head.replace('</MAPPINGTABLE>', '</MAPPINGTABLE><COMPATIBLEDOCUMENT/>')),
            ('child added inside an existing entry', head.replace('<CHARSHAPE Id="0" Height="1000"/>', '<CHARSHAPE Id="0" Height="1000"><UNDERLINE/></CHARSHAPE>')),
            ('entry appended to a non-growable list', head.replace('</MAPPINGTABLE>', '<STYLELIST Count="0"/></MAPPINGTABLE>').replace('<MAPPINGTABLE>', '<MAPPINGTABLE>')),
            ('wrong entry tag appended', head.replace('Count="1"><CHARSHAPE Id="0" Height="1000"/>', 'Count="2"><CHARSHAPE Id="0" Height="1000"/><PARASHAPE Id="1"/>')),
        ):
            with self.subTest(label=label):
                result = _evaluate(_plan(), before, doc(after_head, *inserted))
                self.assertFalse(result['ok'], result)
                self.assertIn('HEAD', ' '.join(result['reasons']))
        dropped = doc(head, *inserted).replace('<TAIL><BINDATASTORAGE>QUJD</BINDATASTORAGE></TAIL>', '<TAIL/>')
        self.assertIn('TAIL changed', ' '.join(_evaluate(_plan(), before, dropped)['reasons']))

    def test_check_before_refuses_unverifiable_or_conflicting_documents(self) -> None:
        header = '<HEADER ApplyPageType="Both"><PARALIST><P><TEXT>' + _char('x') + '</TEXT></P></PARALIST></HEADER>'
        with self.assertRaisesRegex(ObjectInsertError, 'replacing an existing header'):
            check_before(_plan(kind='header', text='t', apply_to='odd'), parse_hwpml(_doc(header + _char('a'))))
        check_before(_plan(kind='footer', text='t'), parse_hwpml(_doc(header + _char('a'))))
        with self.assertRaisesRegex(ObjectInsertError, 'already exists'):
            check_before(_plan(kind='bookmark', text=None, name='b'), parse_hwpml(_doc('<BOOKMARK Name="b"/>' + _char('a'))))
        with self.assertRaisesRegex(ObjectInsertError, 'outside CHAR runs'):
            check_before(_plan(), parse_hwpml('<HWPML><BODY><SECTION><P><TEXT>loose</TEXT></P></SECTION></BODY></HWPML>'))
        for bad in ('', None, '<HWPML><BODY>'):
            with self.subTest(bad=bad), self.assertRaises(ObjectInsertError):
                parse_hwpml(bad)


class _Err(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def _validate(**fields: Any) -> dict[str, Any]:
    step = {'op': 'object_insert_exact', 'kind': 'footnote', 'text': 'n', 'expected_pos': [0, 0, 0], 'confirm_mutation': True}
    step.update(fields)
    step = {key: value for key, value in step.items() if value is not None}
    return validate_step(service=None, index=1, step=step, manifest=MANIFEST, error_type=_Err)


class ValidationTests(unittest.TestCase):
    def test_manifest_lists_every_field(self) -> None:
        self.assertEqual(set(MANIFEST['allowed_keys']), {'op', 'operation', 'label', 'kind', 'expected_pos', 'confirm_mutation', *PARAM_KEYS})
        self.assertIs(MANIFEST['read_only'], False)
        self.assertEqual(MANIFEST['op'], 'object_insert_exact')

    def test_valid_steps_for_every_kind(self) -> None:
        cases = {
            'footnote': {}, 'endnote': {}, 'memo': {},
            'hyperlink': {'text': None, 'url': 'mailto:a@b.c', 'display_text': 'mail'},
            'bookmark': {'text': None, 'name': 'x' * 40},
            'equation': {'text': None, 'script': 'x^2\n+1'},
            'line': {'text': None, 'width_mm': 10, 'height_mm': 0},
            'rectangle': {'text': None, 'width_mm': 10.5, 'height_mm': 3, 'treat_as_char': False},
            'ellipse': {'text': None, 'width_mm': 1, 'height_mm': 1},
            'header': {'apply_to': 'odd'}, 'footer': {},
        }
        self.assertEqual(set(cases), set(KINDS))
        for kind, fields in cases.items():
            with self.subTest(kind=kind):
                step = _validate(kind=kind, **fields)
                self.assertEqual(step['kind'], kind)
        self.assertEqual(_validate(kind='footer')['apply_to'], 'both')
        self.assertIs(_validate(kind='ellipse', text=None, width_mm=1, height_mm=1)['treat_as_char'], True)
        self.assertEqual(_validate(expected_pos=(1, 2, 3))['expected_pos'], [1, 2, 3])

    def test_rejections(self) -> None:
        cases = (
            ({'kind': 'picture'}, 'kind must be one of'),
            ({'confirm_mutation': None}, 'confirm_mutation'),
            ({'confirm_mutation': 'yes'}, 'confirm_mutation'),
            ({'expected_pos': None}, 'expected_pos'),
            ({'expected_pos': [0, 0]}, 'expected_pos'),
            ({'expected_pos': [0, True, 0]}, 'expected_pos'),
            ({'expected_pos': [0, -1, 0]}, 'expected_pos'),
            ({'text': None}, 'text must be a non-empty string'),
            ({'text': '  '}, 'text must be a non-empty string'),
            ({'text': 'a\nb'}, 'control characters'),
            ({'text': 'x' * 2001}, 'at most 2000'),
            ({'url': 'https://x.y'}, 'does not take: url'),
            ({'image_path': 'C:/a.png'}, 'unsupported fields: image_path'),
            ({'in_cell': True}, 'unsupported fields: in_cell'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'javascript:alert(1)', 'display_text': 'a'}, 'scheme'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'file:///c:/x', 'display_text': 'a'}, 'scheme'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'https://a.b/x;1', 'display_text': 'a'}, 'Command format'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'https:///path', 'display_text': 'a'}, 'needs a host'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'https://a.b/' + 'x' * 2000, 'display_text': 'a'}, 'at most 2000'),
            ({'kind': 'hyperlink', 'text': None, 'url': 'https://a.b'}, 'display_text'),
            ({'kind': 'hyperlink', 'url': 'https://a.b', 'display_text': 'a'}, 'does not take: text'),
            ({'kind': 'bookmark', 'text': None, 'name': 'x' * 41}, 'at most 40'),
            ({'kind': 'bookmark', 'text': None, 'name': 'a\tb'}, 'control characters'),
            ({'kind': 'bookmark', 'text': None, 'name': 'b', 'apply_to': 'both'}, 'does not take: apply_to'),
            ({'kind': 'equation', 'text': None, 'script': 'x' * 4001}, 'at most 4000'),
            ({'kind': 'equation', 'text': None, 'script': 'a\x00'}, 'control characters'),
            ({'kind': 'rectangle', 'text': None, 'width_mm': 0, 'height_mm': 3}, 'positive width_mm'),
            ({'kind': 'rectangle', 'text': None, 'width_mm': -1, 'height_mm': 3}, '0..1000'),
            ({'kind': 'rectangle', 'text': None, 'width_mm': True, 'height_mm': 3}, 'millimetres'),
            ({'kind': 'rectangle', 'text': None, 'width_mm': float('nan'), 'height_mm': 3}, 'millimetres'),
            ({'kind': 'line', 'text': None, 'width_mm': 0, 'height_mm': 0}, 'non-zero'),
            ({'kind': 'line', 'text': None, 'width_mm': 3}, 'height_mm'),
            ({'kind': 'ellipse', 'text': None, 'width_mm': 3, 'height_mm': 3, 'treat_as_char': 1}, 'boolean'),
            ({'kind': 'footnote', 'width_mm': 3}, 'does not take: width_mm'),
            ({'kind': 'header', 'apply_to': 'first'}, 'apply_to'),
            ({'target_id': 'ctrl/1'}, 'unsupported fields: target_id'),
        )
        for fields, message in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(_Err, message) as caught:
                _validate(**fields)
            self.assertEqual(caught.exception.status_code, 400)


# ------------------------------------------------------------------ fake Hancom


class _Set:
    def __init__(self, hwp: _FakeHwp, name: str) -> None:
        self.hwp, self.name, self.items = hwp, name, {}
        self.HSet = self

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.items[name] = value


class _Params:
    def __init__(self, hwp: _FakeHwp) -> None:
        self.hwp = hwp

    def __getattr__(self, name: str) -> _Set:
        if not name.startswith('H'):
            raise AttributeError(name)
        return _Set(self.hwp, name)


class _FakeHAction:
    def __init__(self, hwp: _FakeHwp) -> None:
        self.hwp = hwp

    def Run(self, name: str) -> bool:  # noqa: N802
        return self.hwp.native(name, None)

    def GetDefault(self, name: str, hset: _Set) -> bool:  # noqa: N802
        self.hwp.log.append(f'GetDefault({name})')
        hset.items.clear()
        return True

    def Execute(self, name: str, hset: _Set) -> bool:  # noqa: N802
        return self.hwp.native(name, dict(hset.items))


class _FakeHwp:
    """One-section document of paragraphs; each paragraph is a token list.

    A token is one character or one control XML string (Hancom counts a
    control as one position). Fault knobs: ``noop`` actions succeed but change
    nothing; ``raising`` actions raise; ``wrong_tag`` makes a note an ENDNOTE;
    ``extra_control`` also drops a bookmark; ``eat_char`` deletes the next body
    character; ``no_readback`` removes GetTextFile; ``bad_readback`` returns
    garbage; ``snapshot_unknown`` hides the selection state; ``no_return``
    keeps the caret in the note after CloseEx.
    """

    SelectionMode = 0

    def __init__(self, paragraphs: tuple[str, ...] = ('Hello world', 'Second'), **faults: Any) -> None:
        self.paras: list[list[str]] = [list(text) for text in paragraphs]
        self.caret = [0, 0, 5]
        self.selection: tuple[int, int] | None = None
        self.pending: dict[str, Any] | None = None
        self.log: list[str] = []
        self.plain_run_calls: list[str] = []
        self.readbacks = 0
        self.noop = set(faults.get('noop', ()))
        self.raising = set(faults.get('raising', ()))
        self.wrong_tag = bool(faults.get('wrong_tag'))
        self.extra_control = bool(faults.get('extra_control'))
        self.eat_char = bool(faults.get('eat_char'))
        self.bad_readback = bool(faults.get('bad_readback'))
        self.snapshot_unknown = bool(faults.get('snapshot_unknown'))
        self.no_return = bool(faults.get('no_return'))
        if faults.get('no_readback'):
            self.GetTextFile = None  # type: ignore[assignment]
        self.HAction = _FakeHAction(self)
        self.HParameterSet = _Params(self)

    # -- document model
    def xml(self) -> str:
        paragraphs = []
        for tokens in self.paras:
            inner, run = '', ''
            for token in tokens:
                if len(token) == 1:
                    run += token
                    continue
                inner += _char(run) if run else ''
                inner, run = inner + token, ''
            paragraphs.append(inner + (_char(run) if run else ''))
        return _doc(*paragraphs)

    def put(self, xml: str, at: int | None = None) -> None:
        para, pos = self.caret[1], self.caret[2] if at is None else at
        self.paras[para].insert(pos, xml)
        if self.extra_control:
            self.paras[para].insert(pos, '<BOOKMARK Name="stray"/>')
        if self.eat_char and pos + 1 < len(self.paras[para]):
            del self.paras[para][pos + 1]

    # -- native entry points
    def native(self, name: str, items: dict[str, Any] | None) -> bool:
        self.log.append(name if items is None else f'Execute({name})')
        if name in self.raising:
            self.log[-1] += '!raised'
            raise RuntimeError(f'COM error in {name}')
        if name in self.noop:
            return True
        if name in ('InsertFootnote', 'InsertEndnote', 'InsertFieldMemo'):
            tag = {'InsertFootnote': 'FOOTNOTE', 'InsertEndnote': 'ENDNOTE', 'InsertFieldMemo': 'MEMO'}[name]
            self.pending = {'tag': 'ENDNOTE' if self.wrong_tag and tag == 'FOOTNOTE' else tag, 'text': '', 'body': list(self.caret)}
            self.caret = [1, 0, 0]
        elif name == 'HeaderFooter':
            tag = 'HEADER' if items.get('Type') == 0 else 'FOOTER'
            apply = {0: 'Both', 1: 'Even', 2: 'Odd'}[items['ApplyTo']]
            self.pending = {'tag': tag, 'text': '', 'apply': apply, 'body': list(self.caret)}
            self.caret = [2, 0, 0]
        elif name == 'CloseEx' and self.pending is not None:
            self.commit()
        elif name == 'InsertHyperlink':
            start, end = self.selection
            self.paras[self.caret[1]].insert(end, '<FIELDEND Type="Hyperlink"/>')
            self.put(f'<FIELDBEGIN Type="Hyperlink" Command={quoteattr(items["Command"])}/>', at=start)
        elif name == 'Bookmark':
            self.put(f'<BOOKMARK Name={quoteattr(items["Name"])}/>')
        elif name == 'EquationCreate':
            self.put(f'<EQUATION><SHAPEOBJECT/><SCRIPT>{escape(items["string"])}</SCRIPT></EQUATION>')
        elif name.startswith('DrawObjCreator'):
            tag = name.removeprefix('DrawObjCreator').upper()
            self.put(_shape(tag, items['Width'], items['Height'], bool(items['TreatAsChar'])))
            self.selection = (0, 0)  # the new object stays selected until Cancel
        elif name == 'Cancel':
            self.selection = None
        return True

    def commit(self) -> None:
        pending, self.pending = self.pending, None
        text = _char(pending['text']) if pending['text'] else ''
        note_caret, self.caret = self.caret, pending['body']
        tag = pending['tag']
        if tag == 'MEMO':
            self.put(f'<FIELDBEGIN Type="Memo"><SUBLIST><P><TEXT>{text}</TEXT></P></SUBLIST></FIELDBEGIN>')
            self.paras[self.caret[1]].insert(self.caret[2] + 1, '<FIELDEND Type="Memo"/>')
        elif tag in ('HEADER', 'FOOTER'):
            self.put(f'<{tag} ApplyPageType="{pending["apply"]}"><PARALIST><P><TEXT>{text}</TEXT></P></PARALIST></{tag}>', at=0)
        else:
            self.put(f'<{tag}><PARALIST><P><TEXT><AUTONUM Number="1"/>{text}</TEXT></P></PARALIST></{tag}>')
        if self.no_return:
            self.caret = note_caret

    def Run(self, name: str) -> bool:  # noqa: N802 - plain hwp.Run must never be used while HAction.Run exists
        self.plain_run_calls.append(name)
        return self.native(name, None)

    def insert_text(self, text: str) -> None:
        self.log.append('insert_text')
        if self.pending is not None:
            self.pending['text'] += text
        else:
            self.paras[self.caret[1]][self.caret[2]:self.caret[2]] = list(text)

    def GetTextFile(self, fmt: str, option: str) -> str:  # noqa: N802
        assert (fmt, option) == ('HWPML2X', '')
        self.readbacks += 1
        return '<HWPML><BODY>' if self.bad_readback else self.xml()

    def get_pos(self) -> tuple[int, int, int]:
        return tuple(self.caret)  # type: ignore[return-value]

    def get_selected_pos(self) -> tuple[Any, ...]:
        if self.selection is None:
            return (False, 0, 0, 0, 0, 0, 0)
        return (True, 0, self.caret[1], self.selection[0], 0, self.caret[1], self.selection[1])

    def get_selected_text(self, keep_select: bool = False) -> str:
        start, end = self.selection or (0, 0)
        return ''.join(token for token in self.paras[self.caret[1]][start:end] if len(token) == 1)

    def select(self, start: int, end: int) -> None:
        self.selection = (start, end)
        self.caret = [0, self.caret[1], end]


class _Service(LocalCliObjectInsertMixin):
    def __init__(self, hwp: _FakeHwp) -> None:
        self.hwp = hwp

    def _bundle_compact_snapshot(self, _hwp: Any) -> dict[str, Any]:
        if self.hwp.snapshot_unknown:
            return {'pos': list(self.hwp.caret), 'selection_mode': None}
        return {'pos': list(self.hwp.caret), 'is_cell': False, 'has_selection': self.hwp.selection is not None, 'selection_mode': 0}


def _run(hwp: _FakeHwp, **fields: Any) -> dict[str, Any]:
    step = {'op': 'object_insert_exact', 'kind': 'footnote', 'text': 'note', 'expected_pos': list(hwp.caret), 'confirm_mutation': True}
    step.update(fields)
    step = {key: value for key, value in step.items() if value is not None}
    return _Service(hwp)._bundle_object_insert_exact(hwp, step)


SUCCESS_CASES: tuple[tuple[dict[str, Any], list[str], str], ...] = (
    ({'kind': 'footnote'}, ['InsertFootnote', 'insert_text', 'CloseEx'], '<FOOTNOTE>'),
    ({'kind': 'endnote'}, ['InsertEndnote', 'insert_text', 'CloseEx'], '<ENDNOTE>'),
    ({'kind': 'memo', 'text': 'check this'}, ['InsertFieldMemo', 'insert_text', 'CloseEx'], 'Type="Memo"'),
    ({'kind': 'bookmark', 'text': None, 'name': '책갈피1'}, ['GetDefault(Bookmark)', 'Execute(Bookmark)'], 'Name="책갈피1"'),
    ({'kind': 'equation', 'text': None, 'script': 'a over b'}, ['GetDefault(EquationCreate)', 'Execute(EquationCreate)'], '<SCRIPT>a over b</SCRIPT>'),
    ({'kind': 'line', 'text': None, 'width_mm': 30, 'height_mm': 0},
     ['GetDefault(DrawObjCreatorLine)', 'Execute(DrawObjCreatorLine)', 'Cancel'], '<LINE>'),
    ({'kind': 'rectangle', 'text': None, 'width_mm': 30, 'height_mm': 10},
     ['GetDefault(DrawObjCreatorRectangle)', 'Execute(DrawObjCreatorRectangle)', 'Cancel'], '<RECTANGLE>'),
    ({'kind': 'ellipse', 'text': None, 'width_mm': 30, 'height_mm': 10, 'treat_as_char': False},
     ['GetDefault(DrawObjCreatorEllipse)', 'Execute(DrawObjCreatorEllipse)', 'Cancel'], 'TreatAsChar="false"'),
    ({'kind': 'header', 'text': 'Top', 'apply_to': 'even'},
     ['GetDefault(HeaderFooter)', 'Execute(HeaderFooter)', 'insert_text', 'CloseEx'], '<HEADER ApplyPageType="Even">'),
    ({'kind': 'footer', 'text': 'Bottom'},
     ['GetDefault(HeaderFooter)', 'Execute(HeaderFooter)', 'insert_text', 'CloseEx'], '<FOOTER ApplyPageType="Both">'),
)


class ServiceFlowTests(unittest.TestCase):
    def test_each_kind_runs_once_and_verifies(self) -> None:
        for fields, native, marker in SUCCESS_CASES:
            with self.subTest(kind=fields['kind']):
                hwp = _FakeHwp()
                result = _run(hwp, **fields)
                self.assertTrue(result['succeeded'])
                self.assertTrue(result['verification']['ok'], result['verification'])
                self.assertEqual(hwp.log, native)
                self.assertEqual(hwp.plain_run_calls, [])
                self.assertIn(marker, hwp.xml())
                self.assertEqual(hwp.readbacks, 2)
                self.assertNotIn('text', result['plan'])
                typed = fields['kind'] in ('footnote', 'endnote', 'memo', 'header', 'footer')
                self.assertEqual(result['undo']['native_editing_actions'], 2 if typed else 1)
                self.assertIs(result['undo']['single_undo_expected'], not typed)
                self.assertIs(result['undo']['verified_natively'], False)

    def test_hyperlink_wraps_the_proven_selection(self) -> None:
        hwp = _FakeHwp()
        hwp.select(6, 11)
        result = _run(hwp, kind='hyperlink', text=None, url='https://example.com/x?a=1', display_text='world')
        self.assertTrue(result['verification']['ok'], result['verification'])
        self.assertEqual(hwp.log, ['GetDefault(InsertHyperlink)', 'Execute(InsertHyperlink)'])
        self.assertIn('Command="https://example.com/x?a=1;1;0;0;"', hwp.xml())
        self.assertEqual(result['pre_mutation_proof']['selected_range'], [True, 0, 0, 6, 0, 0, 11])

    def test_memo_accepts_a_live_selection(self) -> None:
        hwp = _FakeHwp()
        hwp.select(0, 5)
        self.assertTrue(_run(hwp, kind='memo', text='m')['verification']['ok'])

    def test_refusals_before_mutation_perform_no_native_action(self) -> None:
        header = _FakeHwp()
        header.paras[0].insert(0, '<HEADER ApplyPageType="Both"><PARALIST><P><TEXT><CHAR>h</CHAR></TEXT></P></PARALIST></HEADER>')
        header.caret = [0, 0, 6]
        selected = _FakeHwp()
        selected.select(0, 5)
        cases = (
            (_FakeHwp(no_readback=True), {}, 'GetTextFile is unavailable'),
            (_FakeHwp(bad_readback=True), {}, 'not well-formed HWPML'),
            (_FakeHwp(snapshot_unknown=True), {}, 'not provably in normal edit state'),
            (_FakeHwp(snapshot_unknown=True), {'kind': 'memo'}, 'not provably known'),
            (selected, {}, 'not provably in normal edit state'),
            (_FakeHwp(), {'expected_pos': [0, 0, 4]}, 'caret is at [0, 0, 5], expected [0, 0, 4]'),
            (_FakeHwp(), {'expected_pos': [0, 1, 5]}, 'expected [0, 1, 5]'),
            (header, {'kind': 'header', 'text': 'x'}, 'replacing an existing header'),
            (_FakeHwp(), {'kind': 'hyperlink', 'text': None, 'url': 'https://a.b', 'display_text': 'Hello'}, 'needs a live selection'),
            (_FakeHwp(), {'confirm_mutation': None}, 'confirm_mutation'),
        )
        for hwp, fields, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(LocalCliRuntimeError) as caught:
                    _run(hwp, **fields)
                self.assertNotIsInstance(caught.exception, LocalCliMutationError)
                self.assertIn(message, str(caught.exception))
                self.assertEqual([entry for entry in hwp.log if not entry.startswith('GetDefault')], [])

    def test_hyperlink_selection_mismatch_is_refused(self) -> None:
        for start, end, expected_pos in ((0, 5, None), (6, 10, None), (6, 11, [0, 0, 10])):
            with self.subTest(selection=(start, end)):
                hwp = _FakeHwp()
                hwp.select(start, end)
                with self.assertRaises(LocalCliRuntimeError) as caught:
                    _run(hwp, kind='hyperlink', text=None, url='https://a.b', display_text='world',
                         expected_pos=expected_pos or list(hwp.caret))
                self.assertNotIsInstance(caught.exception, LocalCliMutationError)
                self.assertNotIn('Execute(InsertHyperlink)', hwp.log)

    def test_unverified_results_report_possible_mutation(self) -> None:
        cases = (
            ({'noop': {'InsertFootnote'}}, {}, 'FOOTNOTE count changed by 0'),
            ({'noop': {'Execute(Bookmark)', 'Bookmark'}}, {'kind': 'bookmark', 'text': None, 'name': 'b'}, 'BOOKMARK count changed by 0'),
            ({'wrong_tag': True}, {}, 'FOOTNOTE count changed by 0'),
            ({'extra_control': True}, {}, "other controls changed: {'BOOKMARK': 1}"),
            ({'extra_control': True}, {'kind': 'equation', 'text': None, 'script': 'x'}, "other controls changed: {'BOOKMARK': 1}"),
            ({'eat_char': True}, {'kind': 'rectangle', 'text': None, 'width_mm': 5, 'height_mm': 5}, 'document text outside the new object changed'),
            ({'no_return': True}, {}, 'could not return to the original list'),
        )
        for faults, fields, reason in cases:
            with self.subTest(faults=faults, kind=fields.get('kind', 'footnote')):
                hwp = _FakeHwp(**faults)
                with self.assertRaises(LocalCliMutationError) as caught:
                    _run(hwp, **fields)
                self.assertTrue(caught.exception.mutation_may_have_persisted)
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(caught.exception.rollback['attempted'], False)
                actions = caught.exception.rollback['undo']['native_editing_actions']
                self.assertGreaterEqual(actions, 1)
                if actions > 1:
                    self.assertIn('reopen the working copy', caught.exception.rollback['hint'])

    def test_raising_native_action_is_not_retried(self) -> None:
        for fields, action in (({}, 'InsertFootnote'), ({'kind': 'bookmark', 'text': None, 'name': 'b'}, 'Bookmark')):
            with self.subTest(action=action):
                hwp = _FakeHwp(raising={action})
                with self.assertRaises(LocalCliMutationError) as caught:
                    _run(hwp, **fields)
                self.assertTrue(caught.exception.mutation_may_have_persisted)
                self.assertIn('outcome_unknown', str(caught.exception))
                self.assertEqual([entry for entry in hwp.log if not entry.startswith('GetDefault')], [f'{action}!raised' if fields == {} else f'Execute({action})!raised'])
                self.assertEqual(hwp.plain_run_calls, [])
                self.assertEqual(hwp.readbacks, 1)

    def test_plain_run_is_used_only_without_haction_run(self) -> None:
        hwp = _FakeHwp()
        hwp.HAction.Run = None  # type: ignore[method-assign]
        self.assertTrue(_run(hwp)['verification']['ok'])
        self.assertEqual(hwp.plain_run_calls, ['InsertFootnote', 'CloseEx'])


if __name__ == '__main__':
    unittest.main()
