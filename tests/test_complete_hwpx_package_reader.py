"""Complete package custody, unlike paragraph-only static projection. SYNTHETIC cases."""
from pathlib import Path
from io import BytesIO
import tempfile,unittest,warnings,zipfile
from unittest.mock import patch
from local_cli_v1 import hwpx_reader as reader

class CompletePackageReader(unittest.TestCase):
 def package(self,entries):
  d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup);p=Path(d.name)/'synthetic.hwpx'
  with warnings.catch_warnings():
   warnings.simplefilter('ignore',UserWarning)
   with zipfile.ZipFile(p,'w') as z:
    for name,b in entries:z.writestr(name,b)
  return p
 def read(self,p):return reader.read_package_members(p)
 def test_complete_members_include_unknown_scripts_empty_and_directory_entries(self):
  rows=[('mimetype',b'application/hwp+zip'),('Contents/section0.xml',b'<section><!--keep--><?keep x?><unknown/></section>'),('Scripts/unknown.bin',b'\x00\x01raw'),('empty',b''),('directory/',b'')]
  self.assertEqual(self.read(self.package(rows)),dict(rows))
 def test_duplicate_and_case_or_unicode_aliases_are_rejected(self):
  for names in [('x','x'),('BinData/x.png','bindata/X.PNG'),('cafe\u0301','caf\u00e9')]:
   with self.subTest(names=names),self.assertRaises(reader.HwpxReadError):self.read(self.package([(n,b'x') for n in names]))
 def test_file_directory_aliases_cannot_hide_a_member(self):
  for names in [('x','x/'),('Dir/','dir')]:
   with self.subTest(names=names),self.assertRaises(reader.HwpxReadError):self.read(self.package([(n,b'') for n in names]))
 def test_absolute_traversal_backslash_and_empty_components_are_rejected(self):
  for name in ('/abs','../outside','A/../b','A\\b','A//b','A/./b','C:evil'):
   with self.subTest(name=name),self.assertRaises(reader.HwpxReadError):self.read(self.package([(name,b'x')]))
 def test_malformed_unknown_xml_is_not_dropped_by_section_projection(self):
  p=self.package([('Contents/section0.xml',b'<section/>'),('Unknown/settings.xml',b'<bad>')])
  with self.assertRaises(reader.HwpxReadError):self.read(p)
 def test_doctype_after_long_prefix_and_utf16_are_rejected(self):
  for data in (b' '*5000+b'<!DOCTYPE x><x/>','<!DOCTYPE x><x/>'.encode('utf-16')):
   with self.subTest(data=len(data)),self.assertRaises(reader.HwpxReadError):self.read(self.package([('Contents/unknown.xml',data)]))
 def test_symlink_member_is_rejected_without_following_it(self):
  info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=(0o120777<<16)
  with self.assertRaises(reader.HwpxReadError):self.read(self.package([(info,b'/foreign')]))
 def test_aggregate_expansion_and_member_count_are_bounded(self):
  p=self.package([('a',b'123'),('b',b'456')])
  with patch.object(reader,'MAX_PACKAGE_TOTAL_BYTES',5),self.assertRaises(reader.HwpxReadError):self.read(p)
  with patch.object(reader,'MAX_PACKAGE_MEMBERS',1),self.assertRaises(reader.HwpxReadError):self.read(p)
if __name__=='__main__':unittest.main()
