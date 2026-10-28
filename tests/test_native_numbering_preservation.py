"""Preservation regressions for native page numbers (N1) and official outline numbering (N2).

Every case runs through the real HWPML2X census reader (live_protection_reader) against a SYNTHETIC document.
The HWPML2X element and attribute names are not verified against Hancom output; these tests prove the
source-level preservation contract only, not native behaviour.
"""
from __future__ import annotations

import unittest
from typing import Any, Callable

from app import native_numbering as nn
from app import native_numbering_census as census_module
from app.edit_ops import apply_edit_operations
from app.native_numbering_census import live_protection_reader
from app.worker import _apply_edit_operations_with_optional_step_recorder
from tests.native_numbering_fakes import FakeCtrl, OFFICIAL_SHAPE_LEVELS, STOCK_TAIL_LEVELS, shape_items, stock_shape
from tests.test_native_numbering_bundle_translation import NO_CLEANUP, NUMBERING_STEP, PAGE_STEP, _run_bundle
from tests.test_native_numbering_census import _HwpmlHwp, hwpml

POSITION = {'position': 'bottom-center'}
TEXTS = ('Title', 'Item text', 'Tail')


def make_doc(*, controls: list[FakeCtrl] | None = None, extras: dict[str, Any] | None = None, texts: tuple[str, ...] = TEXTS) -> _HwpmlHwp:
    paragraphs = [{'text': text, 'heading_type': 0, 'level': 0, 'section': 0} for text in texts]
    hwp = _HwpmlHwp(paragraphs=paragraphs, controls=controls or [FakeCtrl('secd')])
    hwp.hwpml_extras = dict(extras or {})
    return hwp


def foreign_shape() -> dict[str, Any]:
    shape = stock_shape()
    shape.update(shape_items((('(가)', 3), ('(1)', 0), ('①', 0), ('A.', 0))))
    return shape


def after_each_native_call(hwp: _HwpmlHwp, mutate: Callable[[_HwpmlHwp], None]) -> None:
    for name in ('Execute', 'Run'):
        original = getattr(hwp.HAction, name)

        def wrapped(*args: Any, _original=original, **kwargs: Any) -> Any:
            result = _original(*args, **kwargs)
            mutate(hwp)
            return result

        setattr(hwp.HAction, name, wrapped)


def protection_of(result: dict[str, Any]) -> dict[str, Any]:
    return result['protection'] if 'protection' in result else result['result']['protection']


def mutating_calls(hwp: _HwpmlHwp) -> list[tuple[Any, ...]]:
    return [call for call in hwp.calls if call[0] in {'Execute', 'Run'}]


def paragraph_shape_executes(hwp: _HwpmlHwp) -> list[tuple[Any, ...]]:
    return [call for call in hwp.calls if call[0] == 'Execute' and call[1] == 'ParagraphShape']


def _update(**values: Any) -> Callable[[_HwpmlHwp], None]:
    return lambda hwp: hwp.hwpml_extras.update(values)


# (label, initial HWPML extras, change applied by the "native call")
STRUCTURE_CORRUPTIONS: list[tuple[str, dict[str, Any], Callable[[_HwpmlHwp], None]]] = [
    ('character-shape-height', {}, _update(char_height=2000)),
    ('unrelated-paragraph-format', {}, _update(margins={'Tail': 2000})),
    ('unrelated-style', {'extra_styles': ('Keep',)}, _update(extra_styles=('Changed',))),
    ('unrelated-numbering', {'extra_numberings': ('^A',)}, _update(extra_numberings=('^B',))),
    ('picture-width', {'picture_width': 1000}, _update(picture_width=2000)),
    ('table-width', {'table_width': 1000}, _update(table_width=2000)),
    ('media-payload', {}, lambda hwp: hwp.package_state.update(bindata=['changed-media'])),
]

# (label, initial HWPML extras, text transform)
UNPROVABLE_STATES: list[tuple[str, dict[str, Any], Callable[[str], str]]] = [
    ('style-list-unrecognized', {}, lambda text: text.replace('STYLELIST', 'XSTYLELIST')),
    ('character-shape-list-unrecognized', {}, lambda text: text.replace('CHARSHAPELIST', 'XCHARSHAPELIST')),
    ('paragraph-shape-list-unrecognized', {}, lambda text: text.replace('PARASHAPELIST', 'XPARASHAPELIST')),
    ('unresolvable-character-shape-reference', {}, lambda text: text.replace('<TEXT CharShape="1">', '<TEXT CharShape="99">', 1)),
    ('unresolvable-style-reference', {}, lambda text: text.replace('Style="0"', 'Style="77"', 1)),
    ('unresolvable-media-reference', {'picture_width': 1000}, lambda text: text.replace('BinItem="1"', 'BinItem="9"')),
]


class CensusV2Tests(unittest.TestCase):
    PARAS = [('Title', 0, 0), ('Item', 0, 0), ('Tail', 0, 0)]

    def _census(self, **kwargs: Any) -> dict[str, Any]:
        return census_module.parse_hwpml2x_census(hwpml(self.PARAS, **kwargs))

    def test_definition_lists_are_kept_as_id_independent_item_digests(self) -> None:
        census = self._census()
        for name in ('charshapelist', 'parashapelist', 'stylelist', 'numberinglist', 'bindatalist'):
            self.assertTrue(census['definitions'][name], msg=name)
        renumbered = hwpml(self.PARAS).replace('CHARSHAPE Id="1"', 'CHARSHAPE Id="7"').replace('CharShape="1"', 'CharShape="7"')
        again = census_module.parse_hwpml2x_census(renumbered)
        self.assertEqual(again['definitions'], census['definitions'])
        self.assertEqual([p['sha256'] for p in again['paragraphs']], [p['sha256'] for p in census['paragraphs']])

    def test_character_and_paragraph_shape_properties_reach_the_digests(self) -> None:
        base = self._census()
        taller = self._census(char_height=2000)
        self.assertNotEqual(base['definitions']['charshapelist'], taller['definitions']['charshapelist'])
        self.assertNotEqual([p['sha256'] for p in base['paragraphs']], [p['sha256'] for p in taller['paragraphs']])
        indented = self._census(margins={'Tail': 500})
        self.assertNotEqual(base['paragraphs'][2]['sha256'], indented['paragraphs'][2]['sha256'])
        self.assertEqual(base['paragraphs'][1]['sha256'], indented['paragraphs'][1]['sha256'])

    def test_picture_and_table_geometry_and_media_references_reach_the_digests(self) -> None:
        for key in ('picture_width', 'table_width'):
            left, right = self._census(**{key: 1000}), self._census(**{key: 2000})
            self.assertNotEqual(left['paragraphs'][0]['sha256'], right['paragraphs'][0]['sha256'], msg=key)
            self.assertNotEqual(left['frame'], None)
            self.assertEqual(left['paragraphs'][1]['sha256'], right['paragraphs'][1]['sha256'], msg=key)

    def test_page_number_elements_are_stripped_only_in_the_stripped_digest(self) -> None:
        base, numbered = self._census(), self._census(pagenum=True)
        self.assertNotEqual(base['paragraphs'][0]['sha256'], numbered['paragraphs'][0]['sha256'])
        self.assertEqual(base['paragraphs'][0]['stripped_sha256'], numbered['paragraphs'][0]['stripped_sha256'])
        self.assertEqual(base['frame']['stripped_sha256'], numbered['frame']['stripped_sha256'])

    def test_heading_fields_are_masked_only_in_the_masked_digest(self) -> None:
        plain = census_module.parse_hwpml2x_census(hwpml([('Title', 0, 0), ('Item', 0, 0), ('Tail', 0, 0)]))
        outline = census_module.parse_hwpml2x_census(hwpml([('Title', 0, 0), ('Item', 1, 2), ('Tail', 0, 0)]))
        self.assertNotEqual(plain['paragraphs'][1]['sha256'], outline['paragraphs'][1]['sha256'])
        self.assertEqual(plain['paragraphs'][1]['masked_sha256'], outline['paragraphs'][1]['masked_sha256'])

    def test_unrecognized_mandatory_lists_and_unresolvable_references_are_not_complete(self) -> None:
        for label, _extras, transform in UNPROVABLE_STATES:
            text = transform(hwpml(self.PARAS, picture_width=1000))
            census = census_module.parse_hwpml2x_census(text)
            definitions = census['definitions']
            mandatory = ('charshapelist', 'parashapelist', 'stylelist')
            complete = (
                definitions is not None
                and all(definitions.get(name) for name in mandatory)
                and census['paragraphs'] is not None
                and census['frame'] is not None
            )
            self.assertFalse(complete, msg=label)


class PageNumberPreservationTests(unittest.TestCase):
    def _insert(self, hwp: _HwpmlHwp) -> dict[str, Any]:
        return nn.apply_page_number(hwp, POSITION, protection=live_protection_reader(hwp))

    def test_ordinary_positive_control_reports_complete_proof(self) -> None:
        hwp = make_doc(extras={'picture_width': 1000, 'table_width': 1000})
        result = self._insert(hwp)
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)
        protection = result['protection']
        self.assertTrue(protection['checked'])
        self.assertEqual(protection['unproven'], [])
        self.assertEqual(protection['page_number_proof'], 'control_chain')
        for field in ('definitions', 'paragraphs', 'frame', 'header_footer'):
            self.assertIn(field, protection['compared'])

    def test_positive_control_with_header_still_inserts(self) -> None:
        hwp = make_doc(controls=[FakeCtrl('secd'), FakeCtrl('head')])
        hwp.package_state['header_footer'] = ['HEADER']
        self.assertEqual(self._insert(hwp)['status'], 'inserted')

    def test_the_new_native_page_number_element_is_the_only_allowed_document_change(self) -> None:
        hwp = make_doc()
        after_each_native_call(hwp, _update(pagenum=True))
        self.assertEqual(self._insert(hwp)['status'], 'inserted')

        hwp = make_doc()
        after_each_native_call(hwp, _update(pagenum=True, picture_width=500))
        with self.assertRaises(nn.NativeNumberingMutationError):
            self._insert(hwp)

    def test_each_structure_corruption_is_an_unknown_outcome_and_is_kept(self) -> None:
        for label, initial, mutate in STRUCTURE_CORRUPTIONS:
            with self.subTest(label):
                hwp = make_doc(extras=initial)
                after_each_native_call(hwp, mutate)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    self._insert(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertEqual(ctx.exception.rollback, NO_CLEANUP)
                self.assertEqual(len(mutating_calls(hwp)), 1)
                self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)

    def test_unknown_mandatory_evidence_refuses_before_any_mutation(self) -> None:
        for label, initial, transform in UNPROVABLE_STATES:
            with self.subTest(label):
                hwp = make_doc(extras=initial)
                hwp.hwpml_text = transform(hwp.get_text_file('HWPML2X', ''))
                with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                    self._insert(hwp)
                self.assertEqual(ctx.exception.code, 'preservation_unprovable')
                self.assertEqual(mutating_calls(hwp), [])

    def test_proof_lost_after_the_call_is_an_unknown_outcome_not_an_insertion(self) -> None:
        for label, initial, transform in UNPROVABLE_STATES:
            with self.subTest(label):
                hwp = make_doc(extras=initial)

                def lose_proof(doc: _HwpmlHwp, _transform=transform) -> None:
                    doc.hwpml_text = None
                    doc.hwpml_text = _transform(doc.get_text_file('HWPML2X', ''))

                after_each_native_call(hwp, lose_proof)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    self._insert(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)


class OfficialNumberingPreservationTests(unittest.TestCase):
    def _number(self, hwp: _HwpmlHwp, *, level: int = 1, find: str = 'Item text') -> dict[str, Any]:
        return nn.apply_official_numbering_at_match(hwp, find=find, level=level, protection=live_protection_reader(hwp))

    def test_ordinary_positive_control_numbers_only_the_target_with_complete_proof(self) -> None:
        hwp = make_doc(extras={'picture_width': 1000, 'table_width': 1000})
        result = self._number(hwp, level=2)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual([p['text'] for p in hwp.paragraphs], list(TEXTS))
        protection = protection_of(result)
        self.assertTrue(protection['checked'])
        self.assertEqual(protection['unproven'], [])
        for field in ('definitions', 'paragraphs', 'frame', 'header_footer'):
            self.assertIn(field, protection['compared'])

    def test_unnumbered_paragraph_uses_one_native_paragraph_shape_action_for_every_level(self) -> None:
        for level in (1, 2, 3, 4):
            with self.subTest(level):
                hwp = make_doc()
                result = self._number(hwp, level=level)
                executes = paragraph_shape_executes(hwp)
                self.assertEqual(len(mutating_calls(hwp)), 1)
                self.assertEqual(len(executes), 1)
                self.assertEqual((executes[0][2]['HeadingType'], executes[0][2]['Level']), (1, level - 1))
                self.assertEqual([c for c in hwp.calls if c[0] == 'Run'], [])
                self.assertEqual(result['native_actions'], [f'Execute:ParagraphShape(HeadingType=1,Level={level - 1})'])
                self.assertEqual(hwp.paragraphs[1]['level'], level - 1)

    def test_existing_outline_paragraph_level_change_is_one_level_only_action(self) -> None:
        hwp = make_doc()
        hwp.paragraphs[1].update(heading_type=1, level=0)
        result = self._number(hwp, level=4)
        self.assertEqual(len(mutating_calls(hwp)), 1)
        self.assertEqual(paragraph_shape_executes(hwp)[0][2]['Level'], 3)
        self.assertEqual(result['native_actions'], ['Execute:ParagraphShape(Level=3)'])

    def test_each_structure_corruption_during_numbering_is_an_unknown_outcome_and_is_kept(self) -> None:
        for label, initial, mutate in STRUCTURE_CORRUPTIONS:
            with self.subTest(label):
                hwp = make_doc(extras=initial)
                after_each_native_call(hwp, mutate)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    self._number(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertEqual(ctx.exception.rollback, NO_CLEANUP)
                self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])

    def test_unknown_mandatory_evidence_refuses_before_any_mutation(self) -> None:
        for label, initial, transform in UNPROVABLE_STATES:
            with self.subTest(label):
                hwp = make_doc(extras=initial)
                hwp.hwpml_text = transform(hwp.get_text_file('HWPML2X', ''))
                with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                    self._number(hwp)
                self.assertEqual(ctx.exception.code, 'preservation_unprovable')
                self.assertEqual(mutating_calls(hwp), [])

    def test_proof_lost_after_the_call_is_an_unknown_outcome_not_an_application(self) -> None:
        for label, initial, transform in UNPROVABLE_STATES:
            with self.subTest(label):
                hwp = make_doc(extras=initial)

                def lose_proof(doc: _HwpmlHwp, _transform=transform) -> None:
                    doc.hwpml_text = None
                    doc.hwpml_text = _transform(doc.get_text_file('HWPML2X', ''))

                after_each_native_call(hwp, lose_proof)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    self._number(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)
                self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])

    def test_target_must_be_uniquely_identified_in_the_census_before_any_mutation(self) -> None:
        hwp = make_doc(texts=('Title', 'Item text', 'Item two', 'Tail'))
        for find in (None, 'Item'):
            with self.subTest(find):
                with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                    nn.apply_official_numbering(hwp, {'level': 1}, protection=live_protection_reader(hwp), target_find=find)
                self.assertEqual(ctx.exception.code, 'target_unidentifiable')
                self.assertEqual(mutating_calls(hwp), [])

    def test_requested_definition_change_is_the_only_allowed_definition_change(self) -> None:
        hwp = make_doc()
        hwp.outline_shape = foreign_shape()
        result = self._number(hwp, level=2)
        self.assertEqual(result['numbering_format']['status'], 'defined_in_current_section')
        self.assertEqual(protection_of(result)['unproven'], [])
        self.assertEqual(len([c for c in hwp.calls if c[0] == 'Execute' and c[1] == 'OutlineNumber']), 1)
        self.assertEqual(len(paragraph_shape_executes(hwp)), 1)
        self.assertEqual([c for c in hwp.calls if c[0] == 'Run'], [])

    def test_definition_changes_beyond_the_requested_levels_are_unknown_outcomes(self) -> None:
        cases: list[tuple[str, dict[str, Any], Callable[[_HwpmlHwp], None]]] = [
            ('level-five-head', {}, lambda hwp: hwp.outline_shape.update(StrFormatLevel4='changed')),
            ('unrelated-numbering', {'extra_numberings': ('^A',)}, _update(extra_numberings=('^B',))),
            ('character-shape-height', {}, _update(char_height=2000)),
            ('unrelated-paragraph-format', {}, _update(margins={'Tail': 2000})),
            ('unrelated-style', {'extra_styles': ('Keep',)}, _update(extra_styles=('Changed',))),
        ]
        for label, initial, mutate in cases:
            with self.subTest(label):
                hwp = make_doc(extras=initial)
                hwp.outline_shape = foreign_shape()
                after_each_native_call(hwp, mutate)
                with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                    self._number(hwp)
                self.assertTrue(ctx.exception.mutation_may_have_persisted)


class CallerPropagationTests(unittest.TestCase):
    def _page_doc(self) -> _HwpmlHwp:
        hwp = make_doc()
        after_each_native_call(hwp, _update(char_height=2000))
        return hwp

    def test_bundle_page_number_corruption_is_an_unknown_outcome_and_state_is_kept(self) -> None:
        hwp = self._page_doc()
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(persisted['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)
        self.assertEqual(hwp.hwpml_extras['char_height'], 2000)
        self.assertEqual(len(mutating_calls(hwp)), 1)

    def test_bundle_official_numbering_corruption_is_an_unknown_outcome_and_state_is_kept(self) -> None:
        hwp = self._page_doc()
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual(hwp.hwpml_extras['char_height'], 2000)

    def test_bundle_unprovable_preservation_is_a_clean_refusal_with_no_native_mutation(self) -> None:
        for step_def in (PAGE_STEP, NUMBERING_STEP):
            with self.subTest(step_def['op']):
                hwp = make_doc()
                hwp.hwpml_text = hwp.get_text_file('HWPML2X', '').replace('STYLELIST', 'XSTYLELIST')
                result, persisted = _run_bundle(hwp, step_def)
                self.assertFalse(result['ok'])
                step = result['steps'][0]
                self.assertFalse(step['mutation_may_have_persisted'])
                self.assertFalse(step['dirty'])
                self.assertEqual(mutating_calls(hwp), [])

    def test_bundle_positive_controls_still_succeed_through_the_real_reader(self) -> None:
        for step_def, kind in ((PAGE_STEP, 'page'), (NUMBERING_STEP, 'numbering')):
            with self.subTest(kind):
                hwp = make_doc()
                result, persisted = _run_bundle(hwp, step_def)
                self.assertTrue(result['ok'])
                self.assertEqual(protection_of(result['steps'][0]['result'])['unproven'], [])

    def _edit_ops_failure(self, apply: Callable[..., Any], operation: dict[str, Any]) -> tuple[_HwpmlHwp, nn.NativeNumberingMutationError, list[dict[str, Any]]]:
        hwp = self._page_doc()
        recorded: list[dict[str, Any]] = []
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            apply(hwp, [operation], step_recorder=recorded.append)
        return hwp, ctx.exception, recorded

    def test_edit_ops_and_worker_propagate_the_unknown_error_without_cleanup(self) -> None:
        operations = [
            {'op': 'page_number', 'position': 'bottom-center'},
            {'op': 'official_numbering', 'find': 'Item text', 'level': 1},
        ]
        for apply in (apply_edit_operations, _apply_edit_operations_with_optional_step_recorder):
            for operation in operations:
                with self.subTest(apply=apply.__name__, op=operation['op']):
                    hwp, error, recorded = self._edit_ops_failure(apply, operation)
                    self.assertEqual(error.mutation_state, 'unknown')
                    self.assertTrue(error.mutation_may_have_persisted)
                    self.assertEqual(recorded[-1]['state'], 'failed')
                    self.assertEqual(len(mutating_calls(hwp)), 1)
                    self.assertEqual(hwp.hwpml_extras['char_height'], 2000)

    def test_edit_ops_positive_controls_still_succeed(self) -> None:
        for operation in (
            {'op': 'page_number', 'position': 'bottom-center'},
            {'op': 'official_numbering', 'find': 'Item text', 'level': 1},
        ):
            with self.subTest(operation['op']):
                hwp = make_doc()
                summary = apply_edit_operations(hwp, [operation])
                self.assertEqual(protection_of(summary[0])['unproven'], [])


if __name__ == '__main__':
    unittest.main()
