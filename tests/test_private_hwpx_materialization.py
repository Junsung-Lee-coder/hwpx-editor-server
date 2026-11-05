"""Prospective private-copy protocol tests; external SDK observations are SYNTHETIC."""
from pathlib import Path
import tempfile,unittest,zipfile
from app.local_cli_runtime import checked_save_hwp_as,LocalCliRuntimeError

class Raw:
 def __init__(self,path,result=True,retarget=False,dirty=True):
  self.path=str(path);self.dirty=dirty;self.result=result;self.retarget=retarget;self.events=[]
 @property
 def Path(self):self.events.append('Path');return self.path
 @property
 def IsModified(self):self.events.append('IsModified');return self.dirty
 def GetPos(self):self.events.append('GetPos');return (0,0,24)
 def SaveAs(self,path,fmt,arg):
  self.events.append(('SaveAs',path,fmt,arg))
  with zipfile.ZipFile(path,'w') as z:
   z.writestr('mimetype','application/hwp+zip');z.writestr('Contents/section0.xml','<section>SYNTHETIC</section>')
  self.dirty=False
  if self.retarget:self.path=path
  return self.result
class Hwp:
 def __init__(self,raw):self.hwp=raw

class PrivateMaterialization(unittest.TestCase):
 def prepare(self,**kw):
  d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup);root=Path(d.name)
  working=root/'working.hwpx';working.write_bytes(b'original input is not overwritten')
  raw=Raw(working,**kw);return root,working,raw,Hwp(raw)
 def take(self,root,working,hwp):
  return checked_save_hwp_as(hwp,root/'tentative.hwpx','HWPX',private_copy=True,working_path=working)
 def test_copy_binds_exact_format_args_and_never_discharges_logical_debt(self):
  root,working,raw,hwp=self.prepare();before=working.read_bytes();r=self.take(root,working,hwp)
  self.assertEqual([e for e in raw.events if isinstance(e,tuple)],[('SaveAs',str(root/'tentative.hwpx'),'HWPX','lock:false;export')])
  self.assertTrue(r['private_copy']);self.assertFalse(r['verified_user_save']);self.assertFalse(r['logical_debt_discharged'])
  self.assertTrue(r['native_before']['modified']);self.assertFalse(r['native_after']['modified'])
  self.assertEqual(r['native_after']['path'],str(working));self.assertEqual(working.read_bytes(),before)
 def test_retarget_is_immediate_uncertainty_without_following_dirty_or_position_getter(self):
  root,working,raw,hwp=self.prepare(retarget=True)
  with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
  where=next(i for i,e in enumerate(raw.events) if isinstance(e,tuple))
  self.assertEqual(raw.events[where+1:],['Path'])
 def test_false_none_and_int_return_stop_before_any_followon_getter(self):
  for result in (False,None,1):
   with self.subTest(result=result):
    root,working,raw,hwp=self.prepare(result=result)
    with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
    where=next(i for i,e in enumerate(raw.events) if isinstance(e,tuple))
    self.assertEqual(raw.events[where+1:],[])
 def test_bad_pre_dirty_is_not_coerced_and_save_is_not_entered(self):
  root,working,raw,hwp=self.prepare(dirty=1)
  with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
  self.assertEqual(raw.events,['Path','IsModified'])
 def test_wrong_owned_working_path_refuses_before_later_getter(self):
  root,working,raw,hwp=self.prepare();raw.path=str(root/'foreign.hwpx')
  with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
  self.assertEqual(raw.events,['Path'])
 def test_complete_unknown_xml_validation_stops_before_followon_native_getters(self):
  root,working,raw,hwp=self.prepare()
  original=raw.SaveAs
  def bad_output(path,fmt,arg):
   result=original(path,fmt,arg)
   with zipfile.ZipFile(path,'a') as z:z.writestr('Unknown/also-protected.xml','<broken>')
   return result
  raw.SaveAs=bad_output
  with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
  where=next(i for i,e in enumerate(raw.events) if isinstance(e,tuple))
  self.assertEqual(raw.events[where+1:],[])
 def test_private_copy_never_overwrites_existing_output(self):
  root,working,raw,hwp=self.prepare();out=root/'tentative.hwpx';out.write_bytes(b'keep')
  with self.assertRaises(LocalCliRuntimeError):self.take(root,working,hwp)
  self.assertEqual(raw.events,[]);self.assertEqual(out.read_bytes(),b'keep')
 def test_private_copy_rejects_other_format_before_sdk(self):
  root,working,raw,hwp=self.prepare()
  with self.assertRaises(LocalCliRuntimeError):checked_save_hwp_as(hwp,root/'x.pdf','PDF',private_copy=True,working_path=working)
  self.assertEqual(raw.events,[])
if __name__=='__main__':unittest.main()
