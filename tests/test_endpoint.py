import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from qwen_bench.endpoint import Endpoint, EndpointError, list_models
from qwen_bench.endpoint_adapter import Adapter

class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.posts=[]; self.responses=[]; self.models=['fake-model']; self.status=200
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,value,status=200):
                body=json.dumps(value).encode(); self.send_response(status); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                owner.assertEqual(self.path,'/v1/models')
                self.reply({'data':[{'id':x} for x in owner.models]})
            def do_POST(self):
                owner.assertEqual(self.path,'/v1/chat/completions')
                owner.posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.reply(owner.responses.pop(0),owner.status)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        (self.root/'workspace').mkdir(); (self.root/'state').mkdir()
        self.events=[]; self.adapter=Adapter(self.events.append)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def preflight(self,**options):
        req={'type':'preflight','checkpoint':None,'workspace':str(self.root/'workspace'),'state_dir':str(self.root/'state'),'benchmark_protocol':'v0.1','options':dict(endpoint=self.url,**options)}
        self.adapter.preflight(req);return req
    def turn_request(self,prompt='exact\nPrompt ☀',checkpoint='C1',session=None):
        data=prompt.encode()
        return {'type':'turn','checkpoint':checkpoint,'session_id':session,'prompt_base64':base64.b64encode(data).decode(),'prompt_sha256':hashlib.sha256(data).hexdigest(),'timeout_seconds':10}
    def response(self,calls=None,finish='stop',content='done'):
        message={'role':'assistant','content':content}
        if calls:message['tool_calls']=calls
        return {'choices':[{'message':message,'finish_reason':finish}],'usage':{'prompt_tokens':12,'completion_tokens':3}}
    def test_bad_tool_arguments_are_repair_evidence(self):
        self.preflight()
        self.responses=[self.response([{'id':'bad','type':'function','function':{'name':'read_file','arguments':'{broken'}}],'tool_calls',None),self.response()]
        self.adapter.turn(self.turn_request())
        self.assertEqual(self.events[-1]['type'],'turn.completed')
        self.assertIn('not valid JSON',self.posts[-1]['messages'][-1]['content'])
    def test_profile_payload_and_instruction_protection(self):
        (self.root/'workspace/AGENTS.md').write_text('Frozen profile instruction.\n')
        self.preflight(reasoning_effort='low',request_options={'chat_template_kwargs':{'enable_thinking':False}})
        self.responses=[self.response()];self.adapter.turn(self.turn_request())
        self.assertEqual(self.posts[0]['messages'][0]['role'],'system')
        self.assertEqual(self.posts[0]['reasoning_effort'],'low')
        self.assertEqual(self.posts[0]['chat_template_kwargs'],{'enable_thinking':False})
        for name in ('AGENTS.md','agents.md','AgEnTs.Md'):
            with self.assertRaises(EndpointError):self.adapter.tool('write_file',{'path':name,'content':'change'})
    def test_actual_core_endpoint_profile_trajectory(self):
        from qwen_bench.core import execute,KIT
        from qwen_bench.profiles import load_profile,apply_profile
        self.responses=[]
        for turn in range(4):
            w,h=(1024,1024) if turn==3 else (1440,960)
            svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}"/>'
            call={'id':f'write-{turn}','type':'function','function':{'name':'write_file','arguments':json.dumps({'path':'output/scene.svg','content':svg})}}
            self.responses.extend([self.response([call],'tool_calls',None),self.response()])
        config={'command':[sys.executable,str(KIT/'qwen_bench/endpoint_adapter.py')],'options':{'endpoint':self.url}}
        config=apply_profile(config,load_profile(KIT/'profiles/example-careful.json'),'direct')
        run,status=execute(runs_dir=self.root/'runs',mode='adapter',config=config,turn_seconds=5,print_fn=lambda _:None)
        self.assertEqual(status['trajectory'],'completed',status)
        self.assertEqual(status['machine_verification'],'verified',status)
        self.assertEqual(len(self.posts),8)
        self.assertEqual(self.posts[0]['messages'][0]['role'],'system')
        self.assertEqual(len([m for m in self.posts[-1]['messages'] if m['role']=='user']),4)
        manifest=json.loads((run/'manifest.json').read_text())
        self.assertEqual(manifest['adapter_preflight']['model'],'fake-model')
        self.assertEqual(json.loads((run/'profile-receipt.json').read_text())['runtime_effective_settings'],'not_independently_attested')
    def test_metadata_only_and_model_selection(self):
        self.preflight(); self.assertEqual(self.posts,[])
        self.assertIn('not yet proven',self.events[-1]['controls'])
        self.assertEqual(list_models(self.url),['fake-model'])
        self.models=['a','b']
        with self.assertRaises(EndpointError):Endpoint(self.url).select_model()
        self.assertEqual(Endpoint(self.url).select_model('b')[0],'b')
    def test_tools_exact_prompts_and_four_turn_history(self):
        self.preflight()
        calls=[{'id':'call-1','type':'function','function':{'name':'write_file','arguments':{'path':'a.svg','content':'<svg/>'}}}]
        self.responses=[self.response(calls,'tool_calls',None)]+[self.response() for _ in range(4)]
        for i in range(4):self.adapter.turn(self.turn_request(f'exact prompt {i}\n',f'C{i+1}',self.adapter.session))
        self.assertEqual((self.root/'workspace/a.svg').read_text(),'<svg/>')
        self.assertEqual([m['content'] for m in self.posts[-1]['messages'] if m['role']=='user'],[f'exact prompt {i}\n' for i in range(4)])
        self.assertEqual(self.posts[0]['messages'],[{'role':'user','content':'exact prompt 0\n'}])
        self.assertIsInstance(self.posts[1]['messages'][1]['tool_calls'][0]['function']['arguments'],str)
        self.assertEqual(self.events[-1]['usage']['requests'][0]['raw']['prompt_tokens'],12)
        self.assertEqual(len([e for e in self.events if e['type']=='turn.completed']),4)
        with self.assertRaises(EndpointError):self.adapter.turn(self.turn_request(session=self.adapter.session))
    def test_workspace_guards_and_tools(self):
        self.preflight()
        try: (self.root/'workspace/link').symlink_to(self.root/'state',target_is_directory=True)
        except OSError: self.skipTest('Symlink privilege unavailable on this platform')
        for path in ('../state/x','/tmp/x','link/x','a/../../x','C:/x'):
            with self.assertRaises(EndpointError):self.adapter.tool('write_file',{'path':path,'content':'bad'})
        self.adapter.tool('write_file',{'path':'safe/a.svg','content':'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="20" viewBox="0 0 10 20"/>'})
        self.assertTrue(self.adapter.tool('check_svg',{'path':'safe/a.svg','width':10,'height':20})['machine_gates_pass'])
        self.assertIn('<svg',self.adapter.tool('read_file',{'path':'safe/a.svg'})['content'])
        self.assertEqual(self.adapter.tool('list_files',{'path':'safe'})[0]['name'],'a.svg')
    def test_incomplete_and_api_errors_stop(self):
        self.preflight()
        for finish in ('length','content_filter','unknown'):
            self.responses=[self.response(finish=finish)]
            with self.assertRaises(EndpointError):self.adapter.turn(self.turn_request(session=self.adapter.session))
        self.assertFalse(any(e['type']=='turn.completed' for e in self.events))
        self.responses=[{'error':{'message':'secret'}}];self.status=500
        with self.assertRaisesRegex(EndpointError,'HTTP 500'):self.adapter.turn(self.turn_request(session=self.adapter.session))
    def test_request_bound(self):
        self.preflight(max_requests=1)
        self.responses=[self.response([{'id':'x','type':'function','function':{'name':'list_files','arguments':'{"path":"."}'}}],'tool_calls')]
        with self.assertRaisesRegex(EndpointError,'Request limit'):self.adapter.turn(self.turn_request())
        self.assertEqual(len(self.posts),1)
    def test_script_jsonl_absolute_path(self):
        req={'type':'preflight','checkpoint':None,'workspace':str(self.root/'workspace'),'state_dir':str(self.root/'state'),'options':{'endpoint':self.url}}
        self.responses=[self.response()]
        script=Path(__file__).resolve().parents[1]/'qwen_bench/endpoint_adapter.py'
        data='\n'.join(json.dumps(x) for x in [req,self.turn_request(),{'type':'shutdown','checkpoint':None}])+'\n'
        result=subprocess.run([sys.executable,str(script)],input=data,text=True,capture_output=True,cwd=self.root/'state',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        events=[json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(events[-1],{'type':'run.closed','checkpoint':None})
        self.assertEqual(events[-2]['type'],'turn.completed')
        self.assertEqual(result.stderr,'')
    def test_secret_not_saved_and_hash_failure(self):
        key='secret-should-never-be-saved'
        os.environ['ENDPOINT_TEST_SECRET']=key
        try:
            self.preflight(api_key_env='ENDPOINT_TEST_SECRET')
            self.responses=[self.response(content=key)]
            self.adapter.turn(self.turn_request())
            self.assertTrue(all(key not in p.read_text() for p in (self.root/'state').iterdir()))
            req=self.turn_request(session=self.adapter.session);req['prompt_sha256']='bad'
            with self.assertRaisesRegex(EndpointError,'hash'):self.adapter.turn(req)
        finally:del os.environ['ENDPOINT_TEST_SECRET']

if __name__=='__main__':unittest.main()
