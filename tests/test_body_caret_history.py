"""BC1 real service/package/public-history regressions; inert SDK model, no native qualification."""
import asyncio,copy,json,unittest
from unittest.mock import patch
from app.local_cli_service import LocalCliUndoRefused
from tests.native_text_containers_fakes import ContainersHwp,container
from tests.native_format_fakes import paragraph
from tests.test_native_paragraph_edit import Flow


class NavigationHistory(unittest.TestCase):
 def setUp(self):
  self.h=ContainersHwp([paragraph('Body 2025'),paragraph('',container(text='Box before'))]);self.f=Flow(self.h);self.f.binding.update(undo_stack=[],redo_stack=[],undo_states=[],redo_states=[]);self.history_calls=[];original=self.h.HAction.Run
  def run(name):
   if name in('Undo','Redo'):self.history_calls.append(name)
   return original(name)
  self.h.HAction.Run=run
  self.inventory()  # Prime the inert native census model through the real public read, not a guard stub.
 def inventory(self):return self.f.step(op='text_container_inventory')['steps'][0]['result']['containers'][0]
 def navigate(self):
  b=self.inventory();result=self.f.step(op='text_container_body_caret',target_id=b['target_id'],expected_hash=b['hash'],expected_text=b['text'],confirm_navigation=True);self.assertTrue(result['ok'],result);data=result['steps'][0]['result'];self.assertTrue(result['dirty']);self.assertTrue(data['result']['changed']);self.assertTrue(data['result']['navigation_only']);self.assertFalse(data['result']['content_changed']);self.assertEqual(self.h.cursor[0],0);return result
 def history(self,name):
  with patch('app.local_cli_service.snapshot_live_location',return_value={}),patch('app.local_cli_runtime.snapshot_live_location',return_value={}),patch('app.local_cli_service._snapshot_cursor_context',return_value={}),patch('app.local_cli_service._capture_nearby_text_context',return_value={}):return getattr(self.f.service,name)()
 def assert_no_history(self):
  for name in('undo','redo'):
   with self.subTest(public=name):
    before=copy.deepcopy(self.f.binding);self.history_calls.clear()
    with self.assertRaises(LocalCliUndoRefused):self.history(name)
    self.assertEqual(self.history_calls,[]);self.assertEqual(self.f.binding,before)
 def test_navigation_only_public_Undo_Redo_refuse_before_any_native_history(self):
  self.h.cursor=(2,0,10);self.navigate();self.assert_no_history()
 def test_previous_qualified_edit_is_not_undone_as_navigation(self):
  result=self.f.step(op='text_replace_exact',expected_page=1,confirm_layout=True,old_text='2025',new_text='2026');self.assertTrue(result['ok'],result);self.assertIsInstance(self.f.binding['undo_stack'][-1],int);prior=copy.deepcopy(self.f.binding['undo_stack']);self.navigate();self.assertEqual(self.f.binding['undo_stack'][:-1],prior);self.assert_no_history()
 def test_previous_unqualified_barrier_is_not_replaced_by_a_counted_navigation(self):
  b=self.inventory();self.h.cursor=(0,0,0);edit=self.f.step(op='text_container_text_exact',target_id=b['target_id'],expected_hash=b['hash'],expected_text=b['text'],new_text='Box after',confirm_layout=True);self.assertTrue(edit['ok'],edit);self.assertIsNone(self.f.binding['undo_stack'][-1]);before=copy.deepcopy(self.f.binding['undo_stack']);self.navigate();self.assertEqual(self.f.binding['undo_stack'][:-1],before);self.assert_no_history()
 def test_public_HTTP_Undo_Redo_navigation_refusal_no_dispatch_and_debt_retained(self):
  self.navigate();before=copy.deepcopy(self.f.binding)
  async def request():
   # Exercise the real route/request validation/exception JSON serialization
   # without adding the optional TestClient/httpx2 dependency to portable CI.
   from fastapi import FastAPI
   from app.local_cli_router import build_local_cli_router
   app=FastAPI();app.include_router(build_local_cli_router(settings=None,interactive_sessions=None,service=self.f.service))
   for action in('undo','redo'):
    with self.subTest(action=action):
     path='/local-cli/'+action;body=json.dumps({'session_id':self.f.binding['session_id']}).encode();sent=[]
     scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
            'path':path,'raw_path':path.encode(),'root_path':'','query_string':b'',
            'headers':[(b'host',b'portable.invalid'),(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())],
            'server':('127.0.0.1',80),'client':('127.0.0.1',1)}
     async def receive():return {'type':'http.request','body':body,'more_body':False}
     async def send(message):sent.append(message)
     await app(scope,receive,send)
     reply=json.loads(b''.join(m.get('body',b'') for m in sent))
     self.assertEqual(sent[0]['status'],409,reply);self.assertEqual(self.history_calls,[])
  asyncio.run(request())
  self.assertEqual(self.f.binding,before);self.assertTrue(self.f.binding['dirty'])
 def test_legitimate_qualified_history_still_dispatches_when_no_navigation_barrier(self):
  result=self.f.step(op='text_replace_exact',expected_page=1,confirm_layout=True,old_text='2025',new_text='2026');self.assertTrue(result['ok'],result)
  undo=self.history('undo');self.assertTrue(undo['ok'],undo);self.assertIn('Undo',self.history_calls);redo=self.history('redo');self.assertTrue(redo['ok'],redo);self.assertIn('Redo',self.history_calls)
