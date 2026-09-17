"""Loopback-only browser launcher; no uploads, cloud account or frontend build."""
import hmac
import http.server
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlparse,unquote
import webbrowser
from .core import KIT,execute,safe_config

PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Qwen × R9700</title>
<style>body{font:17px system-ui;max-width:780px;margin:auto;padding:24px;background:#f5f4fa;color:#222238}h1{line-height:1.1}section{background:white;padding:22px;border:1px solid #ddd;border-radius:12px;margin:18px 0}label{display:block;font-weight:600;margin-top:15px}input,select,button{font:inherit;padding:10px;box-sizing:border-box}input,select{width:100%;margin-top:6px}button{cursor:pointer;margin-top:16px;border-radius:6px;border:1px solid #6859b4;background:#f7f5ff;color:#322169}button.primary{background:#5840ad;color:white}button:disabled{opacity:.5;cursor:default}small{display:block;line-height:1.5;color:#555;margin-top:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:300px;overflow:auto}a{color:#4631a0}</style></head><body>
<h1>Qwen × R9700</h1><p>A four-part SVG coding test. Use the model you already have running.</p>
<section><label for="harness">Run through</label><select id="harness"><option value="direct">Built-in harness → model server</option><option value="dsh">DeepSeek Harness → model server</option><option value="codex">Codex CLI → its configured provider</option><option value="opencode">OpenCode → its configured provider</option><option value="fixture">Demo only — no model or inference</option></select>
<div id="direct"><label for="endpoint">Server address</label><input id="endpoint" value="http://127.0.0.1:8000/v1" spellcheck="false"><small>vLLM commonly uses port 8000; llama.cpp commonly uses 8080. Use your actual address. API keys, if needed, come from SVG_BENCH_API_KEY in the launcher's environment.</small><button id="discover">Find loaded models</button><label for="models">Loaded model</label><select id="models"><option value="">Find models, or auto-select if only one is loaded</option></select></div>
<p>Performance telemetry is recorded automatically where available: per-request streaming latency, token counts, and supported runtime prefill/decode metrics. The report separates measured values from unavailable fields. Use an otherwise idle model server for attributable engine timing.</p>
<div id="external" hidden><label for="external-model">Harness model ID (optional)</label><input id="external-model" placeholder="Leave blank to use the harness default"><small>Install and configure this harness normally first, pointing it at the model/server you want to test. The benchmark starts a fresh conversation and handles the four sections. This does not automatically translate a chat-only server into a different harness API.</small></div>
<label for="profile">Saved profile (optional)</label><select id="profile"><option value="">Standard harness settings</option><option value="baseline.json">Baseline — built-in text tools</option><option value="dsh-gestalt-standard.json">Gestalt standard — DeepSeek xhigh</option><option value="example-careful.json">Example experiment — validation instructions</option></select><label for="profile-path">Or load your profile JSON</label><input id="profile-path" placeholder="Local path to a profile JSON file"><small>Profiles freeze instructions and requested parameters. Saved profile settings override matching fields below, including the output-token ceiling. To use the token field directly, select Standard harness settings and clear the profile JSON path. Server templates/plugins remain externally configured unless the connector can confirm them; unverified requirements are reported, not silently applied.</small><details><summary>Optional settings</summary><label for="seconds">Seconds per section</label><input id="seconds" type="number" min="1" value="900"><label for="tokens">Output-token ceiling per model request (built-in / DeepSeek)</label><input id="tokens" type="number" min="1" value="8192"></details>
<p id="profile"><small>Built-in profile: restricted file tools and structural checks, currently text-only. No shell, helper models or web tools. External harnesses have different capabilities and their own security configuration; do not pool them as identical model-only tests.</small></p>
<button class="primary" id="run">Run benchmark</button> <button id="stop" disabled>Stop and keep results</button><p id="message" role="status"></p></section>
<section><h2>What happens</h2><p>The benchmark creates a fresh workspace, runs the original task, two enhancements, then a square-layout change in the same conversation. It saves every section and opens an offline report.</p><p>Completion and machine checks are automatic. Artistic quality and physical relationships need a visual review; a clean XML file is not a passing picture.</p><pre id="progress">Ready. No model request is made until you press Run.</pre><a id="report" hidden target="_self">Open report</a></section>
<script>const token='TOKEN';let busy=false;const el=id=>document.getElementById(id);async function api(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Bench-Token':token},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||'Request failed');return v}el('harness').onchange=()=>{el('direct').hidden=!['direct','dsh'].includes(el('harness').value);if(el('harness').value==='dsh')el('profile').value='dsh-gestalt-standard.json';else if(el('profile').value==='dsh-gestalt-standard.json')el('profile').value='';el('external').hidden=!['codex','opencode'].includes(el('harness').value)};el('discover').onclick=async()=>{try{el('message').textContent='Checking server metadata (no inference)…';const v=await api('/models',{endpoint:el('endpoint').value});el('models').replaceChildren(...v.models.map(m=>new Option(m,m)));el('message').textContent=v.models.length+' model(s) found. Tool-call compatibility is checked during execution, not proven by discovery.'}catch(e){el('message').textContent=e.message}};el('run').onclick=async()=>{try{const v=await api('/start',{harness:el('harness').value,endpoint:el('endpoint').value,model:['direct','dsh'].includes(el('harness').value)?el('models').value:el('external-model').value,seconds:Number(el('seconds').value),max_tokens:Number(el('tokens').value),profile:el('profile-path').value||el('profile').value});el('message').textContent='Started. Keep this launcher running.';el('report').hidden=true;busy=true;poll()}catch(e){el('message').textContent=e.message}};el('stop').onclick=async()=>{await api('/stop',{});el('message').textContent='Stopping; captured results will be retained.'};async function poll(){try{const v=await api('/status',{});busy=v.busy;el('run').disabled=busy;el('stop').disabled=!busy;el('progress').textContent=v.messages.join('\n');if(v.report){el('report').href=v.report;el('report').hidden=false}if(v.error)el('message').textContent=v.error}catch(e){el('message').textContent=e.message}if(busy)setTimeout(poll,1000)}poll();</script></body></html>'''


class Launcher:
    def __init__(self,runs_dir):
        self.runs_dir=Path(runs_dir).resolve();self.lock=threading.Lock();self.busy=False
        self.messages=[];self.report=None;self.error=None;self.cancel=threading.Event();self.thread=None
    def start(self,data):
        with self.lock:
            if self.busy:raise ValueError('A run is already active; no concurrent sweep is supported')
            seconds=float(data.get('seconds',900));tokens=int(data.get('max_tokens',8192))
            if not 0<seconds<=86400 or not 0<tokens<=1000000:raise ValueError('Invalid positive budget')
            harness=data.get('harness','direct');mode='adapter';model=data.get('model') or None
            if harness=='fixture':
                config=json.loads((KIT/'examples/fixture.json').read_text());mode='fixture'
            elif harness in ('direct','dsh'):
                endpoint=str(data.get('endpoint','')).strip()
                if not endpoint.startswith(('http://','https://')):raise ValueError('Enter an HTTP(S) server address ending in /v1')
                config={'command':['{python}','{kit}/qwen_bench/'+('dsh_adapter.py' if harness=='dsh' else 'endpoint_adapter.py')],
                        'deployment':{'endpoint':endpoint,'model':model,'harness':'dsh' if harness=='dsh' else 'builtin-text-tools-v1'},
                        'options':{'endpoint':endpoint,'model':model,'max_tokens':tokens}}
            elif harness in ('codex','opencode'):
                config={'command':['{python}','{kit}/qwen_bench/harness_adapter.py'],
                        'deployment':{'harness':harness,'model':model,'server':'existing harness configuration'},
                        'options':{'harness':harness,'model':model}}
            else:raise ValueError('Unknown harness')
            profile_path=data.get('profile')
            if profile_path:
                if harness=='fixture':raise ValueError('Demo does not apply model profiles')
                from .profiles import load_profile,apply_profile
                selected=Path(profile_path).expanduser()
                if not selected.is_file():selected=KIT/'profiles'/profile_path
                config=apply_profile(config,load_profile(selected),harness)
            safe_config(config)
            self.busy=True;self.messages=[];self.report=None;self.error=None;self.cancel=threading.Event()
            if harness in ('direct','dsh'):
                source='saved profile' if config.get('profile',{}).get('definition',{}).get('settings',{}).get('max_tokens') is not None else 'token field'
                self.messages.append(f"Effective output-token ceiling per model request: {config['options']['max_tokens']} (from {source}; requested limit, not server-attested).")
        def log(message):
            with self.lock:self.messages.append(str(message));self.messages=self.messages[-40:]
        def work():
            try:
                run,status=execute(runs_dir=self.runs_dir,mode=mode,config=config,turn_seconds=seconds,print_fn=log,cancel_event=self.cancel)
                with self.lock:
                    self.report=run/'report.html';self.error=status.get('error')
            except Exception as error:
                with self.lock:self.error=f'{type(error).__name__}: {error}'
            finally:
                with self.lock:self.busy=False
        self.thread=threading.Thread(target=work,daemon=False);self.thread.start()


def make_server(runs_dir,port=0):
    launcher=Launcher(runs_dir);token=secrets.token_urlsafe(32)
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def send(self,code,data,content_type='application/json'):
            encoded=data if isinstance(data,bytes) else json.dumps(data).encode()
            self.send_response(code);self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        def valid_host(self):return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
        def do_GET(self):
            if not self.valid_host():self.send(403,{'error':'Invalid local host'});return
            if self.path=='/':self.send(200,PAGE.replace('TOKEN',token).encode(),'text/html; charset=utf-8');return
            path=unquote(urlparse(self.path).path)
            if path.startswith('/report/'):
                with launcher.lock:report=launcher.report
                if report:
                    relative=path[len('/report/'):]
                    target=(report.parent/relative).resolve()
                    allowed={'report.html','status.json','manifest.json','visual-review.json','telemetry.json','telemetry.csv'}
                    import re
                    if relative in allowed or re.fullmatch(r'checkpoints/C[0-3]/(scene\.svg|result\.json|telemetry\.json)',relative):
                        if target.is_relative_to(report.parent) and target.is_file():
                            # SVG source is download-only, never an active HTML navigation.
                            mime='text/html; charset=utf-8' if target.name=='report.html' else 'application/json' if target.suffix=='.json' else 'application/octet-stream'
                            self.send(200,target.read_bytes(),mime);return
            self.send(404,{'error':'Not found'})
        def do_POST(self):
            if not self.valid_host() or not hmac.compare_digest(self.headers.get('X-Bench-Token',''),token):
                self.send(403,{'error':'Invalid launcher token'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if size<0 or size>65536:raise ValueError('Request too large')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('Expected object')
                if self.path=='/models':
                    from .endpoint import list_models
                    safe_config(data)
                    self.send(200,{'models':list_models(data.get('endpoint',''))})
                elif self.path=='/start':launcher.start(data);self.send(200,{'started':True})
                elif self.path=='/stop':launcher.cancel.set();self.send(200,{'stopping':True})
                elif self.path=='/status':
                    with launcher.lock:
                        value={'busy':launcher.busy,'messages':list(launcher.messages),'error':launcher.error,'report':'/report/report.html' if launcher.report else None}
                    self.send(200,value)
                else:self.send(404,{'error':'Not found'})
            except Exception as error:self.send(400,{'error':str(error)})
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    return server,launcher,token


def serve(runs_dir,open_browser=True):
    server,launcher,_=make_server(runs_dir)
    url=f'http://127.0.0.1:{server.server_port}/';print('Open '+url+' (local only; Ctrl-C closes launcher)',flush=True)
    if open_browser:webbrowser.open(url)
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:
        launcher.cancel.set()
        if launcher.thread:
            launcher.thread.join(timeout=20)
            if launcher.thread.is_alive():print('Waiting for the recorder to finish cleanup; captured results are being retained.',flush=True)
        server.server_close()
