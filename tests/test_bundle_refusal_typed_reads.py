"""Same-bundle initial typed position failures must not inherit semantic authority."""
import inspect,unittest
from tests.native_table_fakes import sample_document
from tests.test_bundle_step_local_refusal import style,owner_for
from tests.test_guarded_bundle_observations import run
class TypedRead(unittest.TestCase):
 def test_malformed_current_read_after_confirmed_prefix_stops_without_next_sdk(self):
  for method in ('get_pos','get_selected_pos'):
   with self.subTest(method=method):
    h=sample_document();first=style(h);o=owner_for(self,h);orig=getattr(h,method);mark=[]
    def getter(*a,**k):
     f=inspect.currentframe().f_back;inside=False
     while f:inside=inside or f.f_code.co_name=='_bundle_table_read';f=f.f_back
     if inside:mark.append(len(h.calls));return ('bad',)
     return orig(*a,**k)
    setattr(h,method,getter)
    result=run(o,first,{'op':'table_read','table_id':'tbl:999999'},{'op':'table_inventory'})
    self.assertTrue(mark);self.assertTrue(result['steps'][0]['ok']);self.assertTrue(o.session._native_custody_held,result);self.assertTrue(result['dirty']);self.assertNotIn('refusal_code',result['steps'][1]);self.assertEqual(len(h.calls),mark[0]);self.assertEqual(o.cleanup_events(),[])
if __name__=='__main__':unittest.main()
