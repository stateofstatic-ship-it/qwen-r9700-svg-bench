import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from qwen_bench.harness_adapter import Harness, HarnessError

FAKE = '''#!/usr/bin/env python3
import json, pathlib, sys, time
args=sys.argv[1:]
if '--version' in args: print('fake 1'); sys.exit()
if '--help' in args: print('--json --skip-git-repo-check --config --format --session --model --variant SESSION_ID'); sys.exit()
p=pathlib.Path('calls.jsonl')
with p.open('a') as f: f.write(json.dumps({'args':args,'prompt':sys.stdin.read()})+'\\n')
n=len(p.read_text().splitlines())
mode=pathlib.Path('mode').read_text() if pathlib.Path('mode').exists() else 'ok'
if mode=='timeout': time.sleep(10)
if mode=='malformed': print('not json'); sys.exit()
sid='11111111-1111-4111-8111-111111111111' if 'exec' in args else 'ses_abcdef123'
if mode=='changed' and n>1: sid=sid.replace('1','2').replace('abc','xyz')
def emit(**kw): print(json.dumps(kw))
if 'exec' in args:
 emit(type='thread.started',thread_id=sid)
 if mode=='failed': emit(type='turn.failed'); sys.exit()
 if mode!='missing': emit(type='turn.completed',usage={'input_tokens':12})
else:
 emit(type='step_start',sessionID=sid,part={})
 if mode=='failed': emit(type='error',sessionID=sid); sys.exit()
 if mode!='missing': emit(type='step_finish',sessionID=sid,part={'reason':'stop','tokens':{'input':12}})
if mode=='nonzero': sys.exit(2)
'''


@unittest.skipUnless(os.name=='posix','Fake native executables use a POSIX shebang; real Windows native integration is not validated')
class Connectors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'; self.work.mkdir()
        self.state = self.root / 'state'; self.state.mkdir()
        self.exe = self.root / 'fake harness'; self.exe.write_text(FAKE); self.exe.chmod(0o755)
        (self.work / 'mode').write_text('ok')
        self.events = []

    def start(self, kind='codex', **options):
        self.h = Harness(self.events.append)
        self.h.preflight({'benchmark_protocol':'v0.3-portable','workspace':str(self.work), 'state_dir':str(self.state),
                          'options':dict(harness=kind,executable=str(self.exe),**options)})
        self.assertFalse((self.work/'calls.jsonl').exists())
        self.assertEqual(self.events[-1]['status'], 'passed')

    def turn(self, number=0, timeout=2):
        prompt=b'--bad $(touch injected)\nWrite output/scene.svg\n'
        self.h.turn({'checkpoint':'C'+str(number),'session_id':self.h.session,
                     'prompt_base64':base64.b64encode(prompt).decode(),
                     'prompt_sha256':hashlib.sha256(prompt).hexdigest(),'timeout_seconds':timeout})

    def test_codex_four_turns(self):
        self.start(model='model; echo not-a-shell', effort='low')
        for number in range(4): self.turn(number)
        calls=[json.loads(x) for x in (self.work/'calls.jsonl').read_text().splitlines()]
        self.assertNotIn('resume',calls[0]['args'])
        for call in calls[1:]:
            self.assertIn('resume',call['args']); self.assertIn(self.h.session,call['args'])
        self.assertTrue(all(c['prompt'].startswith('--bad') for c in calls))
        self.assertTrue(all('model; echo not-a-shell' in c['args'] for c in calls))
        self.assertFalse((self.work/'injected').exists())
        self.assertEqual(len([e for e in self.events if e['type']=='turn.completed']),4)

    def test_opencode_four_turns(self):
        self.start('opencode', effort='high')
        for number in range(4): self.turn(number)
        calls=[json.loads(x)['args'] for x in (self.work/'calls.jsonl').read_text().splitlines()]
        self.assertNotIn('--session',calls[0])
        self.assertTrue(all(c[-2:]==['--session',self.h.session] for c in calls[1:]))
        self.assertEqual(json.loads(self.h.env['OPENCODE_CONFIG_CONTENT'])['share'],'disabled')

    def test_missing_terminal(self):
        self.start(); (self.work/'mode').write_text('missing')
        with self.assertRaises(HarnessError): self.turn()

    def test_failure(self):
        self.start(); (self.work/'mode').write_text('failed')
        with self.assertRaises(HarnessError): self.turn()

    def test_nonzero(self):
        self.start(); (self.work/'mode').write_text('nonzero')
        with self.assertRaises(HarnessError): self.turn()

    def test_malformed(self):
        self.start(); (self.work/'mode').write_text('malformed')
        with self.assertRaises(ValueError): self.turn()

    def test_session_change(self):
        self.start(); self.turn(); (self.work/'mode').write_text('changed')
        with self.assertRaisesRegex(HarnessError,'session changed'): self.turn(1)

    def test_timeout_preserves_trace(self):
        self.start(); (self.work/'mode').write_text('timeout')
        with self.assertRaisesRegex(HarnessError,'timed out'): self.turn(timeout=.2)
        self.assertTrue((self.state/'native-harness/C0.stdout.jsonl').exists())

    def test_persistent_outer_protocol(self):
        request={'type':'preflight','checkpoint':None,'benchmark_protocol':'v0.3-portable',
                 'workspace':str(self.work),'state_dir':str(self.state),
                 'options':{'harness':'codex','executable':str(self.exe)}}
        requests=[request]
        for number in range(4):
            prompt=b'Write output/scene.svg'
            requests.append({'type':'turn','checkpoint':'C'+str(number),
                             'session_id':None if number==0 else '11111111-1111-4111-8111-111111111111',
                             'prompt_base64':base64.b64encode(prompt).decode(),
                             'prompt_sha256':hashlib.sha256(prompt).hexdigest(),'timeout_seconds':2})
        requests.append({'type':'shutdown','checkpoint':None})
        result=subprocess.run([sys.executable,'-m','qwen_bench.harness_adapter'],
                              input=''.join(json.dumps(r)+'\n' for r in requests),
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([e['type'] for e in events],
                         ['preflight.completed']+['turn.completed']*4+['run.closed'])

    def test_actual_core_absolute_script_contract(self):
        kit=Path(os.environ.get('BENCHMARK_KIT_ROOT', str(Path(__file__).resolve().parents[1])))
        if not (kit/'qwen_bench/core.py').exists():
            self.skipTest('Set BENCHMARK_KIT_ROOT to the integration owner checkout')
        adapter=Path(__file__).resolve().parents[1]/'qwen_bench/harness_adapter.py'
        config={'command':[sys.executable,str(adapter)],
                'options':{'harness':'codex','executable':str(self.exe)}}
        code = """import json,sys
from qwen_bench.core import execute
run,status=execute(runs_dir=sys.argv[1],mode='adapter',config=json.loads(sys.argv[2]),
                   turn_seconds=5,print_fn=lambda _:None)
assert status['execution']=='completed',status
assert status['trajectory']=='completed',status
assert status['process_exit']==0,status
assert len(status['sections'])==4,status
preflight=json.loads((run/'preflight.json').read_text())
assert preflight['status']=='passed' and preflight['same_session_supported'] is True
assert preflight['candidate_workspace']=='.'
for section in status['sections']:
 assert section['terminal_event']['status']=='completed'
"""
        result=subprocess.run([sys.executable,'-c',code,str(self.root/'runs'),json.dumps(config)],
                              cwd=kit,env=dict(os.environ,PYTHONPATH=str(kit)),
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_endpoint_rejected_without_invocation(self):
        with self.assertRaisesRegex(HarnessError,'Responses'): self.start(endpoint='http://localhost:1234/v1')
        self.assertFalse((self.work/'calls.jsonl').exists())


if __name__ == '__main__': unittest.main()
