"""Synthetic faults through actual service/package/owning STA; not native evidence."""
from types import MethodType
import copy
import unittest
from app import local_cli_service as svc
from tests import test_numbering_target_guard as prior


def complete_protection(hwp):
    """Explicit synthetic SDK fixture: actual existing HWPML emitter + real parser.

    Legacy FakeHwp.census lacks media/restart proof. Do not fill its missing
    hashes or waive the guard; emit the current fake document through the
    already published full-census model. No production helper is replaced.
    """
    from app.native_numbering_census import parse_hwpml2x_census
    from tests.test_native_numbering_census import _HwpmlHwp
    hwp.hwpml_calls = 0
    hwp.hwpml_text = None
    hwp.hwpml_extras = {}
    hwp.seen_shapes = set()
    return lambda: parse_hwpml2x_census(_HwpmlHwp.get_text_file(hwp, 'HWPML2X', ''))


class ReadOrderTests(unittest.TestCase):
    owned = prior.NumberingTargetGuardTests.owned
    prepare = prior.NumberingTargetGuardTests.prepare
    locked = prior.NumberingTargetGuardTests.locked
    run_bundle = prior.NumberingTargetGuardTests.run_bundle
    command = prior.NumberingTargetGuardTests.command
    assert_held = prior.NumberingTargetGuardTests.assert_held

    def scenario(self, body=False):
        result = self.prepare()
        service, manager, session, hwp, calls, held = result
        self.locked(hwp, 'body' if body else False)
        armed = [False]
        real = service._bundle_official_numbering_apply
        def bundle(*a, **k):
            armed[0] = True
            try:
                return real(*a, **k)
            finally:
                armed[0] = False
        service._bundle_official_numbering_apply = bundle
        for name in ('get_pos', 'get_selected_pos', 'get_text_file', 'MoveDocBegin', 'MoveRight', 'set_pos', 'select_text'):
            original = getattr(hwp, name)
            def wrap(*a, _name=name, _original=original, **k):
                if armed[0]:
                    hwp.calls.append(('sdk', _name))
                return _original(*a, **k)
            setattr(hwp, name, wrap)
        return result, armed

    def outcome(self, setup, mark, match='N2NOMATCH', *, unissued=False):
        service, manager, session, hwp, calls, held = setup
        result = self.run_bundle(service, match=match)
        self.assertTrue(mark, 'fault boundary must actually be reached')
        self.assertFalse(result['ok'])
        self.assert_held(manager, session, hwp, calls, held, may_have_mutated=not unissued)
        self.assertEqual(hwp.calls[mark[0]:], [], 'no SDK after first unknown/invalid observation')
        self.assertFalse(any(c[0] == 'Execute' for c in hwp.calls))

    def state_fault(self, name, nth, bad, body=False):
        setup, armed = self.scenario(body)
        hwp = setup[3]
        original = getattr(hwp, name)
        count = 0
        mark = []
        def getter(*a, **k):
            nonlocal count
            if armed[0]:
                count += 1
                if count == nth:
                    hwp.calls.append(('fault', name))
                    mark[:] = [len(hwp.calls)]
                    if bad == 'raise':
                        raise OSError('PRIVATE')
                    return bad
            return original(*a, **k)
        setattr(hwp, name, getter)
        self.outcome(setup, mark, 'Item' if body else 'N2NOMATCH')

    def test_state_each_sampler_fences_next_getter(self):
        for body, positions, selections in ((False, range(1, 7), range(1, 4)), (True, range(1, 5), range(1, 3))):
            for name, counts, bads in (('get_pos', positions, ('raise', None, (0, True, 0))),
                                       ('get_selected_pos', selections, ('raise', None, (False, 0, 0, 0, 0, 0, None)))):
                for nth in counts:
                    for bad in bads:
                        with self.subTest(body=body, name=name, nth=nth, bad=bad):
                            self.state_fault(name, nth, bad, body)

    def test_foreign_state_container_is_not_read(self):
        callbacks = []
        class Foreign(tuple):
            def __len__(self):
                callbacks.append('len')
                return 3
            def __getitem__(self, k):
                callbacks.append('getitem')
                return 0
            def __iter__(self):
                callbacks.append('iter')
                return iter((0, 0, 0))
        self.state_fault('get_pos', 1, Foreign((0, 0, 0)))
        self.assertEqual(callbacks, [])

    def protection_fault(self, nth, transform, body=False, *, unissued=False):
        setup, armed = self.scenario(body)
        service, _, session, hwp, _, _ = setup
        original = service._numbering_protection_reader(session._active_handle)
        mark = []
        count = 0
        def reader():
            nonlocal count
            count += 1
            value = original()
            if count == nth:
                value = transform(value)
                mark[:] = [len(hwp.calls)]
            return value
        service._numbering_protection_reader = lambda h: reader
        self.outcome(setup, mark, 'Item' if body else 'N2NOMATCH', unissued=unissued)
        self.assertEqual(count, nth)

    def test_complete_protection_before_every_next_sdk(self):
        def alter(field, value):
            def transform(raw):
                raw[field] = value
                return raw
            return transform
        transforms = [lambda r: {}, alter('media_sha256', None), alter('frame', {}),
                      alter('paragraphs', None), alter('definitions', {}), alter('header_footer', None),
                      alter('outline_shapes', None), alter('body_sha256', 'x' * 64),
                      alter('page_restart_positions', None), alter('page_restarts', None)]
        def invalid_paragraph(raw):
            raw['paragraphs'][0]['section'] = True
            return raw
        transforms.append(invalid_paragraph)
        for body, nth in ((False, 1), (False, 2), (True, 2)):
            for index, transform in enumerate(transforms):
                with self.subTest(body=body, nth=nth, variant=index):
                    # These exact returned/validated incomplete first censuses
                    # precede all navigation/Execute. Custody remains UNKNOWN;
                    # it must not manufacture current-command edit debt (R4).
                    unissued = not body and nth == 1 and index in (1, 3, 4, 5, 6, 7, 8)
                    self.protection_fault(nth, transform, body, unissued=unissued)

    def test_foreign_census_containers_keys_values_have_zero_callbacks(self):
        callbacks = []
        class ForeignDict(dict):
            def __len__(self):
                callbacks.append('len'); return 1
            def items(self):
                callbacks.append('items'); return super().items()
            def get(self, *a):
                callbacks.append('get'); return super().get(*a)
        class Key:
            def __hash__(self):
                return hash('frame')
            def __eq__(self, other):
                callbacks.append('key_eq'); return False
        class Text(str):
            def __eq__(self, other):
                callbacks.append('text_eq'); return True
            def encode(self, *a, **k):
                callbacks.append('encode'); return b''
        def outer(r): return ForeignDict(r)
        def nested(r): r['frame'] = ForeignDict(r['frame']); return r
        def key(r):
            r[Key()] = 'PRIVATE'
            callbacks.clear()  # construction is outside the code under test
            return r
        def nested_key(r):
            r['frame'][Key()] = 'PRIVATE'; callbacks.clear(); return r
        def text(r): r['body_sha256'] = Text('PRIVATE'); return r
        for transform in (outer, nested, key, nested_key, text):
            with self.subTest(transform=transform.__name__):
                callbacks.clear()
                self.protection_fault(1, transform)
                self.assertEqual(callbacks, [])

    def test_changed_post_protection_stops_before_unicode(self):
        def changed(raw): raw['body_sha256'] = '0' * 64; return raw
        self.protection_fault(2, changed)

    def test_unicode_failure_or_nonexact_stops_before_control_reads(self):
        class Text(str):
            def encode(self, *a, **k): raise AssertionError('PRIVATE_CALLBACK')
        for nth in (1, 2):
            for bad in ('raise', None, Text('PRIVATE')):
                with self.subTest(nth=nth, type=type(bad).__name__):
                    setup, armed = self.scenario()
                    hwp = setup[3]
                    original = hwp.get_text_file
                    mark = []; count = [0]
                    def read(fmt, option):
                        if armed[0] and fmt == 'UNICODE':
                            count[0] += 1
                            if count[0] == nth:
                                hwp.calls.append(('fault', 'UNICODE')); mark[:] = [len(hwp.calls)]
                                if bad == 'raise': raise OSError('PRIVATE')
                                return bad
                        return original(fmt, option)
                    hwp.get_text_file = read
                    self.outcome(setup, mark)

    def test_page_getter_failure_invalid_and_unknown_never_fallback(self):
        for bad in ('raise', None, True, 0, '1'):
            with self.subTest(bad=bad):
                setup, armed = self.scenario(True)
                service, _, _, hwp, _, _ = setup
                service._bundle_page_evidence = MethodType(svc.LocalCliService._bundle_page_evidence, service)
                mark = []
                def first():
                    hwp.calls.append(('fault', 'current_page')); mark[:] = [len(hwp.calls)]
                    if bad == 'raise': raise OSError('PRIVATE')
                    return bad
                hwp.current_page = first
                hwp.get_current_page = lambda: hwp.calls.append(('forbidden_fallback',)) or 1
                self.outcome(setup, mark, 'Item')

    def test_missing_move_api_is_not_silent_noop(self):
        setup, armed = self.scenario()
        hwp = setup[3]
        hwp.MoveDocBegin = None
        boundary = len(hwp.calls)
        result = self.run_bundle(setup[0], match='N2NOMATCH')
        self.assertFalse(result['ok'])
        self.assert_held(setup[1], setup[2], hwp, setup[4], setup[5])
        self.assertFalse(any(c == ('locked_execute', 'RepeatFind') for c in hwp.calls[boundary:]))

    def test_false_restore_does_not_issue_proof_reads(self):
        for name in ('set_pos', 'select_text'):
            with self.subTest(name=name):
                setup, armed = self.scenario()
                hwp = setup[3]
                if name == 'select_text': hwp.selection_para = 2; hwp.cursor = (0, 2, 0)
                mark = []
                def refused(*a):
                    hwp.calls.append(('fault', name)); mark[:] = [len(hwp.calls)]; return False
                setattr(hwp, name, refused)
                self.outcome(setup, mark)

    def test_inner_native_state_reads_all_stop_at_fault(self):
        # Full successful matched path supplies the actual number of samplers;
        # every occurrence is then faulted through the same real public caller.
        setup, armed = self.scenario(True)
        self.assertTrue(self.run_bundle(setup[0])['ok'])
        names = ('get_pos', 'get_selected_pos')
        counts = {name: sum(c == ('sdk', name) for c in setup[3].calls) for name in names}
        self.assertGreater(counts['get_pos'], 4)
        for name in names:
            for nth in range(1, counts[name] + 1):
                with self.subTest(name=name, nth=nth):
                    self.state_fault(name, nth, 'raise', True)

    def test_invalid_control_id_stops_before_next_pointer(self):
        for nth in (1, 2):
            for bad in (None, False, 3, ''):
                with self.subTest(nth=nth, bad=bad):
                    setup, armed = self.scenario()
                    hwp = setup[3]; mark = []; reads = [0]
                    original = type(hwp).HeadCtrl.fget
                    class Broken:
                        @property
                        def CtrlID(node):
                            hwp.calls.append(('fault', 'CtrlID')); mark[:] = [len(hwp.calls)]; return bad
                        @property
                        def Next(node):
                            hwp.calls.append(('forbidden', 'Next')); return None
                    def head(obj):
                        if armed[0]:
                            reads[0] += 1
                            if reads[0] == nth: return Broken()
                        return original(obj)
                    hwp.__class__ = type('ControlFaultSdk', (type(hwp),), {'HeadCtrl': property(head)})
                    self.outcome(setup, mark)

    def test_post_text_mismatch_stops_before_control_census(self):
        setup, armed = self.scenario()
        hwp = setup[3]; original = hwp.get_text_file; count = [0]; mark = []
        def text(fmt, option):
            value = original(fmt, option)
            if armed[0] and fmt == 'UNICODE':
                count[0] += 1
                if count[0] == 2:
                    mark[:] = [len(hwp.calls)]; return value + ' changed'
            return value
        hwp.get_text_file = text
        self.outcome(setup, mark)

    def test_census_exact_container_and_metadata_class_are_not_coerced(self):
        callbacks = []
        class Meta(type):
            def __eq__(cls, other):
                callbacks.append('meta_eq'); return False
            def __getattribute__(cls, name):
                if name in ('__name__', '__mro__', '__dict__'):
                    callbacks.append('meta_attr')
                return super().__getattribute__(name)
        class Foreign(dict, metaclass=Meta):
            pass
        def foreign(raw): return Foreign(raw)
        self.protection_fault(1, foreign)
        self.assertEqual(callbacks, [])
        def tuple_header(raw): raw['header_footer'] = tuple(raw['header_footer']); return raw
        self.protection_fault(1, tuple_header)

    def test_native_outline_invalid_text_stops_before_number_format_getter(self):
        setup, armed = self.scenario(True)
        hwp = setup[3]; mark = []; original = hwp.HAction.GetDefault
        class Shape:
            @property
            def StrFormatLevel0(obj):
                hwp.calls.append(('fault', 'StrFormatLevel0')); mark[:] = [len(hwp.calls)]; return None
            @property
            def NumFormatLevel0(obj):
                hwp.calls.append(('forbidden', 'NumFormatLevel0')); return 0
        def defaults(name, hset):
            result = original(name, hset)
            if name == 'OutlineNumber': hset.OutlineShape = Shape()
            return result
        hwp.HAction.GetDefault = defaults
        self.outcome(setup, mark, 'Item')

    def test_native_paragraph_invalid_level_stops_before_target_getter(self):
        setup, armed = self.scenario(True)
        hwp = setup[3]; mark = []; original = hwp.HAction.GetDefault
        def defaults(name, hset):
            result = original(name, hset)
            if name == 'ParagraphShape':
                hset.Level = None; mark[:] = [len(hwp.calls)]
            return result
        hwp.HAction.GetDefault = defaults
        self.outcome(setup, mark, 'Item')

    def test_declared_page_descriptor_error_does_not_probe_metadata_or_fallback(self):
        setup, armed = self.scenario(True)
        service, _, _, hwp, _, _ = setup
        service._bundle_page_evidence = MethodType(svc.LocalCliService._bundle_page_evidence, service)
        callbacks = []; mark = []
        class Meta(type):
            def __getattribute__(cls, name):
                if name in ('__name__', '__mro__', '__dict__'):
                    callbacks.append(name)
                return super().__getattribute__(name)
        def page(obj):
            obj.calls.append(('fault', 'page_descriptor')); mark[:] = [len(obj.calls)]
            raise AttributeError('PRIVATE')
        cls = Meta('DeclaredPageSdk', (type(hwp),), {'current_page': property(page)})
        hwp.__class__ = cls
        hwp.get_current_page = lambda: hwp.calls.append(('forbidden_fallback',)) or 1
        callbacks.clear()
        self.outcome(setup, mark, 'Item')
        self.assertEqual(callbacks, [])

    def test_hwpml_foreign_text_is_refused_before_parser_callbacks(self):
        setup, armed = self.scenario()
        hwp = setup[3]; original = hwp.get_text_file; callbacks = []; mark = []
        class Text(str):
            def strip(self, *a):
                callbacks.append('strip'); return super().strip(*a)
            def __len__(self):
                callbacks.append('len'); return super().__len__()
        def read(fmt, option):
            value = original(fmt, option)
            if armed[0] and fmt == 'HWPML2X':
                mark[:] = [len(hwp.calls)]; return Text(value)
            return value
        hwp.get_text_file = read
        self.outcome(setup, mark)
        self.assertEqual(callbacks, [])


if __name__ == '__main__':
    unittest.main()
