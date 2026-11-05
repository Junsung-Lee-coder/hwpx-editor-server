"""Strict one-shot picture selection through real service/retained STA.
External COM observations are synthetic, never native qualification.
"""
import unittest
from typing import Any
from app import native_picture as picture
from tests.test_picture_n2_contract import Picture,owned,req,command,public_call

class ExclusivePicture(Picture):
    def __init__(self,selector: Any=True):
        super().__init__(shared=True)
        self.selector=selector;self.selection_calls=[];self.native_selected={self.ctrls[1].ident};self.executed_selection=None
        self.selector_entered=False;self.after_failed_selector=[]
        self.selected=self.ctrls[1]
    def SelectCtrl(self,ident,option):
        raise AssertionError('Superseded raw selector must not be entered')
    def Run(self,name):
        if name!='SelectCtrlFront':return super().Run(name)
        self.selection_calls.append((name,self.native_position));self.selector_entered=True
        if isinstance(self.selector,BaseException):raise self.selector
        if self.selector is not True:return self.selector
        return super().Run(name)
    @property
    def CurSelectedCtrl(self):
        if self.selector_entered and self.selector is not True:self.after_failed_selector.append('CurSelectedCtrl')
        return super().CurSelectedCtrl
    def Execute(self,name,pset):
        self.executed_selection=set(self.native_selected)
        return super().Execute(name,pset)

class ExclusiveSelection(unittest.TestCase):
    def test_one_documented_exclusive_selection_clears_prior_other_target(self):
        h=ExclusivePicture();request=req(h,'picture_layout_exact',mode='square');ident=request['target_id'].split(':')[1]
        with owned(h) as o:
            result=command(o,request)
            self.assertTrue(result['ok'],result)
            self.assertEqual(h.selection_calls,[('SelectCtrlFront',(0,0,1))])
            self.assertEqual(h.events.count('Run:Cancel'),1)
            self.assertEqual(h.executed_selection,{ident})
    def test_actual_service_witness_binds_entered_native_id_and_option(self):
        h=ExclusivePicture();events=[];request=req(h,'picture_layout_exact',mode='square');ident=request['target_id'].split(':')[1]
        with owned(h) as o:
            o.service._picture_call_witness=events.append
            result=command(o,request);self.assertTrue(result['ok'],result)
        # Historical method name retained; the task supersedes option=1 with
        # verified clearing, an exact anchor, and one documented Front action.
        entries=[e for e in events if e['phase']=='HAction.Run.SelectCtrlFront' and e['stage']=='before']
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0].get('arguments'),{'native_id':ident,'anchor':[0,0,1],'action':'SelectCtrlFront'})
    def test_selector_false_exception_and_malformed_return_latch_without_followon(self):
        for value in (False,None,1,'true',RuntimeError('external selector failure')):
            with self.subTest(value_type=type(value).__name__):
                h=ExclusivePicture(value);events=[]
                with owned(h) as o:
                    o.service._picture_call_witness=events.append
                    result,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
                    self.assertTrue(error is not None or (type(result) is dict and result['ok'] is False))
                    self.assertTrue(o.session._thread.is_alive());self.assertTrue(o.session._quarantined)
                    self.assertEqual(o.cleanup_events(),[])
                self.assertEqual(len(h.selection_calls),1)
                self.assertEqual(h.after_failed_selector,[])
                self.assertIsNone(h.executed_selection)
                self.assertEqual(events[-1]['phase'],'HAction.Run.SelectCtrlFront')
                self.assertIn(events[-1]['stage'],('invalid_return','exception'))
    def test_first_failed_native_census_getter_prevents_selection_and_later_getters(self):
        h=ExclusivePicture();events=[]
        def fail():raise RuntimeError('external GetCtrlInstID failure')
        h.ctrls[0].GetCtrlInstID=fail
        with owned(h) as o:
            o.service._picture_call_witness=events.append
            result,error=public_call(o,req(h,'picture_layout_exact',mode='square'))
            self.assertTrue(error is not None or (type(result) is dict and result['ok'] is False))
            self.assertTrue(o.session._quarantined);self.assertEqual(o.cleanup_events(),[])
        self.assertEqual(h.selection_calls,[]);self.assertIsNone(h.executed_selection)
        self.assertTrue(events[-1]['phase'].endswith('.GetCtrlInstID'));self.assertEqual(events[-1]['stage'],'exception')

if __name__=='__main__':unittest.main()
