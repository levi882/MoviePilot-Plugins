import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[2]/'plugins.v3/felplayback/directories.py'
spec=importlib.util.spec_from_file_location('fel_directory_test',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.bindings=[{'id':'sata','mp_root':'/example/hdd/Movie'},{'id':'nvme','mp_root':'/example/ssd/Movie'}]
        self.rule={'name':'Existing MP movie rule','media_type':'电影','storage':'local','library_storage':'local','library_path':'/example/hdd/Movie/','download_path':'/example/hdd/ExistingDownloads/','priority':3}
    def resolve(self,rows):return m.resolve_directories(rows,self.bindings,'/config/plugins/FelPlayback/queue')
    def test_reads_original_rule_without_replacing_download_root(self):
        rows,issues=self.resolve([self.rule]);self.assertFalse(issues)
        self.assertEqual(rows[0]['download_root'],'/example/hdd/ExistingDownloads')
        self.assertNotIn('KodiPlayback',rows[0]['staging_root'])
    def test_rule_changes_are_read_again(self):
        first,_=self.resolve([self.rule]);self.rule['download_path']='/example/hdd/NewDownloads'
        second,_=self.resolve([self.rule]);self.assertNotEqual(first[0]['download_root'],second[0]['download_root'])
    def test_tv_music_and_unmounted_roots_do_not_redirect(self):
        rows,issues=self.resolve([{**self.rule,'media_type':'电视剧'},{**self.rule,'library_path':'/example/unmounted/Movie'}]);self.assertEqual(rows,[]);self.assertIn('未挂载',issues[0])
    def test_ambiguous_download_rule_does_not_choose_arbitrarily(self):
        rows,issues=self.resolve([self.rule,{**self.rule,'download_path':'/different'}]);self.assertEqual(rows,[]);self.assertIn('无法唯一匹配',issues[0])
    def test_duplicate_same_rule_is_safe(self):
        rows,issues=self.resolve([self.rule,self.rule]);self.assertEqual(len(rows),1);self.assertFalse(issues)
    def test_remote_storage_not_treated_as_local(self):
        rows,issues=self.resolve([{**self.rule,'storage':'115'}]);self.assertEqual(rows,[]);self.assertTrue(issues)
if __name__=='__main__':unittest.main()
