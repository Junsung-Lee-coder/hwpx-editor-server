"""Read-only-scope supplemental audit: no SDK exception identity is no-effect proof."""
import inspect,unittest
from app import native_numbering as nn
from tests.native_table_fakes import sample_document
from tests.test_bundle_step_local_refusal import style,owner_for
from tests.test_guarded_bundle_observations import run
class ForgedRead(unittest.TestCase):
    def test_same_bundle_forged_sdk_read_refusal_is_technical(self):
        h=sample_document();step=style(h);o=owner_for(self,h);orig=h.get_text_file;fired=[]
        def get(*a,**k):
            frame=inspect.currentframe().f_back;in_read=False
            while frame:
                in_read=in_read or frame.f_code.co_name=='_bundle_table_read';frame=frame.f_back
            if in_read:
                fired.append(len(h.calls));raise nn.NativeNumberingRefusal('synthetic forged SDK read refusal',code='table_not_found')
            return orig(*a,**k)
        h.get_text_file=get
        result=run(o,step,{'op':'table_read','table_id':'tbl:999999'})
        self.assertTrue(fired)
        self.assertTrue(result['steps'][0]['ok'])
        self.assertTrue(o.session._native_custody_held,result)
        self.assertNotIn('refusal_code',result['steps'][1])
        self.assertEqual(len(h.calls),fired[0])
if __name__=='__main__':unittest.main()
