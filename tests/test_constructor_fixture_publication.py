"""Exact native constructor fixture admission without widening runtime source rules."""
from __future__ import annotations
import hashlib,json,os,shutil,stat,subprocess,tempfile,unittest,zipfile
from pathlib import Path
from scripts import source_bundle_policy as policy
from tests.test_source_bundle_test_fixtures import load_script,git,sha256
ROOT=Path(__file__).resolve().parents[1]
FIXED={
 'tests/fixtures/native_text_box_constructor/native-fields-seed.hwpx':(18028,'94713acdd8052a7c886482d005f6b9750356c950e91691b95b3159b0e1d96243'),
 'tests/fixtures/native_text_box_constructor/native-inline-40x20.hwpx':(18827,'d3e89abb1e9e9d381c69de9d85c0e4c8ba5973a08ff0653803ea31730a2e2971'),
}
class FixedConstructorFixturePublicationTests(unittest.TestCase):
 def setUp(self):
  self.tmp_owner=tempfile.TemporaryDirectory();self.addCleanup(self.tmp_owner.cleanup);self.tmp=Path(self.tmp_owner.name);self.builder=load_script('build_source_bundle.py');self.verifier=load_script('verify_source_bundle.py');self.bindings={}
 def source(self,name='source',*,ignored_parent=False):
  root=self.tmp/name;root.mkdir();(root/'app').mkdir();(root/'app/main.py').write_text('VALUE = 1\n');ignore=(ROOT/'.gitignore').read_text();(root/'.gitignore').write_text(('fixtures/\n' if ignored_parent else '')+ignore)
  for name,(size,digest) in FIXED.items():
   b=(ROOT/name).read_bytes();self.assertEqual(len(b),size);self.assertEqual(hashlib.sha256(b).hexdigest(),digest);p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
  return root
 def build(self,source,label='bundle',*,git_mode=False):
  out=self.tmp/label;out.mkdir();kw={} if git_mode else {'commit':'a'*40,'tree':'b'*40};self.bindings[str(out)]=(git(source,'rev-parse','HEAD'),git(source,'rev-parse','HEAD^{tree}')) if git_mode else ('a'*40,'b'*40);manifest=self.builder.build_source_bundle(source_root=source,archive_path=out/'source.zip',manifest_path=out/'manifest.json',repository='fixture-test',**kw);return out,manifest
 def verify(self,out,destination='extract'):
  commit,tree=self.bindings[str(out)];return self.verifier.verify_source_bundle(archive_path=out/'source.zip',manifest_path=out/'manifest.json',destination=self.tmp/destination,expected_repository='fixture-test',expected_commit=commit,expected_tree=tree,expected_manifest_sha256=sha256(out/'manifest.json'),expected_archive_sha256=sha256(out/'source.zip'))
 def assert_namespace_and_bytes(self,out,m,dest):
  self.assertEqual(m['file_count'],len(m['files']));self.assertEqual(m['test_fixture_file_count'],2);self.assertEqual({x['path'] for x in m['test_fixture_files']},set(FIXED));self.assertFalse(set(FIXED)&{x['path'] for x in m['files']})
  for x in m['test_fixture_files']:
   self.assertEqual((x['size'],x['sha256']),FIXED[x['path']]);self.assertEqual(sha256(dest/x['path']),FIXED[x['path']][1])
 def wire_package(self):
  root=self.tmp/'wire-source';root.mkdir();(root/'main.py').write_text('VALUE = 1\n');out,m=self.build(root,'wire');payloads={i.filename:b for i,b in self.zip_items(out)}
  for name,(size,digest) in FIXED.items():payloads[name]=(ROOT/name).read_bytes()
  m['test_fixture_file_count']=2;m['test_fixture_files']=[{'path':n,'size':size,'sha256':digest} for n,(size,digest) in FIXED.items()];self.rewrite(out,m,payloads);return out,m,payloads
 def zip_items(self,out):
  with zipfile.ZipFile(out/'source.zip') as z:return [(i,z.read(i.filename)) for i in z.infolist()]
 def rewrite(self,out,m,payloads,symlink=None):
  with zipfile.ZipFile(out/'source.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
   for name,b in payloads.items():
    if name==symlink:
     i=zipfile.ZipInfo(name);i.create_system=3;i.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(i,b)
    else:z.writestr(name,b)
  m['archive_sha256']=sha256(out/'source.zip');(out/'manifest.json').write_text(json.dumps(m))
 def test_fixed_paths_fixture_only_and_all_other_document_paths_rejected(self):
  for name in FIXED:
   with self.subTest(path=name):self.assertTrue(policy.is_public_test_fixture_member(name));self.assertTrue(policy.is_prohibited_member(name))
  for name in ['tests/fixtures/other.hwpx','docs/native-fields-seed.hwpx','app/fixtures/native-fields-seed.hwpx','fixtures/native-fields-seed.hwpx','tests/fixtures/native_text_box_constructor/unrelated.hwpx','tests/fixtures/native_text_box_constructor/native-fields-seed.hwp','tests/fixtures/native_text_box_constructor/native-fields-seed.hwpx/extra','tests/fixtures/native_text_box_constructor/../native-fields-seed.hwpx','/tests/fixtures/native_text_box_constructor/native-fields-seed.hwpx','tests/fixtures/native_text_box_constructor/native-fields-seed.HWPX']:
   with self.subTest(path=name):self.assertFalse(policy.is_public_test_fixture_member(name))
 def test_receiver_exact_paths_fixture_role_only(self):
  for name in FIXED:
   with self.subTest(path=name):
    self.assertEqual(self.verifier._safe_test_fixture_member_name(name),name)
    with self.assertRaises(self.verifier.SourceBundleVerificationError):self.verifier._safe_member_name(name)
    for alias in (name.replace('/','\\'),name.replace('/native-fields','//native-fields') if 'native-fields'in name else name+'/..'):
     with self.assertRaises(self.verifier.SourceBundleVerificationError):self.verifier._safe_test_fixture_member_name(alias)
 def test_ordinary_git_add_reincludes_parent_and_exact_two_members(self):
  root=self.source(ignored_parent=True);git(root,'init','-q');other=root/'tests/fixtures/native_text_box_constructor/unrelated.hwpx';other.write_bytes(b'not-an-admitted-document');git(root,'add','--all');names=set(git(root,'ls-files').splitlines());self.assertTrue(set(FIXED)<=names);self.assertNotIn(str(other.relative_to(root)),names)
 def test_gitless_build_verify_extract_preserves_fixed_authority(self):
  out,m=self.build(self.source());self.verify(out);self.assert_namespace_and_bytes(out,m,self.tmp/'extract');self.assertEqual(m['identity_source'],'asserted-gitless');self.assertFalse(m['identity_verified'])
 def test_clean_git_build_verify_extract_preserves_fixed_authority(self):
  root=self.source();git(root,'init','-q');git(root,'add','--all');git(root,'commit','-q','-m','fixture source');out,m=self.build(root,git_mode=True);self.verify(out);self.assert_namespace_and_bytes(out,m,self.tmp/'extract');self.assertEqual(m['identity_source'],'git');self.assertTrue(m['identity_verified'])
 def test_builder_rejects_changed_fixed_bytes_without_trusting_filename(self):
  root=self.source();p=root/next(iter(FIXED));b=p.read_bytes();p.write_bytes(b[:-1]+bytes([b[-1]^1]))
  with self.assertRaises(self.builder.SourceBundleError):self.build(root)
 def test_verifier_rejects_changed_payload_even_with_rehashed_manifest(self):
  out,m,contents=self.wire_package();name=next(iter(FIXED));b=contents[name];contents[name]=b[:-1]+bytes([b[-1]^1]);next(x for x in m['test_fixture_files'] if x['path']==name)['sha256']=hashlib.sha256(contents[name]).hexdigest();self.rewrite(out,m,contents)
  with self.assertRaises(self.verifier.SourceBundleVerificationError):self.verify(out)
 def test_wire_positive_then_undeclared_runtime_unsafe_and_symlink_refuse(self):
  out,m,contents=self.wire_package();self.verify(out,'positive');original=json.loads(json.dumps(m))
  for case in ('undeclared','runtime','unsafe','symlink'):
   with self.subTest(case=case):
    mm=json.loads(json.dumps(original));cc=dict(contents);name=next(iter(FIXED));link=None
    if case=='undeclared':mm['test_fixture_files']=mm['test_fixture_files'][1:];mm['test_fixture_file_count']=1
    elif case=='runtime':entry=mm['test_fixture_files'].pop(0);mm['test_fixture_file_count']-=1;mm['files'].append(entry);mm['file_count']+=1
    elif case=='unsafe':cc['tests/fixtures/native_text_box_constructor/../../outside.hwpx']=cc.pop(name)
    else:link=name
    self.rewrite(out,mm,cc,link)
    with self.assertRaises(self.verifier.SourceBundleVerificationError):self.verify(out,'reject-'+case)
 def test_builder_rejects_fixed_path_symlink(self):
  root=self.source();name=next(iter(FIXED));p=root/name;target=self.tmp/'outside.hwpx';target.write_bytes(p.read_bytes());p.unlink();p.symlink_to(target)
  with self.assertRaises(self.builder.SourceBundleError):self.build(root)
 def test_windows_manifest_and_copy_remain_runtime_files_only(self):
  s=(ROOT/'scripts/windows_install_common.psm1').read_text();body=s[s.index('function Get-SourceManifest'):s.index('\nfunction ',s.index('function Get-SourceManifest')+1)];self.assertIn('$manifest.files',body);self.assertNotIn('test_fixture_files',body);self.assertIn('fixtures',body);self.assertIn("'.hwpx'",s);self.assertIn("'.hwpx'",s[s.index('function Test-ProhibitedSourceMember'):s.index('# Windows path checks')])

if __name__=='__main__':unittest.main()
