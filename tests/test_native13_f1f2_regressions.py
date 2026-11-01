"""Native13 F1/F2 regressions: save/reopen call contract and ambiguous foreign paragraph identifiers.

F1: `_official_delta_problems` must stay callable with the unchanged N2 save/reopen call shape and must not excuse a new
paragraph identifier unless the caller states that the ParagraphShape write happened.
F2: a non-target paragraph carrying several InstId spellings must never be read as "no identifier", so it cannot be used to
establish that a new target identifier is unique.
Fakes prove call shapes and refusal behaviour of this source only; they are not Hancom evidence.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import re
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app import native_numbering as nn
from app import native_numbering_census as census_module
from app.native_numbering_census import live_protection_reader
from tests import test_native13_first_level_inst_id as T

# `N2Run.reopened_problems`, copied verbatim from the N2 native case runner (n2_run.py sha256 c5fc1aa3342576dd...,
# segment sha256 cd99af538343f756...). The same segment is unchanged in the runner used for later native runs.
REOPENED_PROBLEMS = Path(__file__).resolve().parent / 'fixtures' / 'native_numbering' / 'n2_harness' / 'reopened_problems.py.txt'
REOPENED_PROBLEMS_SHA256 = '79e79ebb66168894df3e1f7f6ce0b403b6d17a2800514af1ac42c1521061ca74'
TAIL_ID = '153176621'
OTHER_ID = '153176622'
_TAIL_HEAD = re.compile(r'<P (?:InstId="[^"]*" )?(?=ParaShape="\d+" Style="0"><TEXT CharShape="\d+"><CHAR>Tail)')


class _ForeignSpellingsHwp(T._NativeIdsHwp):  # noqa: SLF001
    """The unchanged non-target `Tail` paragraph carries `tail_head` attribute spellings before and after the write."""

    def __init__(self, tail_head: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tail_head = tail_head

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        text = super().get_text_file(fmt, option)
        if fmt == 'HWPML2X':
            text, count = _TAIL_HEAD.subn(lambda _match: f'<P {self.tail_head} ', text)
            assert count == 1, count
        return text


def validated_census(hwp: Any) -> dict[str, Any]:
    return nn._validated_census(live_protection_reader(hwp)())  # noqa: SLF001


def written(hwp: Any) -> Any:
    hwp.calls.append(('Execute', 'ParagraphShape', {}))
    return hwp


def pair(assign: dict[str, str | None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Two censuses of an already-outlined target; the second may carry a native-assigned identifier."""
    before = validated_census(T._NativeIdsHwp(target_heading=(1, 0)))  # noqa: SLF001
    after = validated_census(written(T._NativeIdsHwp(assign=assign, target_heading=(1, 0))))  # noqa: SLF001
    return before, after


def reopened_problems_function() -> Any:
    """The unchanged `N2Run.reopened_problems` source, bound to its pinned bytes and compiled without importing the harness."""
    path = REOPENED_PROBLEMS
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == REOPENED_PROBLEMS_SHA256
    tree = ast.parse(textwrap.dedent(data.decode('utf-8')))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'reopened_problems')
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)  # noqa: S102
    return namespace['reopened_problems']


def assert_post_issued_unknown(case: unittest.TestCase, hwp: Any) -> nn.NativeNumberingMutationError:
    with case.assertRaises(nn.NativeNumberingMutationError) as ctx:
        T.run_level(hwp)
    case.assertIn('outline numbering outcome is not as requested', str(ctx.exception))
    case.assertIn('identifier', str(ctx.exception))
    case.assertEqual(ctx.exception.mutation_state, 'unknown')
    case.assertTrue(ctx.exception.mutation_may_have_persisted)
    case.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
    case.assertEqual(len(T.mutating(hwp)), 1)
    case.assertEqual([call for call in hwp.calls if call[0] in {'Run', 'Undo', 'Close', 'Discard', 'save', 'save_as'}], [])
    return ctx.exception


class RuntimeOriginTests(unittest.TestCase):
    def test_the_exercised_runtime_is_the_repository_under_test(self) -> None:
        root = Path(__file__).resolve().parents[1] / 'app'
        self.assertEqual(Path(nn.__file__).resolve(), root / 'native_numbering.py')
        self.assertEqual(Path(census_module.__file__).resolve(), root / 'native_numbering_census.py')


class SaveReopenCallContractTests(unittest.TestCase):
    """F1: the call shape of the unchanged N2 save/reopen comparison."""

    def test_the_unchanged_save_reopen_call_shape_is_accepted_for_identical_censuses(self) -> None:
        before, after = pair()
        self.assertEqual(nn._official_delta_problems(before, after, target_index=1, native_level=0, define=False), [])  # noqa: SLF001

    def test_the_save_reopen_call_shape_does_not_excuse_a_new_identifier(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        problems = nn._official_delta_problems(before, after, target_index=1, native_level=0, define=False)  # noqa: SLF001
        self.assertEqual(problems, ['the target paragraph identifier changed beyond one new native assignment'])

    def test_an_explicit_paragraph_write_is_the_only_thing_that_excuses_a_new_identifier(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        accepted = nn._official_delta_problems(  # noqa: SLF001
            before, after, target_index=1, native_level=0, define=False, target_written=True
        )
        refused = nn._official_delta_problems(  # noqa: SLF001
            before, after, target_index=1, native_level=0, define=False, target_written=False
        )
        self.assertEqual(accepted, [])
        self.assertEqual(refused, ['the target paragraph identifier changed beyond one new native assignment'])

    def test_no_default_or_explicit_write_flag_hides_unrelated_changes(self) -> None:
        def style(after: dict[str, Any]) -> None:
            after['styles_sha256'] = 'changed'

        def media(after: dict[str, Any]) -> None:
            after['bindata_sha256'] = 'changed'

        def header(after: dict[str, Any]) -> None:
            after['header_footer'] = ['changed']

        def other_text(after: dict[str, Any]) -> None:
            after['paragraphs'][2]['text_sha256'] = 'changed'

        def other_paragraph(after: dict[str, Any]) -> None:
            after['paragraphs'][2]['sha256'] = 'changed'

        def target_content(after: dict[str, Any]) -> None:
            after['paragraphs'][1]['masked_sha256'] = 'changed'

        for flags in ({}, {'target_written': False}, {'target_written': True}):
            for name, change in (
                ('styles', style), ('media', media), ('header-footer', header), ('other-text', other_text),
                ('other-paragraph', other_paragraph), ('target-content', target_content),
            ):
                with self.subTest(flags=flags, change=name):
                    before, after = pair({T.TARGET: T.NEW_ID})
                    change(after)
                    problems = nn._official_delta_problems(  # noqa: SLF001
                        before, after, target_index=1, native_level=0, define=False, **flags
                    )
                    self.assertTrue(problems)

    def test_the_target_must_still_be_an_outline_paragraph_at_the_requested_level(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        problems = nn._official_delta_problems(before, after, target_index=1, native_level=1, define=False)  # noqa: SLF001
        self.assertIn('the target paragraph is not an outline paragraph at the requested level', problems)

    def test_an_existing_identifier_is_never_replaced_or_removed_on_a_reopen(self) -> None:
        for label, assign in (('changed', {T.TARGET: '222'}), ('removed', {T.TARGET: None})):
            with self.subTest(label):
                before = validated_census(T._NativeIdsHwp(before_ids={T.TARGET: '111'}, target_heading=(1, 0)))  # noqa: SLF001
                after = validated_census(
                    written(T._NativeIdsHwp(before_ids={T.TARGET: '111'}, assign=assign, target_heading=(1, 0)))  # noqa: SLF001
                )
                for flags in ({}, {'target_written': True}):
                    problems = nn._official_delta_problems(  # noqa: SLF001
                        before, after, target_index=1, native_level=0, define=False, **flags
                    )
                    self.assertTrue(problems)


class UnchangedN2ReopenCallSiteTests(unittest.TestCase):
    """F1: the actual, unmodified N2Run.reopened_problems call site (n2_run.py:246-247)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reopened_problems = staticmethod(reopened_problems_function())

    def call(self, last: dict[str, Any], reopened: dict[str, Any]) -> list[str]:
        harness = SimpleNamespace(sdk={'numbering': nn})
        return self.reopened_problems(harness, last, reopened, {}, {}, T.TARGET, 0)

    def raw_pair(self, assign: dict[str, str | None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        last = live_protection_reader(T._NativeIdsHwp(target_heading=(1, 0)))()  # noqa: SLF001
        reopened = live_protection_reader(written(T._NativeIdsHwp(assign=assign, target_heading=(1, 0))))()  # noqa: SLF001
        return last, reopened

    def test_an_unchanged_reopen_has_no_problems(self) -> None:
        self.assertEqual(self.call(*self.raw_pair()), [])

    def test_a_reopen_is_not_a_paragraph_shape_write_so_a_new_identifier_is_a_problem(self) -> None:
        problems = self.call(*self.raw_pair({T.TARGET: T.NEW_ID}))
        self.assertEqual(problems, ['the target paragraph identifier changed beyond one new native assignment'])

    def test_a_reopen_still_reports_unrelated_changes(self) -> None:
        last, reopened = self.raw_pair()
        reopened['styles_sha256'] = 'changed'
        self.assertTrue(any('styles' in problem for problem in self.call(last, reopened)))


class IdentifierCensusTests(unittest.TestCase):
    """F2: the census keeps every InstId spelling instead of collapsing several to nothing."""

    def tail_census(self, hwp: Any) -> dict[str, Any]:
        return live_protection_reader(hwp)()['paragraphs'][2]

    def test_every_spelling_is_retained_and_a_single_spelling_keeps_its_scalar(self) -> None:
        cases = (
            ('absent', T._NativeIdsHwp(), [], None),  # noqa: SLF001
            ('single', T._NativeIdsHwp(before_ids={'Tail': TAIL_ID}), [TAIL_ID], TAIL_ID),  # noqa: SLF001
            ('two', _ForeignSpellingsHwp(f'instid="{OTHER_ID}" InstId="{TAIL_ID}"'), [TAIL_ID, OTHER_ID], None),
            ('same-value-twice', _ForeignSpellingsHwp(f'instid="{TAIL_ID}" InstId="{TAIL_ID}"'), [TAIL_ID, TAIL_ID], None),
        )
        for label, hwp, spellings, scalar in cases:
            with self.subTest(label):
                paragraph = self.tail_census(hwp)
                self.assertEqual(paragraph.get('inst_ids'), spellings)
                self.assertEqual(paragraph['inst_id'], scalar)

    def test_the_numbering_validator_rejects_malformed_identifier_lists(self) -> None:
        for label, value in (('string', TAIL_ID), ('integers', [1]), ('mixed', [TAIL_ID, None]), ('mapping', {'a': TAIL_ID})):
            with self.subTest(label):
                raw = live_protection_reader(T._NativeIdsHwp())()  # noqa: SLF001
                raw['paragraphs'][2]['inst_ids'] = value
                with self.assertRaises(RuntimeError):
                    nn._validated_census(raw)  # noqa: SLF001

    def test_a_census_without_the_identifier_list_keeps_the_scalar_contract(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        for census in (before, after):
            for item in census['paragraphs']:
                item.pop('inst_ids', None)
        self.assertEqual(
            nn._official_delta_problems(  # noqa: SLF001
                before, after, target_index=1, native_level=0, define=False, target_written=True
            ),
            [],
        )


class AmbiguousForeignIdentifierTests(unittest.TestCase):
    """F2: a new target identifier needs positive uniqueness against every identifier spelling of every other paragraph."""

    def test_the_parent_witness_is_refused_after_exactly_one_issued_write(self) -> None:
        hwp = _ForeignSpellingsHwp(f'instid="{TAIL_ID}" InstId="{T.NEW_ID}"', assign={T.TARGET: T.NEW_ID})
        before = live_protection_reader(hwp)()
        self.assertEqual([item['inst_id'] for item in before['paragraphs']], [T.FIRST_ID, None, None])
        self.assertEqual(before['paragraphs'][2]['inst_ids'], [T.NEW_ID, TAIL_ID])
        assert_post_issued_unknown(self, hwp)

    def test_ambiguous_or_colliding_foreign_spellings_never_establish_uniqueness(self) -> None:
        for label, head in (
            ('collides-with-one-spelling', f'instid="{TAIL_ID}" InstId="{T.NEW_ID}"'),
            ('collides-either-order', f'InstId="{TAIL_ID}" instid="{T.NEW_ID}"'),
            ('same-value-twice', f'InstId="{T.NEW_ID}" instid="{T.NEW_ID}"'),
            ('ambiguous-but-distinct', f'InstId="{TAIL_ID}" instid="{OTHER_ID}"'),
        ):
            with self.subTest(label):
                assert_post_issued_unknown(self, _ForeignSpellingsHwp(head, assign={T.TARGET: T.NEW_ID}))

    def test_ambiguity_is_not_a_pre_mutation_refusal_when_the_target_gains_no_identifier(self) -> None:
        hwp = _ForeignSpellingsHwp(f'instid="{TAIL_ID}" InstId="{T.NEW_ID}"')
        result = T.run_level(hwp)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['native_actions'], T.LEVEL_ONE_ACTION)
        self.assertEqual(len(T.mutating(hwp)), 1)

    def test_a_unique_foreign_identifier_still_allows_the_ordinary_assignment(self) -> None:
        for label, hwp in (
            ('missing-foreign-ids', T._NativeIdsHwp(assign={T.TARGET: T.NEW_ID})),  # noqa: SLF001
            ('single-foreign-id', T._NativeIdsHwp(before_ids={'Tail': TAIL_ID}, assign={T.TARGET: T.NEW_ID})),  # noqa: SLF001
        ):
            with self.subTest(label):
                result = T.run_level(hwp)
                self.assertEqual(result['status'], 'applied')
                self.assertEqual(len(T.mutating(hwp)), 1)

    def test_a_single_foreign_identifier_that_equals_the_new_one_is_still_refused(self) -> None:
        assert_post_issued_unknown(
            self, T._NativeIdsHwp(before_ids={'Tail': T.NEW_ID}, assign={T.TARGET: T.NEW_ID})  # noqa: SLF001
        )

    def test_a_foreign_paragraph_that_changes_alongside_the_assignment_is_still_refused(self) -> None:
        hwp = _ForeignSpellingsHwp(
            f'InstId="{TAIL_ID}" instid="{OTHER_ID}"',
            assign={T.TARGET: T.NEW_ID},
            after=lambda text: text.replace('<CHAR>Tail</CHAR>', '<CHAR>Tail</CHAR><CHAR>x</CHAR>'),
        )
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            T.run_level(hwp)
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertEqual(len(T.mutating(hwp)), 1)

    def test_ambiguity_on_the_target_is_never_a_new_assignment(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        before = copy.deepcopy(before)
        before['paragraphs'][1]['inst_id'] = None
        before['paragraphs'][1]['inst_ids'] = ['1', '2']
        problems = nn._official_delta_problems(  # noqa: SLF001
            before, after, target_index=1, native_level=0, define=False, target_written=True
        )
        self.assertEqual(problems, ['the target paragraph identifier changed beyond one new native assignment'])

    def test_a_second_spelling_on_the_assigned_target_is_still_refused(self) -> None:
        before, after = pair({T.TARGET: T.NEW_ID})
        after['paragraphs'][1]['inst_id'] = None
        after['paragraphs'][1]['inst_ids'] = [T.NEW_ID, T.NEW_ID + '1']
        problems = nn._official_delta_problems(  # noqa: SLF001
            before, after, target_index=1, native_level=0, define=False, target_written=True
        )
        self.assertEqual(problems, ['the target paragraph identifier changed beyond one new native assignment'])


if __name__ == '__main__':
    unittest.main()
