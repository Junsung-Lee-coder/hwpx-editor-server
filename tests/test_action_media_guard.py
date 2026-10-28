"""Issue #3: `hwpx action` must not silently remove a picture or its embedded BinData.

The fake models the reported Hancom behaviour: a paragraph holds an inline picture followed by caption text, a
selection covers only a trailing caption substring, and `Delete` removes that text *and* the adjacent picture with its
BinData entry while returning True. The real service `action` path, the media inventory and `hwpx undo` run unchanged;
only the Hancom object and `_execute_live` are faked. This is not Hancom evidence.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.local_cli_router import LocalCliActionRequest
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliService
from app.local_cli_service_support import LocalCliServiceError
from app.media_guard import MediaGuardError, media_change_reasons, media_inventory, public_counts
from local_cli_v1.main import build_parser

CAPTION = 'Figure 1. Caption text TAIL'
PAYLOAD = 'c/L9xgABZkCsAcQcUMzIIAEWPwDER/ggGA4a/g9pBADr7lGm9gAAAA=='


def hwpml(*, picture: bool, caption: str, payload: str | None = PAYLOAD, size: int = 127, extra_body: str = '') -> str:
    """HWPML2X shaped like a native Hancom readback (tag and attribute names as Hancom writes them)."""
    picture_xml = (
        '<PICTURE Reverse="false"><SHAPEOBJECT InstId="1231575054" TextWrap="TopAndBottom"><SIZE Height="2268" Width="2268"/>'
        '</SHAPEOBJECT><SHAPECOMPONENT InstID="157833231"/><IMAGE Alpha="0" BinItem="1" Bright="0" Contrast="0" Effect="RealPic"/>'
        '</PICTURE>'
        if picture
        else ''
    )
    bindata_list = '<BINDATALIST Count="1"><BINITEM BinData="1" Format="BMP" Type="Embedding"/></BINDATALIST>' if payload else ''
    storage = (
        f'<BINDATASTORAGE><BINDATA Compress="true" Encoding="Base64" Id="1" Size="{size}">{payload}</BINDATA></BINDATASTORAGE>'
        if payload
        else ''
    )
    return (
        '<?xml version="1.0" encoding="UTF-16" standalone="no" ?>'
        f'<HWPML Style="embed" Version="2.91"><HEAD SecCnt="1">{bindata_list}</HEAD><BODY><SECTION Id="0">'
        f'<P ParaShape="0" Style="0"><TEXT CharShape="0">{picture_xml}<CHAR>{caption}</CHAR></TEXT></P>'
        f'<P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>Plain paragraph</CHAR></TEXT></P>{extra_body}'
        f'</SECTION></BODY><TAIL>{storage}</TAIL></HWPML>'
    )


class PictureCaptionHwp:
    """One paragraph: inline picture + caption. Offsets count the picture as one position, as `hwpx find` does."""

    def __init__(self, *, delete_takes_picture: bool = True, size_drift: bool = False) -> None:
        self.picture = True
        self.caption = CAPTION
        self.payload: str | None = PAYLOAD
        self.delete_takes_picture = delete_takes_picture
        self.size_drift = size_drift
        tail = CAPTION.index('TAIL') + 1
        self.selection: tuple[int, int] | None = (tail, tail + 4)
        self.caret = tail + 4
        self.calls: list[tuple[Any, ...]] = []
        self.readbacks = 0
        self.fail_readback_after_run = False
        self.fail_readback = False
        self.readback_collapses_selection = False
        self.readback_changes_selection_mode = False
        self.SelectionMode = 0
        self.HAction = SimpleNamespace(Run=self._run)

    def _run(self, name: str) -> bool:
        self.calls.append(('Run', name))
        if name == 'Delete' and self.selection is not None:
            start, end = self.selection
            offset = 1 if self.picture else 0
            self.caption = self.caption[: start - offset] + self.caption[end - offset:]
            if self.delete_takes_picture:
                self.picture = False
                self.payload = None
            self.caret = start
            self.selection = None
        return True

    def get_pos(self) -> tuple[int, int, int]:
        return (0, 0, self.caret)

    def get_selected_pos(self) -> tuple[Any, ...]:
        if self.selection is None:
            return (False, 0, 0, 0, 0, 0, 0)
        return (True, 0, 0, self.selection[0], 0, 0, self.selection[1])

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        self.calls.append(('get_text_file', fmt, option))
        ran = any(call[0] == 'Run' for call in self.calls)
        if self.fail_readback or (self.fail_readback_after_run and ran):
            raise RuntimeError('export failed')
        if self.readback_collapses_selection:
            self.selection = None
        if self.readback_changes_selection_mode:
            self.SelectionMode = 1
        self.readbacks += 1
        size = 127 + self.readbacks if self.size_drift else 127
        return hwpml(picture=self.picture, caption=self.caption, payload=self.payload, size=size)


def make_service(hwp: Any, store: dict[str, Any]) -> LocalCliService:
    service = object.__new__(LocalCliService)

    def save(binding: dict[str, Any]) -> dict[str, Any]:
        store['binding'] = binding
        return binding

    def execute_live(*, binding: dict[str, Any], handler: Any, command_name: str = '', **kwargs: Any) -> Any:
        store.setdefault('commands', []).append(command_name)
        if command_name == 'undo':
            store['undo_dispatched'] = True
            return {'snapshot': {}, 'context': {}, 'location': {}}
        handle = SimpleNamespace(hwp=hwp, source_filename='a.hwpx', session_id='s', mutation_intent=False, hold_native_custody=None)
        handle.mark_mutating_intent = lambda: setattr(handle, 'mutation_intent', True)
        return handler(handle)

    service._load_active_binding = lambda session_id=None: store['binding']  # type: ignore[method-assign]
    service._save_binding = save  # type: ignore[method-assign]
    service._update_live_binding = lambda current, **kwargs: current  # type: ignore[method-assign]
    service._record_local_cli_command = lambda *args, **kwargs: store.setdefault('records', []).append((args, kwargs))  # type: ignore[method-assign]
    service._execute_live = execute_live  # type: ignore[method-assign]
    return service


def fresh_store() -> dict[str, Any]:
    return {'binding': {'session_id': 's', 'command_generation': 0, 'native_command_sequence': 0, 'pending_logical_undo_count': 1}}


def native_runs(hwp: PictureCaptionHwp) -> list[tuple[Any, ...]]:
    return [call for call in hwp.calls if call[0] == 'Run']


class ActionMediaGuardTests(unittest.TestCase):
    def run_action(self, hwp: PictureCaptionHwp, store: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        service = make_service(hwp, store)
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            return service.action(action_name='Delete', **kwargs)

    def test_a_delete_that_also_removes_the_adjacent_picture_is_reported_not_silent(self) -> None:
        hwp = PictureCaptionHwp(delete_takes_picture=True)
        store = fresh_store()
        with self.assertRaises(LocalCliServiceError) as ctx:
            self.run_action(hwp, store)
        message = ctx.exception.message
        self.assertEqual(ctx.exception.status_code, 409)
        for needle in ('Delete', 'picture controls 1 -> 0', 'BinData', 'may have persisted', 'owning STA', 'full native close is unsupported'):
            self.assertIn(needle, message)
        self.assertEqual(native_runs(hwp), [('Run', 'Delete')])
        binding = store['binding']
        self.assertTrue(binding['working_copy_dirty'])
        self.assertIsNone(binding['pending_logical_undo_count'])
        self.assertIn('Delete', binding['logical_undo_unverified'])
        self.assertIsNone(binding['last_find'])
        recorded = store['records'][-1][1]['payload']
        self.assertIs(recorded['ok'], False)
        self.assertEqual(recorded['media_guard']['status'], 'changed')
        service = make_service(hwp, store)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()
        self.assertNotIn('undo_dispatched', store)

    def test_a_text_only_delete_keeps_the_picture_and_reports_the_unchanged_inventory(self) -> None:
        hwp = PictureCaptionHwp(delete_takes_picture=False)
        store = fresh_store()
        result = self.run_action(hwp, store)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['media_guard']['status'], 'unchanged')
        self.assertEqual(result['media_guard']['before'], result['media_guard']['after'])
        self.assertEqual(result['media_guard']['before']['picture_controls'], 1)
        self.assertEqual(result['media_guard']['before']['bindata_payloads'], 1)
        self.assertEqual(native_runs(hwp), [('Run', 'Delete')])
        self.assertEqual(hwp.caption, CAPTION[:-4])
        self.assertEqual(store['binding']['pending_logical_undo_count'], 1)
        self.assertIsNone(store['binding'].get('logical_undo_unverified'))
        self.assertIs(store['records'][-1][1]['payload']['ok'], True)

    def test_an_unchanged_payload_with_a_different_stored_size_is_not_a_media_change(self) -> None:
        hwp = PictureCaptionHwp(delete_takes_picture=False, size_drift=True)
        result = self.run_action(hwp, fresh_store())
        self.assertEqual(result['media_guard']['status'], 'unchanged')

    def test_an_unreadable_inventory_before_the_action_refuses_without_running_it(self) -> None:
        hwp = PictureCaptionHwp()
        hwp.fail_readback = True
        store = fresh_store()
        with self.assertRaises(LocalCliServiceError) as ctx:
            self.run_action(hwp, store)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('no native action was run', ctx.exception.message)
        self.assertEqual(native_runs(hwp), [])
        self.assertNotIn('working_copy_dirty', store['binding'])

    def test_a_readback_that_moves_the_selection_refuses_without_running_the_action(self) -> None:
        hwp = PictureCaptionHwp()
        hwp.readback_collapses_selection = True
        with self.assertRaises(LocalCliServiceError) as ctx:
            self.run_action(hwp, fresh_store())
        self.assertIn('selection', ctx.exception.message)
        self.assertIn('no native action was run', ctx.exception.message)
        self.assertEqual(native_runs(hwp), [])

    def test_a_readback_that_changes_the_selection_mode_refuses_without_running_the_action(self) -> None:
        hwp = PictureCaptionHwp()
        hwp.readback_changes_selection_mode = True
        with self.assertRaises(LocalCliServiceError) as ctx:
            self.run_action(hwp, fresh_store())
        self.assertIn('no native action was run', ctx.exception.message)
        self.assertEqual(native_runs(hwp), [])

    def test_an_unreadable_inventory_after_the_action_is_reported_as_a_possible_change(self) -> None:
        hwp = PictureCaptionHwp(delete_takes_picture=False)
        hwp.fail_readback_after_run = True
        store = fresh_store()
        with self.assertRaises(LocalCliServiceError) as ctx:
            self.run_action(hwp, store)
        self.assertIn('could not be read after the action', ctx.exception.message)
        self.assertEqual(native_runs(hwp), [('Run', 'Delete')])
        self.assertTrue(store['binding']['working_copy_dirty'])
        self.assertIsNone(store['binding']['pending_logical_undo_count'])

    def test_an_explicitly_intended_media_change_skips_the_guard(self) -> None:
        hwp = PictureCaptionHwp(delete_takes_picture=True)
        result = self.run_action(hwp, fresh_store(), allow_media_change=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['media_guard']['status'], 'not_checked')
        self.assertEqual([call for call in hwp.calls if call[0] == 'get_text_file'], [])
        self.assertEqual(native_runs(hwp), [('Run', 'Delete')])


class MediaInventoryTests(unittest.TestCase):
    def test_counts_follow_the_native_tag_names(self) -> None:
        inventory = media_inventory(hwpml(picture=True, caption=CAPTION))
        self.assertEqual(
            public_counts(inventory),
            {'picture_controls': 1, 'ole_controls': 0, 'binitem_references': 1, 'bindata_items': 1, 'bindata_payloads': 1},
        )

    def test_each_kind_of_loss_or_change_is_a_reason(self) -> None:
        base = media_inventory(hwpml(picture=True, caption=CAPTION))
        cases = {
            'picture-and-bindata-removed': hwpml(picture=False, caption=CAPTION, payload=None),
            'picture-removed-bindata-kept': hwpml(picture=False, caption=CAPTION),
            'bindata-removed-picture-kept': hwpml(picture=True, caption=CAPTION, payload=None),
            'payload-changed': hwpml(picture=True, caption=CAPTION, payload=PAYLOAD[:-4] + 'AAA='),
            'extra-picture': hwpml(picture=True, caption=CAPTION, extra_body='<P><TEXT><PICTURE><IMAGE BinItem="1"/></PICTURE></TEXT></P>'),
            'ole-added': hwpml(picture=True, caption=CAPTION, extra_body='<P><TEXT><OLE BinItem="1"/></TEXT></P>'),
        }
        for label, after in cases.items():
            with self.subTest(label):
                self.assertTrue(media_change_reasons(base, media_inventory(after)))

    def test_body_text_changes_alone_are_not_media_changes(self) -> None:
        before = media_inventory(hwpml(picture=True, caption=CAPTION))
        after = media_inventory(hwpml(picture=True, caption='Figure 1.', size=99))
        self.assertEqual(media_change_reasons(before, after), [])

    def test_unreadable_readbacks_are_refused(self) -> None:
        for label, value in (
            ('none', None),
            ('empty', '  '),
            ('malformed', '<HWPML><BODY>'),
            ('no-body', '<HWPML><HEAD/></HWPML>'),
            ('doctype', '<!DOCTYPE x [<!ENTITY e "x">]><HWPML><BODY/></HWPML>'),
        ):
            with self.subTest(label), self.assertRaises(MediaGuardError):
                media_inventory(value)


class ActionSurfaceTests(unittest.TestCase):
    def test_cli_and_request_carry_the_explicit_media_change_opt_in(self) -> None:
        parser = build_parser()
        self.assertFalse(parser.parse_args(['action', 'Delete']).allow_media_change)
        self.assertTrue(parser.parse_args(['action', 'Delete', '--allow-media-change']).allow_media_change)
        self.assertFalse(LocalCliActionRequest(action_name='Delete').allow_media_change)
        self.assertTrue(LocalCliActionRequest(action_name='Delete', allow_media_change=True).allow_media_change)


if __name__ == '__main__':
    unittest.main()
