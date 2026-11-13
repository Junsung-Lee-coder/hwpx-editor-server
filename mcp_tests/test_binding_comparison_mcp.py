"""MCP/HTTP/owner comparison diagnostics, private exact fixture operands only."""
import json
from unittest.mock import patch
from tests.test_binding_comparison_observation import mismatch,private,SECRET,ContainersHwp,CREATE
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
class BindingComparisonMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.model=None;old=Backend.do_POST
  def post(h):
   if h.path!='/local-cli/command-bundle':return old(h)
   payload=json.loads(h.rfile.read(int(h.headers['Content-Length'])));return h.reply(self.model.own.service.command_bundle(**payload))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_exact_comparison_boundary_reaches_wire_without_public_HML(self):
  self.model,events,head=mismatch(self,sid=SID)
  with self.model.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):r=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  self.assertTrue(r.is_error);d=r.structured_content['error']['details']['backend']['steps'][0]['failure_diagnostic']
  self.assertIn('native_binding_branch',d);self.assertEqual(d['native_binding_branch'],'full_signature');self.assertEqual(d['native_binding_phase'],'constructor.initial')
  self.assertNotEqual(d['native_binding_expected'],d['native_binding_observed']);self.assertNotIn(SECRET,json.dumps(r.structured_content));self.assertNotIn('"observed_hml":',json.dumps(r.structured_content))
  self.assertIn(SECRET,private(self.model)['observed_hml']);self.assertTrue(self.model.own.session._native_custody_held);self.assertEqual(self.model.own.cleanup_events(),[])
