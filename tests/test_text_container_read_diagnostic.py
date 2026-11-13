"""Prospective fixed read/parse diagnostics, not a box07 cause or native evidence.

Real service/manager/owning thread/public projection; only SDK and constructor
process authority are modeled by the existing Owned harness. Its final_release
is model-only after assertions, never native cleanup authority.
"""
import asyncio
import json
from types import MethodType
import unittest
from unittest.mock import patch
from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService
from app.native_numbering import NativeNumberingReadFailure, NativeNumberingRefusal
from tests.native_text_containers_fakes import ContainersHwp
from tests.test_native_numbering_runtime_custody import Owned

CREATE = {'op':'text_box_create', 'text':'Diagnostic box', 'width_mm':40, 'height_mm':20,
          'expected_pos':[0,1,0], 'confirm_layout':True}
PRIVATE = 'PRIVATE-SDK-ERROR-MUST-NOT-BE-PUBLISHED'

class Probe:
    """Model-only trace of each raw interface access; no COM or metadata claim."""
    def __init__(self, inner, kind):
        self.inner, self.kind, self.trace, self.fired = inner, kind, [], False
    def __getattr__(self, name):
        self.trace.append(('AFTER:' if self.fired else '') + name)
        if name == 'GetTextFile':
            def read(*args):
                source = self.inner.GetTextFile(*args)
                self.fired = True
                if self.kind == 'raised':
                    raise RuntimeError(PRIVATE)
                if self.kind == 'forged_refusal':
                    raise NativeNumberingRefusal(PRIVATE, code='container_wrong_caret')
                if self.kind == 'none': return None
                if self.kind == 'opaque': return HostileValue()
                if self.kind == 'empty': return ''
                if self.kind == 'xml': return '<HWPML><BODY>'
                if self.kind == 'body': return '<HWPML><HEAD/></HWPML>'
                if self.kind == 'outline':
                    # Well-formed, otherwise full fixture: remove the actual referred definition.
                    import xml.etree.ElementTree as E
                    root = E.fromstring(source)
                    for element in root.iter():
                        if element.tag.lower() == 'numberinglist':
                            for child in list(element): element.remove(child)
                    return E.tostring(root, encoding='unicode')
                raise AssertionError('unknown fixture kind')
            return read
        return getattr(self.inner, name)

class HostileValue:
    events = []
    def __bool__(self): self.events.append('bool'); raise AssertionError('callback')
    def __repr__(self): self.events.append('repr'); raise AssertionError('callback')
    def __str__(self): self.events.append('str'); raise AssertionError('callback')
    def __getattribute__(self, name):
        if name not in ('events', '__class__'):
            type(self).events.append('getattribute'); raise AssertionError('callback')
        return object.__getattribute__(self, name)


def owned_probe(case, kind, *, session_id='custody-session-0001'):
    hwp = ContainersHwp()
    probe = Probe(hwp, kind)
    hwp.hwp = probe
    owned = Owned(case, hwp, real_execute_live=True, session_id=session_id)
    hwp.cursor = (0,1,0)
    owned.service._public_bundle_steps = MethodType(LocalCliService._public_bundle_steps, owned.service)
    return owned, probe


def assert_held(case, owned, probe, reply):
    case.assertTrue(probe.fired)
    case.assertFalse(reply['ok'], reply)
    case.assertTrue(owned.session._native_custody_held)
    step = reply['steps'][0]
    case.assertTrue(step['native_custody_held'])
    case.assertFalse(step['mutation_may_have_persisted'])
    case.assertFalse(step['dirty'])
    case.assertNotIn('refusal_code', step)
    case.assertEqual(probe.trace, ['GetTextFile'], 'no SDK access after first unusable mandatory read')
    case.assertEqual(owned.hwp.executes(), [])
    case.assertEqual(owned.cleanup_events(), [])
    case.assertNotIn(PRIVATE, json.dumps(reply))
    return step['failure_diagnostic']


class ReadBoundary(unittest.TestCase):
    def check(self, kind, site, returned):
        owned, probe = owned_probe(self, kind)
        original = rt._native_diagnostic
        observed = []
        def diagnostic(*args):
            # No report callback is entered until the actual owning runtime is held.
            self.assertTrue(owned.session._native_custody_held)
            observed.append('diagnostic_after_owning_hold')
            return original(*args)
        with patch.object(rt, '_native_diagnostic', side_effect=diagnostic):
            result, error = owned.run(CREATE)
        self.assertIsNone(error)
        facts = assert_held(self, owned, probe, result)
        self.assertTrue(observed)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')), (site,returned), facts)
        self.assertEqual(facts['causal_class'], 'NativeNumberingReadFailure')
    def test_native_raise_is_not_a_parser_failure(self):
        self.check('raised','container.GetTextFile','raised')
    def test_none_return_is_not_a_parser_failure(self):
        self.check('none','container.GetTextFile','none')
    def test_opaque_return_is_classified_without_callbacks(self):
        HostileValue.events.clear()
        self.check('opaque','container.GetTextFile','object')
        self.assertEqual(HostileValue.events, [])
    def test_empty_text_stops_at_input_validation(self):
        self.check('empty','container.document.input','raised')
    def test_malformed_xml_stops_at_xml_parse(self):
        self.check('xml','container.document.xml','raised')
    def test_well_formed_without_body_is_distinct(self):
        self.check('body','container.document.body','raised')
    def test_real_missing_outline_definition_is_distinct(self):
        self.check('outline','container.document.outline','raised')
    def test_semantic_looking_getter_exception_cannot_authorize_refusal(self):
        self.check('forged_refusal','container.GetTextFile','raised')
    def test_healthy_whole_document_remains_an_eligible_read(self):
        hwp = ContainersHwp()
        owned = Owned(self,hwp,real_execute_live=True)
        result,error = owned.run({'op':'text_container_inventory'})
        self.assertIsNone(error)
        self.assertTrue(result['ok'], result)
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual(hwp.executes(), [])
    def test_actual_pre_witness_raw_abi_still_has_eligible_positive(self):
        from app.native_text_containers import _Native
        hwp = ContainersHwp()
        hwp.cursor = (0,0,238)
        hwp.GetSelectedPos = lambda: (False,0,0,0,0,0,0)
        self.assertEqual(_Native(hwp).state(),((0,0,238),()))


class FixedProjection(unittest.TestCase):
    def test_fixed_read_label_survives_existing_projection(self):
        error = NativeNumberingReadFailure('fixed',code='container_state_unproven')
        error.native_site, error.native_return = 'container.GetTextFile', 'none'
        facts = rt._native_diagnostic('bundle_step',error)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.GetTextFile','none'))
    def test_hostile_mapping_and_foreign_keys_are_not_queried(self):
        from tests.test_diagnostic_no_callbacks import CallbackDict, ForeignKey
        events = []
        error = NativeNumberingReadFailure('fixed',code='container_state_unproven')
        hostile = CallbackDict(events)
        dict.__setitem__(hostile,'native_site','container.GetTextFile')
        error.__dict__ = hostile
        self.assertNotIn('native_site',rt._native_diagnostic('bundle_step',error))
        self.assertEqual(events,[])
        key = ForeignKey('native_site',events)
        error.__dict__ = {key:'container.GetTextFile','native_return':'none'}
        events.clear()
        self.assertNotIn('native_site',rt._native_diagnostic('bundle_step',error))
        self.assertEqual(events,[])
    def test_arbitrary_site_or_return_is_never_published(self):
        for site,ret in ((PRIVATE,'none'),('container.GetTextFile',PRIVATE)):
            error=NativeNumberingReadFailure('fixed',code='container_state_unproven')
            error.native_site,error.native_return=site,ret
            facts=rt._native_diagnostic('bundle_step',error)
            self.assertNotIn('native_site',facts)
            self.assertNotIn(PRIVATE,json.dumps(facts))


async def asgi_post(service, step):
    from fastapi import FastAPI
    from app.local_cli_router import build_local_cli_router
    app = FastAPI()
    app.include_router(build_local_cli_router(settings=None,interactive_sessions=None,service=service))
    body=json.dumps({'steps':[step]}).encode()
    messages=[]
    async def receive(): return {'type':'http.request','body':body,'more_body':False}
    async def send(message): messages.append(message)
    await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
               'scheme':'http','path':'/local-cli/command-bundle','raw_path':b'/local-cli/command-bundle',
               'query_string':b'','headers':[(b'content-type',b'application/json')],
               'client':('127.0.0.1',1234),'server':('127.0.0.1',1235)},receive,send)
    return messages[0]['status'],json.loads(b''.join(m.get('body',b'') for m in messages[1:]))


class BackendHTTP(unittest.TestCase):
    def test_real_route_json_keeps_semantic_failure_and_fixed_leaf(self):
        owned,probe=owned_probe(self,'outline')
        status,reply=asyncio.run(asgi_post(owned.service,CREATE))
        self.assertEqual(status,200)
        facts=assert_held(self,owned,probe,reply)
        self.assertEqual(facts.get('native_site'),'container.document.outline')
        self.assertEqual(facts.get('native_return'),'raised')
