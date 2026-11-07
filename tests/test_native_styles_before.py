"""ACTUAL HWPML2X strings Hancom returned in earlier native runs (tests/fixtures), through the real style model, inventory and
command preconditions.

No style command was ever run natively on these documents, so there is no after state: each replay below stops at the
first native call a command would make next (the block selection for apply, CreateAction for create, and for a change the
IHwpObject.CharShape read at the style's use once the caret stands there), which the recorded object answers with NotCaptured,
never with an invented result. The caret and selection are the recorded
object's own. This is eligibility evidence for the actual before states, not style-edit, save or render evidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import unittest

from app import native_styles
from app.local_cli_bundle_styles import LocalCliBundleStylesMixin
from tests.test_native_paragraph_edit import Flow

FIXTURES = Path(__file__).parent / 'fixtures'
CAPTURES = sorted(FIXTURES.glob('**/*.hwpml2x'))


def captured(name: str) -> str:
    # Strict UTF-8: the string keeps its in-memory UTF-16 declaration, which is not a byte decoding (app.hwpx_saved_proof.producer_text).
    return (FIXTURES / name).read_text(encoding='utf-8')


class NotCaptured(RuntimeError):
    """The native session that returned this text made no such call, so its answer is not known."""


class _Typed:
    def __init__(self, owner: 'RecordedHwp', name: str) -> None:
        self.owner, self.name, self.HSet = owner, name, self


class _Params:
    def __init__(self, owner: 'RecordedHwp') -> None:
        self.HStyle = _Typed(owner, 'HStyle')


class _HAction:
    def __init__(self, owner: 'RecordedHwp') -> None:
        self.owner = owner

    def GetDefault(self, name: str, hset: Any) -> Any:  # noqa: N802
        self.owner.calls.append(('GetDefault', name, hset.name))
        raise NotCaptured(f'GetDefault({name})')

    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802
        self.owner.calls.append(('Execute', name))
        raise NotCaptured(f'Execute({name})')

    def Run(self, name: str) -> Any:  # noqa: N802
        raise NotCaptured(f'Run({name})')


class RecordedHwp:
    """The captured document text, a caret of its own and nothing else."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple] = []
        self.cursor = (0, 0, 0)
        self.IsModified = False
        self.PageCount = 1
        self.HAction = _HAction(self)
        self.HParameterSet = _Params(self)

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        self.calls.append(('get_text_file', fmt, option))
        if (fmt, option) != ('HWPML2X', ''):
            raise NotCaptured(f'get_text_file({fmt!r}, {option!r})')
        return self.text

    def get_pos(self) -> tuple[int, int, int]:
        return self.cursor

    def get_selected_pos(self) -> tuple:
        return (False, None, None, None, None, None, None)

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.calls.append(('set_pos', list_id, para, pos))
        self.cursor = (list_id, para, pos)
        return True

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.calls.append(('select_text', spara, spos, epara, epos, slist))
        raise NotCaptured('select_text')

    def CreateAction(self, name: str) -> Any:  # noqa: N802
        self.calls.append(('CreateAction', name))
        raise NotCaptured(f'CreateAction({name})')

    @property
    def CharShape(self) -> Any:  # noqa: N802
        self.calls.append(('CharShape',))
        raise NotCaptured('CharShape')


def run(hwp: RecordedHwp, **fields) -> dict:
    return Flow(hwp).step(**fields)


def listed(hwp: RecordedHwp, **fields) -> dict:
    outcome = run(hwp, op='style_inventory', **fields)
    assert outcome['ok'], outcome
    return outcome['steps'][0]['result']


def stopped(test: unittest.TestCase, outcome: dict, hwp: RecordedHwp) -> dict:
    """The command stopped at a native call the capture cannot answer: an unknown outcome before any write."""
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertNotIn('refusal_code', step, 'it was not refused: every precondition held on the actual text')
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertNotIn('Execute', [call[0] for call in hwp.calls])
    return step


class EveryCapture(unittest.TestCase):
    def test_every_capture_gives_a_style_model_whose_paragraphs_are_the_censuss(self) -> None:
        self.assertEqual(len(CAPTURES), 32)
        for path in CAPTURES:
            with self.subTest(capture=str(path.relative_to(FIXTURES))):
                side = LocalCliBundleStylesMixin()._styles_read_state(RecordedHwp(path.read_text(encoding='utf-8')))()  # noqa: SLF001
                model = side['styles']
                self.assertGreaterEqual(len(model['styles']), 1)
                self.assertEqual([item['text'] for item in model['paragraphs'] if item['area'] is None], [item['text'] for item in side['census']['paragraphs']])
                self.assertEqual(len(model['areas']), len(side['census']['header_footer']), 'the same headers and footers as the census')
                self.assertTrue(all(item['area'] in {area['id'] for area in model['areas']} for item in model['paragraphs'] if item['area'] is not None))


class Inventories(unittest.TestCase):
    def test_the_built_in_list_of_a_native_document(self) -> None:
        result = listed(RecordedHwp(captured('native_fields/before.hwpml2x')))
        self.assertEqual((result['count'], result['paragraph_styles'], result['character_styles'], result['duplicate_names']), (23, 21, 2, []))
        self.assertEqual([item['name'] for item in result['styles']],
                         ['바탕글', '본문', *[f'개요 {number}' for number in range(1, 11)], '쪽 번호', '줄 번호', '머리말', '각주', '미주', '메모', '차례 제목',
                          '차례 1', '차례 2', '차례 3', '캡션'])
        rows = {item['name']: item for item in result['styles']}
        self.assertEqual([rows[f'개요 {number}']['numbering'] for number in (1, 10)], [{'kind': 'outline', 'level': 1}, {'kind': 'outline', 'level': 10}])
        self.assertEqual((rows['쪽 번호']['kind'], rows['쪽 번호']['uses'], rows['쪽 번호']['paragraph']), ('character', {'recorded': False}, None))
        self.assertEqual(rows['바탕글']['character'], {'font_family': '함초롬바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000'})
        self.assertEqual((rows['바탕글']['uses']['total'], rows['본문']['uses']['total']), (1, 0))

    def test_a_converted_document_with_missing_english_names_and_header_and_footer_uses(self) -> None:
        result = listed(RecordedHwp(captured('picture_native_preedit.hwpml2x')))
        self.assertEqual((result['count'], result['paragraph_styles'], result['character_styles']), (64, 37, 27))
        rows = {item['name']: item for item in result['styles']}
        self.assertIsNone(rows['Heading 9 Char']['english_name'])
        self.assertEqual(rows['header']['uses']['by_context']['header'], 1)
        self.assertEqual(rows['footer']['uses']['by_context']['footer'], 1)
        title = rows['Title']['uses']
        self.assertEqual((title['total'], title['plain'], title['rows'][0]['context']), (1, 0, 'body'), 'its paragraph has a format of its own')

    def test_a_renumbered_character_shape_is_not_a_style_change(self) -> None:
        """Measured: in this pair Hancom renumbered the character shape 바탕글 names (CharShape 0 to 1) with the same content."""
        before, after = (native_styles.parse_style_model(captured(f'native_paragraph_edit/page_{side}.hwpml2x')) for side in ('before', 'after'))
        raw = [dict(item['settings']) for item in before['styles']]
        self.assertNotIn('charshape', raw[0], 'the shape is compared by content, not by its number')
        self.assertIn('CharShape="0"', captured('native_paragraph_edit/page_before.hwpml2x').split('<STYLE ', 1)[1].split('/>', 1)[0])
        self.assertIn('CharShape="1"', captured('native_paragraph_edit/page_after.hwpml2x').split('<STYLE ', 1)[1].split('/>', 1)[0])
        self.assertEqual((before['styles'][0]['hash'], before['styles_hash']), (after['styles'][0]['hash'], after['styles_hash']))


class Preconditions(unittest.TestCase):
    """On an actual document whose nine paragraphs all show 바탕글: each command binds, finds its place and stops at its first native call."""

    TEXT = 'native_numbering/native13/raw-1.hwpml2x'

    def test_apply_binds_the_paragraph_and_stops_at_the_block_selection(self) -> None:
        hwp = RecordedHwp(captured(self.TEXT))
        found = next(item for item in listed(hwp, name='개요 1')['styles'])
        stopped(self, run(hwp, op='style_apply_exact', style_name='개요 1', expected_style_hash=found['hash'], paragraph_index=2, expected_text='N2L2A',
                          expected_page=1, confirm_layout=True), hwp)
        selections = [call for call in hwp.calls if call[0] == 'select_text']
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0][1:], (2, 0, 2, 5, 0), "the block is the paragraph's own five characters in the body list")

    def test_create_binds_its_base_and_stops_at_the_create_action(self) -> None:
        hwp = RecordedHwp(captured(self.TEXT))
        result = listed(hwp)
        based = result['styles'][0]
        stopped(self, run(hwp, op='style_create_exact', name='보고서 본문', kind='paragraph', based_on='바탕글', expected_based_on_hash=based['hash'],
                          expected_styles_hash=result['styles_hash'], bold=True, confirm_layout=True), hwp)
        self.assertEqual([call for call in hwp.calls if call[0] in {'CreateAction', 'set_pos', 'GetDefault'}], [('CreateAction', 'StyleAddNew')],
                         'the action is made before the caret moves')
        side = LocalCliBundleStylesMixin()._styles_read_state(RecordedHwp(captured(self.TEXT)))()  # noqa: SLF001
        self.assertEqual(native_styles._stand(side, side['styles']['styles'][0], 'the base style')[:2], (0, 0),  # noqa: SLF001
                         'the caret would stand in the first body paragraph, which shows 바탕글 exactly')

    def test_update_binds_every_use_and_stops_at_the_change_action(self) -> None:
        hwp = RecordedHwp(captured(self.TEXT))
        found = listed(hwp, name='바탕글')['styles'][0]
        self.assertEqual((found['uses']['total'], found['uses']['plain'], found['uses']['rows_complete']), (9, 9, True))
        stopped(self, run(hwp, op='style_update_exact', style_name='바탕글', expected_style_hash=found['hash'], expected_uses=9,
                          expected_uses_hash=found['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True), hwp)
        native = [call for call in hwp.calls if call[0] in {'CreateAction', 'set_pos', 'GetDefault', 'CharShape'}]
        self.assertEqual([call[0] for call in native], ['set_pos', 'CharShape'], "the caret at the style's use, then the shape read, before any action")

    def test_update_of_the_converted_document_is_refused_for_its_own_formats(self) -> None:
        hwp = RecordedHwp(captured('picture_native_preedit.hwpml2x'))
        found = listed(hwp, name='Normal')['styles'][0]
        outcome = run(hwp, op='style_update_exact', style_name='Normal', expected_style_hash=found['hash'], expected_uses=found['uses']['total'],
                      expected_uses_hash=found['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True)
        self.assertEqual(outcome['steps'][0].get('refusal_code'), 'style_use_has_own_format')
        self.assertEqual([call for call in hwp.calls if call[0] in {'CreateAction', 'GetDefault', 'Execute', 'select_text'}], [])


if __name__ == '__main__':
    unittest.main()
