import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib import request,error
from qwen_bench.core import KIT
from qwen_bench.profiles import load_profile,apply_profile
from qwen_bench.ui import Launcher,make_server,PAGE


class ProfileTests(unittest.TestCase):
    def test_freeze_profile_and_apply_only_supported_settings(self):
        loaded=load_profile(KIT/'profiles/example-careful.json')
        config=apply_profile({'options':{'endpoint':'http://localhost/v1'}},loaded,'direct')
        self.assertEqual(config['options']['temperature'],0)
        self.assertEqual(len(loaded['source_sha256']),64)
        self.assertEqual(len(loaded['agents_md_sha256']),64)
        self.assertEqual(config['profile_receipt']['runtime_effective_settings'],'not_independently_attested')
        with self.assertRaises(ValueError):apply_profile({},loaded,'codex')
    def test_unknown_setting_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'profile.json';p.write_text(json.dumps({'format':'svg-bench-profile/1','harness':'direct','settings':{'pretend_plugin_enabled':True}}))
            with self.assertRaises(ValueError):load_profile(p)


class UITests(unittest.TestCase):
    def test_javascript_has_escaped_not_literal_newline(self):
        self.assertNotIn("join('\n')",PAGE)
        self.assertIn("join('\\n')",PAGE)

    def test_effective_token_limit_logged_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            profile=Path(directory)/'profile.json'
            profile.write_text(json.dumps({'format':'svg-bench-profile/1','harness':'direct','settings':{'max_tokens':4096}}))
            for selected,expected,source in [('',16384,'token field'),(str(profile),4096,'saved profile')]:
                with self.subTest(profile=selected):
                    launcher=Launcher(directory)
                    observed=[]
                    def execute_stub(**kwargs):
                        observed.append((kwargs['config']['options']['max_tokens'],list(launcher.messages)))
                        return Path(directory),{}
                    with patch('qwen_bench.ui.execute',side_effect=execute_stub) as execute:
                        launcher.start({'endpoint':'http://localhost/v1','max_tokens':16384,'profile':selected})
                        launcher.thread.join(timeout=5)
                    self.assertFalse(launcher.thread.is_alive())
                    execute.assert_called_once()
                    self.assertEqual(observed,[(expected,[f'Effective output-token ceiling per model request: {expected} (from {source}; requested limit, not server-attested).'])])
                    self.assertIsNone(launcher.error)

    def test_launcher_auth_and_fixture_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            server,launcher,token=make_server(directory)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def post(path,data,auth=token):
                req=request.Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Bench-Token':auth})
                with request.urlopen(req,timeout=5) as response:return json.load(response)
            try:
                with request.urlopen(base,timeout=5) as response:page=response.read().decode()
                self.assertIn('Run benchmark',page);self.assertIn(token,page)
                with self.assertRaises(error.HTTPError) as denied:post('/start',{'harness':'fixture'},'wrong')
                self.assertEqual(denied.exception.code,403)
                self.assertTrue(post('/start',{'harness':'fixture','seconds':3})['started'])
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    status=post('/status',{})
                    if not status['busy']:break
                    time.sleep(.05)
                self.assertFalse(status['busy']);self.assertIsNone(status['error']);self.assertEqual(status['report'],'/report/report.html')
                with request.urlopen(base+status['report'],timeout=5) as response:report=response.read().decode()
                self.assertIn('synthetic_fixture',report)
                with self.assertRaises(error.HTTPError):request.urlopen(base+'/report/private-adapter-state/anything',timeout=5)
            finally:
                launcher.cancel.set();server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
