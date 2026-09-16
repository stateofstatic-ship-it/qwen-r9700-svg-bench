import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from qwen_bench.core import KIT, aggregate, capture, execute, safe_config
from qwen_bench.checks import inspect


class KitTests(unittest.TestCase):
    def run_fixture(self, mode='success', timeout=2):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        config=json.loads((KIT/'examples/fixture.json').read_text());config['options']={'fixture_mode':mode}
        run,status=execute(runs_dir=temp.name,mode='fixture',config=config,turn_seconds=timeout,print_fn=lambda _:None)
        return run,status

    def test_complete_fixture_and_report(self):
        run,status=self.run_fixture()
        self.assertEqual(status['trajectory'],'completed');self.assertEqual(status['machine_verification'],'verified')
        self.assertEqual(status['process_exit'],0);self.assertEqual(len(status['sections']),4)
        for label in ('C0','C1','C2','C3'):
            p=run/'checkpoints'/label
            self.assertTrue((p/'scene.svg').exists());self.assertTrue((p/'events.jsonl').exists())
        report=(run/'report.html').read_text()
        self.assertIn('Content-Security-Policy',report);self.assertNotIn('<script',report)
        self.assertIn('not standardized renderer',report)
        self.assertEqual((run/'checkpoints/C1/prompt.txt').read_bytes(),b'Enhance.')
        self.assertEqual((run/'checkpoints/C2/prompt.txt').read_bytes(),b'ENHANCE!!!')

    def test_protocol_path_only_change_and_hashes(self):
        original=KIT/'protocol/v0.2';portable=KIT/'protocol/v0.3-portable'
        for name in ('INITIAL_PROMPT.md','CHANGE_REQUEST.md'):
            self.assertEqual((original/name).read_bytes().replace(b'/work/output/scene.svg',b'output/scene.svg'),(portable/name).read_bytes())
        for directory in (original,portable):
            for name,wanted in json.loads((directory/'manifest.json').read_text())['files'].items():
                self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),wanted)

    def test_failures_stop_preserve_and_never_become_full(self):
        for mode in ('malformed','wrong_checkpoint','nonzero','error','changed_session','timeout'):
            with self.subTest(mode=mode):
                run,status=self.run_fixture(mode,.2 if mode=='timeout' else 2)
                self.assertEqual(status['execution'],'failed');self.assertEqual(status['trajectory'],'incomplete')
                self.assertTrue((run/'report.html').exists());self.assertTrue((run/'raw/adapter.stdout.log').exists())
                self.assertLess(len(status['sections']),4)

    def test_missing_artifact_preserves_all_section_diagnostics(self):
        run,status=self.run_fixture('missing')
        self.assertEqual(status['trajectory'],'completed');self.assertEqual(status['machine_verification'],'partial')
        self.assertEqual(len(status['sections']),4)

    def test_subset_is_not_complete(self):
        status={'execution':'completed','sections':[{'checkpoint':'C0','execution':'completed','checks':{'machine_gates_pass':True}}]}
        aggregate(status,['C0']);self.assertEqual(status['trajectory'],'incomplete')
        self.assertEqual(status['machine_verification'],'partial')

    def test_reject_credentials(self):
        for config in ({'api_key':'secret'},{'command':['--api-key=secret']},{'endpoint':'http://user:password@localhost'}):
            with self.assertRaises(ValueError):safe_config(config)

    def test_reject_active_svg_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=root/'workspace';work.mkdir();(work/'output').mkdir();dest=root/'checkpoint';dest.mkdir()
            source=work/'output/scene.svg'
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="960" viewBox="0 0 1440 960"><script>alert(1)</script></svg>')
            result=inspect(source,1440,960);self.assertFalse(result['safe_preview'])
            source.unlink()
            try:source.symlink_to(root/'outside.svg')
            except OSError:self.skipTest('Symlink creation requires additional Windows privilege')
            self.assertEqual(capture(work,dest,(1440,960))['status'],'failed')

    def test_manual_stop_retains_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            replies=iter(['','stop'])
            run,status=execute(runs_dir=directory,mode='manual',input_fn=lambda _:next(replies),print_fn=lambda _:None)
            self.assertEqual(status['execution'],'interrupted');self.assertTrue((run/'report.html').exists())
            self.assertEqual(status['sections'][0]['checkpoint'],'C0')


if __name__=='__main__':unittest.main()
