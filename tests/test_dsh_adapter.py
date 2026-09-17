"""Unit contract tests plus optional installed-DSH synthetic integration (no inference)."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import sys
import subprocess
import textwrap
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from qwen_bench.dsh_adapter import Harness, DshError, resolve_options, install_for, main
from qwen_bench.adapter import CommandAdapter


class StubProxy:
    def __init__(self, endpoint, model, state, emit, overrides, max_requests=64):
        self.endpoint = endpoint
        self.key = 'synthetic-no-secret'
        self.sections = []
    def start(self): return self.endpoint.url
    def begin_section(self, checkpoint, timeout_seconds): self.sections.append(checkpoint)
    def end_section(self): pass
    def close(self): pass


def turn_request(number, session=None, prompt=None):
    prompt = prompt if prompt is not None else ('Prompt %s\n--literal $(not-a-command) Ω\n' % number).encode()
    return {'type': 'turn', 'checkpoint': 'C' + str(number), 'session_id': session,
            'prompt_base64': base64.b64encode(prompt).decode(), 'prompt_sha256': hashlib.sha256(prompt).hexdigest(),
            'timeout_seconds': 15}


class UnitTests(unittest.TestCase):
    def test_defaults_and_seed(self):
        result = resolve_options({'endpoint': 'http://localhost:1/v1', 'seed': 7})
        self.assertEqual((result['effort'], result['max_tokens'], result['temperature'], result['top_p']), ('xhigh', 65536, .8, .95))
        self.assertEqual(result['seed'], 7)
        self.assertFalse(result['vision'])
        self.assertTrue(resolve_options({'endpoint':'http://localhost:1/v1', 'vision':True})['vision'])

    def test_invalid_options(self):
        for values in ({'vision':'true'}, {'vision':1}, {'harness':'codex'}, {'max_requests':65}, {'max_tokens':True}, {'effort':'bogus'},
                       {'temperature':float('nan')}, {'seed':False}, {'context_window':0}, {'unknown':1},
                       {'request_options':{'messages':[]}}, {'request_options':{'max_tokens':123}},
                       {'request_options':{'bad':float('inf')}}):
            with self.subTest(values=values), self.assertRaises(DshError):
                resolve_options(dict(endpoint='http://localhost:1/v1', **values))

    def test_missing_install_does_not_boot(self):
        with patch('qwen_bench.dsh_adapter.shutil.which', return_value=None), self.assertRaises(DshError):
            install_for()

    def test_native_terminal_required(self):
        harness = Harness(lambda event: None)
        harness.ready = True; harness.native_session = 'session-fake'; harness.proxy = StubProxy(None,None,None,None,None)
        harness.process = types.SimpleNamespace(poll=lambda: None)
        harness.stop_native = lambda: None
        harness._send = lambda request: None
        request = turn_request(0)
        for reason in (None, 'error', 'cancelled'):
            harness._receive = lambda *args: {'status':'completed','native_terminal':{'type':'turn/end','data':{'reason':{'kind':reason}}}}
            with self.assertRaises(DshError): harness.turn(request)
        self.assertEqual(harness.turns, 0)

    def test_changed_session_and_checkpoint(self):
        harness = Harness(lambda event: None); harness.native_session='session-right'
        for event in ({'type':'turn.completed','checkpoint':'C0','session_id':'session-wrong'},
                      {'type':'turn.completed','checkpoint':'C1','session_id':'session-right'}):
            harness.events.put(('event',event))
            with self.assertRaises(DshError): harness._receive('turn.completed','C0',time.monotonic()+1)

    def test_timeout_and_bad_prompt(self):
        harness = Harness(lambda event: None)
        with self.assertRaises(DshError): harness._receive('native.ready',None,time.monotonic()-.1)
        harness.ready=True
        request=turn_request(0); request['prompt_sha256']='bad'
        with self.assertRaises(DshError): harness.turn(request)

    def test_public_native_events_and_main_errors_redact_known_keys(self):
        secrets=('synthetic-upstream-key', 'synthetic-proxy-key')
        events=[]; harness=Harness(events.append); harness.public_secrets.update(secrets)
        for kind in ('tool.started','tool.completed','assistant.message','error'):
            harness.emit({'type':kind,'checkpoint':'C0','nested':[{'text':' '.join(secrets)}]})
        self.assertNotIn(secrets[0],json.dumps(events)); self.assertNotIn(secrets[1],json.dumps(events))
        self.assertIn('[REDACTED]',json.dumps(events))
        def factory(emit):
            harness=Harness(emit); harness.public_secrets.update(secrets)
            def fail(request): raise DshError(' '.join(secrets))
            harness.preflight=fail
            return harness
        output=io.StringIO()
        with patch('qwen_bench.dsh_adapter.Harness',side_effect=factory), \
             patch('qwen_bench.dsh_adapter.enable_child_reaping'), \
             patch('qwen_bench.dsh_adapter.signal.signal'), \
             patch('sys.stdin',types.SimpleNamespace(buffer=io.BytesIO(b'{"type":"preflight"}\n'))), \
             patch('sys.stdout',output):
            self.assertEqual(main(),1)
        event=json.loads(output.getvalue())
        self.assertEqual(event['type'],'error')
        self.assertEqual(event['message'],'[REDACTED] [REDACTED]')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux descendant-reaping check')
    def test_cleanup_reaps_adopted_child_after_native_parent_exit(self):
        # Run subreaper state in a dedicated process, never the shared test runner.
        code=textwrap.dedent("""
            import os, signal, subprocess, sys
            from pathlib import Path
            sys.path.insert(0,sys.argv[1])
            from qwen_bench.dsh_adapter import Harness, direct_children, enable_child_reaping
            enable_child_reaping()
            unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
            harness=Harness(lambda event: None)
            harness.child_baseline=direct_children()
            child=None
            try:
                spawn="import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)"
                harness.process=subprocess.Popen([sys.executable,'-c',spawn],start_new_session=True,
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                child=int(harness.process.stdout.readline())
                assert harness.process.wait(timeout=3)==0
                assert child in direct_children(), 'Detached child was not adopted'
                harness.close()
                assert not Path('/proc/'+str(child)).exists(), 'Adopted child survived cleanup'
                try: os.waitpid(child,os.WNOHANG)
                except ChildProcessError: pass
                else: raise AssertionError('Adopted child was not reaped')
                assert unrelated.poll() is None, 'Unrelated baseline child was stopped'
            finally:
                harness.close()
                if child:
                    try: os.kill(child,signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: os.waitpid(child,0)
                    except ChildProcessError: pass
                unrelated.kill(); unrelated.wait(timeout=3)
        """)
        result=subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1])],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr[-3000:])


class SyntheticServer(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        raw=json.dumps({'data':[{'id':'synthetic-dsh','max_model_len':131072}]}).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(payload)
        if getattr(self.server, 'stall', False):
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers(); self.wfile.flush()
            self.server.started.set()
            self.connection.settimeout(8)
            try:
                self.server.disconnected = self.connection.recv(1) == b''
            except (OSError, TimeoutError):
                self.server.disconnected = False
            self.server.finished.set()
            return
        number=len(self.server.requests)
        chunks=[]
        def chunk(delta, finish=None):
            return {'id':'synthetic-'+str(number),'object':'chat.completion.chunk','created':1,'model':'synthetic-dsh',
                    'choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
        chunks.append(chunk({'role':'assistant','reasoning_content':'SYNTHETIC_REASONING_MARKER'}))
        if number==1:
            # Genuine stock DSH filesystem read, not a fabricated tool lifecycle.
            tools={entry['function']['name']:entry['function'] for entry in payload['tools']}
            self.server.tool_schemas=tools
            if getattr(self.server, 'image_path', None):
                tool='read_image'; args={'file_path':self.server.image_path}
            elif 'bash' in tools:
                tool='bash'; args={'command':getattr(self.server,'tool_command','printf synthetic-tool-result'), 'description':'Synthetic connector test'}
            else:
                tool=next(iter(tools)); args={}
            chunks.append(chunk({'tool_calls':[{'index':0,'id':'call_synthetic','type':'function',
                               'function':{'name':tool,'arguments':json.dumps(args)}}]}))
            chunks.append(chunk({},'tool_calls'))
        else:
            chunks.append(chunk({'content':'Synthetic response '+str(number)+getattr(self.server,'reflection','')}))
            chunks.append(chunk({},'stop'))
        chunks.append({'id':'synthetic-'+str(number),'object':'chat.completion.chunk','created':1,'model':'synthetic-dsh',
                       'choices':[], 'usage':{'prompt_tokens':31+number,'completion_tokens':11,'total_tokens':42+number}})
        raw=(''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n').encode()
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)


@unittest.skipUnless(shutil.which('dsh') and shutil.which('node'), 'Installed DSH and Node.js unavailable; native integration not_run')
class NativeIntegration(unittest.TestCase):
    def test_vision_image_reaches_native_provider(self):
        with tempfile.TemporaryDirectory(prefix='svg-dsh-vision-') as temp:
            root=Path(temp); work=root/'work'; state=root/'state'; work.mkdir(); state.mkdir()
            (work/'pixel.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP8z0AaYCJR/aiGUQ1DSAMAQC4BH2bjRnMAAAAASUVORK5CYII='))
            server=ThreadingHTTPServer(('127.0.0.1',0),SyntheticServer)
            server.requests=[]; server.tool_schemas={}; server.image_path='pixel.png'
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            events=[]; harness=Harness(events.append)
            try:
                harness.preflight({'type':'preflight','benchmark_protocol':'v0.3-portable','workspace':str(work),
                    'state_dir':str(state),'options':{'harness':'dsh','endpoint':'http://127.0.0.1:%s/v1'%server.server_port,
                    'model':'synthetic-dsh','max_tokens':1024,'vision':True}})
                self.assertTrue(events[-1]['resolved']['vision'])
                harness.turn(turn_request(0,harness.session))
                harness.close(graceful=True)
                self.assertEqual(len(server.requests),2)
                images=[part for message in server.requests[1]['messages']
                        if isinstance(message.get('content'),list) for part in message['content']
                        if part.get('type')=='image_url']
                self.assertEqual(len(images),1, str([e for e in events if e['type']=='tool.completed'])[:2000])
                self.assertTrue(images[0]['image_url']['url'].startswith('data:image/png;base64,'))
                self.assertEqual([e['status'] for e in events if e['type']=='turn.completed'],['completed'])
            finally:
                harness.close(); server.shutdown(); server.server_close(); thread.join(2)

    def test_four_turns_native_events_and_reasoning_replay(self):
        with tempfile.TemporaryDirectory(prefix='svg-dsh-synthetic-') as temp:
            root=Path(temp); work=root/'work'; state=root/'state'; work.mkdir(); state.mkdir()
            server=ThreadingHTTPServer(('127.0.0.1',0),SyntheticServer)
            server.requests=[]; server.tool_schemas={}
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            events=[]; harness=Harness(events.append)
            try:
                with patch.dict(os.environ, {'TEST_UPSTREAM_SECRET':'synthetic-upstream-secret'}):
                    harness.preflight({'type':'preflight','benchmark_protocol':'v0.3-portable','workspace':str(work),
                        'state_dir':str(state),'options':{'harness':'dsh','endpoint':'http://127.0.0.1:%s/v1'%server.server_port,
                        'model':'synthetic-dsh','max_tokens':1024,'api_key_env':'TEST_UPSTREAM_SECRET'}})
                self.assertNotIn('TEST_UPSTREAM_SECRET',harness.env)
                self.assertEqual(harness.env['SVG_BENCH_DSH_KEY'],harness.proxy.key)
                reflected=('synthetic-upstream-secret',harness.proxy.key)
                server.reflection=' '.join(reflected)
                server.tool_command="printf '%s' '"+server.reflection+"'"
                self.assertEqual(server.requests, [], 'Preflight must not infer')
                self.assertEqual(events[-1]['status'],'passed')
                pid=harness.process.pid
                for number in range(4):
                    harness.turn(turn_request(number,harness.session))
                    self.assertEqual(harness.process.pid,pid)
                harness.close(graceful=True)
                terminals=[event for event in events if event['type']=='turn.completed']
                self.assertEqual(len(terminals),4)
                self.assertEqual(len({event['session_id'] for event in terminals}),1)
                self.assertEqual(len(server.requests),5)
                tools=[event for event in events if event['type']=='tool.completed']
                self.assertEqual(len(tools),1)
                self.assertIn('bash',server.tool_schemas)
                self.assertFalse(set(server.tool_schemas) & {'subagent', 'subagent_fork', 'list_agents', 'web_search', 'web_fetch', 'workflow', 'ralph'})
                self.assertTrue(any(event['type']=='telemetry' for event in events))
                self.assertNotIn('SYNTHETIC_REASONING_MARKER', json.dumps(events))
                for secret in reflected: self.assertNotIn(secret,json.dumps(events))
                self.assertIn('[REDACTED]',json.dumps(events))
                self.assertEqual(server.requests[0]['max_tokens'], 1024)
                self.assertEqual(server.requests[0]['reasoning_effort'], 'xhigh')
                self.assertTrue(all(event['usage']['steps'] for event in terminals))
                for number in range(4):
                    users=[message['content'] for message in server.requests[number+1]['messages'] if message['role']=='user']
                    expected=base64.b64decode(turn_request(number)['prompt_base64']).decode()
                    self.assertIn(expected,users)
                prior=[message for message in server.requests[1]['messages'] if message['role']=='assistant']
                self.assertTrue(any(message.get('reasoning_content')=='SYNTHETIC_REASONING_MARKER' for message in prior), prior)
                raw=(state/'dsh-native/native-events.jsonl').read_text()
                self.assertIn('tool/call',raw); self.assertIn('tool/result',raw)
                self.assertTrue((state/'dsh-native/home/sessions').is_dir())
            except Exception:
                # Bounded diagnostic only; all synthetic values, no credentials.
                stderr=state/'dsh-native/native-stderr.log'
                if stderr.exists(): print('SYNTHETIC STDERR TAIL:',stderr.read_text()[-3000:],file=sys.stderr)
                stdout=state/'dsh-native/native-stdout.log'
                if stdout.exists(): print('SYNTHETIC STDOUT TAIL:',stdout.read_text()[-7000:],file=sys.stderr)
                raise
            finally:
                harness.close()
                server.shutdown(); server.server_close(); thread.join(1)

    def test_command_four_exact_protocol_prompts_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix='svg-dsh-command-') as temp:
            root=Path(temp); work=root/'work'; state=root/'private-adapter-state'
            work.mkdir(); state.mkdir(); (root/'raw').mkdir()
            server=ThreadingHTTPServer(('127.0.0.1',0),SyntheticServer)
            server.requests=[]; server.tool_schemas={}
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            events=[]; kit=Path(__file__).resolve().parents[1]
            adapter=CommandAdapter([sys.executable,str(kit/'qwen_bench/dsh_adapter.py')],
                                   root,lambda kind,event: events.append((kind,event)))
            try:
                adapter.send({'type':'preflight','benchmark_protocol':'v0.3-portable','workspace':str(work),
                    'state_dir':str(state),'options':{'harness':'dsh','endpoint':'http://127.0.0.1:%s/v1'%server.server_port,
                    'model':'synthetic-dsh','max_tokens':65536,'temperature':.8,'top_p':.95,
                    'request_options':{'top_k':20,'min_p':0,'presence_penalty':0,'repetition_penalty':1,
                    'chat_template_kwargs':{'enable_thinking':True,'preserve_thinking':True,'reasoning_effort':'xhigh'}}}})
                receipt=adapter.receive('preflight.completed',None,30)
                self.assertEqual(server.requests, [])
                session=None
                for number,name in enumerate(('INITIAL_PROMPT.md','C1.txt','C2.txt','CHANGE_REQUEST.md')):
                    prompt=(kit/'protocol/v0.3-portable'/name).read_bytes()
                    adapter.send(turn_request(number,session,prompt))
                    event=adapter.receive('turn.completed','C'+str(number),30)
                    self.assertEqual(event['prompt_sha256'],hashlib.sha256(prompt).hexdigest())
                    self.assertEqual(event['native_terminal']['data']['reason']['kind'],'completed')
                    if session: self.assertEqual(event['session_id'],session)
                    session=event['session_id']
                self.assertEqual(session,receipt['native_session_id'])
                adapter.shutdown()
                self.assertEqual(adapter.process.returncode,0)
                self.assertEqual(len(server.requests),5)
                self.assertEqual(server.requests[0]['top_k'],20)
                self.assertEqual(server.requests[0]['max_tokens'],65536)
                self.assertTrue(server.requests[0]['chat_template_kwargs']['preserve_thinking'])
                self.assertNotIn('SYNTHETIC_REASONING_MARKER',json.dumps(events))
            finally:
                adapter.close()
                server.shutdown(); server.server_close(); thread.join(1)

    def test_command_entrypoint_and_outer_cancellation(self):
        with tempfile.TemporaryDirectory(prefix='svg-dsh-cancel-') as temp:
            root=Path(temp); work=root/'work'; state=root/'private-adapter-state'
            work.mkdir(); state.mkdir(); (root/'raw').mkdir()
            server=ThreadingHTTPServer(('127.0.0.1',0),SyntheticServer)
            server.requests=[]; server.stall=True; server.started=threading.Event(); server.finished=threading.Event()
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            events=[]
            adapter=CommandAdapter([sys.executable,str(Path(__file__).resolve().parents[1]/'qwen_bench/dsh_adapter.py')],
                                   root,lambda kind,event: events.append((kind,event)))
            native_pid=None
            try:
                adapter.send({'type':'preflight','benchmark_protocol':'v0.3-portable','workspace':str(work),
                    'state_dir':str(state),'options':{'harness':'dsh','endpoint':'http://127.0.0.1:%s/v1'%server.server_port,
                    'model':'synthetic-dsh','max_tokens':1024}})
                receipt=adapter.receive('preflight.completed',None,30)
                self.assertEqual(receipt['version'], '0.1.5-rc.2')
                self.assertEqual(server.requests, [])
                if sys.platform.startswith('linux'):
                    children=Path('/proc/%s/task/%s/children'%(adapter.process.pid,adapter.process.pid)).read_text().split()
                    self.assertEqual(len(children),1)
                    native_pid=int(children[0])
                request=turn_request(0); request['timeout_seconds']=900
                adapter.send(request)
                self.assertTrue(server.started.wait(15), 'Native DSH never requested the synthetic model')
                before=time.monotonic(); adapter.close()
                self.assertLess(time.monotonic()-before,4)
                self.assertIsNotNone(adapter.process.returncode)
                if native_pid:
                    self.assertFalse(Path('/proc/%s'%native_pid).exists(), 'Native child not terminated and reaped')
                self.assertTrue(server.finished.wait(3), 'Upstream connection remained open after stop')
                self.assertTrue(server.disconnected)
            finally:
                adapter.close()
                server.shutdown(); server.server_close(); thread.join(1)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux descendant-reaping check')
    def test_outer_cancellation_reaps_detached_native_shell_children(self):
        with tempfile.TemporaryDirectory(prefix='svg-dsh-tool-cancel-') as temp:
            root=Path(temp); work=root/'work'; state=root/'private-adapter-state'
            work.mkdir(); state.mkdir(); (root/'raw').mkdir()
            server=ThreadingHTTPServer(('127.0.0.1',0),SyntheticServer)
            server.requests=[]; server.tool_schemas={}
            server.tool_command="printf '%s' $$ > native-tool.pid; sleep 600 & printf '%s' $! > native-sleep.pid; wait"
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            adapter=CommandAdapter([sys.executable,str(Path(__file__).resolve().parents[1]/'qwen_bench/dsh_adapter.py')],
                                   root,lambda *args: None)
            try:
                adapter.send({'type':'preflight','benchmark_protocol':'v0.3-portable','workspace':str(work),
                    'state_dir':str(state),'options':{'harness':'dsh','endpoint':'http://127.0.0.1:%s/v1'%server.server_port,
                    'model':'synthetic-dsh','max_tokens':1024}})
                adapter.receive('preflight.completed',None,30)
                request=turn_request(0); request['timeout_seconds']=900
                adapter.send(request)
                until=time.monotonic()+10
                while not (work/'native-sleep.pid').exists() and time.monotonic()<until:
                    time.sleep(.02)
                self.assertTrue((work/'native-sleep.pid').exists(), 'Stock native bash tool did not start')
                # DSH's Linux sandbox may remap $$/$! into a PID namespace;
                # inspect the owning adapter's host descendants, not those IDs.
                pids=[]; pending=[adapter.process.pid]
                while pending:
                    pid=pending.pop()
                    for task in (Path('/proc')/str(pid)/'task').iterdir():
                        children=[int(value) for value in (task/'children').read_text().split()]
                        pids.extend(children); pending.extend(children)
                self.assertGreaterEqual(len(pids),3)
                before=time.monotonic(); adapter.close()
                self.assertLess(time.monotonic()-before,4)
                for pid in pids:
                    self.assertFalse(Path('/proc/%s'%pid).exists(), 'Native shell descendant not terminated/reaped: '+str(pid))
            finally:
                adapter.close()
                server.shutdown(); server.server_close(); thread.join(1)


if __name__ == '__main__': unittest.main()
