"""Persistent text-only diagnostic agent, separate from the native primary track."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sys
import time
import uuid
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_bench.endpoint import Endpoint, EndpointError
from qwen_bench.checks import inspect

MAX_FILE = 256 * 1024
MAX_LINE = 1024 * 1024

def schema(name, description, properties, required):
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}

TOOLS = [
    schema('write_file','Write UTF-8 text to a workspace-relative file.',{'path':{'type':'string'},'content':{'type':'string'}},['path','content']),
    schema('read_file','Read a UTF-8 workspace-relative file.',{'path':{'type':'string'}},['path']),
    schema('list_files','List immediate workspace directory entries.',{'path':{'type':'string'}},['path']),
    schema('check_svg','Static SVG checks only; does not render or assess visual quality.',{'path':{'type':'string'},'width':{'type':'integer'},'height':{'type':'integer'}},['path','width','height']),
]

class Adapter:
    def __init__(self, emit):
        self.emit = emit
        self.messages = []
        self.session = None
        self.ready = False
        self.turns = 0
        self.serial = 0

    def save(self, name, value):
        data = json.dumps(value, ensure_ascii=False, indent=2)
        # Defense against accidental provider reflection of the authorization secret.
        if self.endpoint.key:
            data = data.replace(self.endpoint.key, '[REDACTED]')
        fd = os.open(self.state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data + '\n')

    def event(self, kind, **fields):
        self.emit(dict(type=kind, checkpoint=self.checkpoint, **fields))

    def preflight(self, req):
        if self.ready:
            raise EndpointError('Repeated preflight')
        self.checkpoint = None
        options = dict(req.get('options', {}))
        allowed = {'endpoint','model','api_key_env','max_tokens','temperature','top_p','seed','max_requests','max_tool_steps','reasoning_effort','request_options'}
        if set(options) - allowed:
            raise EndpointError('Unknown endpoint adapter option')
        self.workspace = Path(req['workspace']).resolve(strict=True)
        self.state = Path(req['state_dir']).resolve(strict=True)
        if not self.workspace.is_dir() or not self.state.is_dir() or self.state == self.workspace or self.workspace in self.state.parents:
            raise EndpointError('Private state must be outside candidate workspace')
        os.chmod(self.state, 0o700)
        self.endpoint = Endpoint(options['endpoint'], options.get('api_key_env','SVG_BENCH_API_KEY'))
        self.model, metadata = self.endpoint.select_model(options.get('model'))
        self.config = dict(endpoint=self.endpoint.url, model=self.model,
            api_key_env=options.get('api_key_env','SVG_BENCH_API_KEY'), max_tokens=options.get('max_tokens',8192),
            temperature=options.get('temperature',0), top_p=options.get('top_p',1),
            max_requests=options.get('max_requests',16), max_tool_steps=options.get('max_tool_steps',48))
        if 'reasoning_effort' in options:
            if not isinstance(options['reasoning_effort'],str): raise EndpointError('reasoning_effort must be a string')
            self.config['reasoning_effort']=options['reasoning_effort']
        extra=options.get('request_options',{})
        if not isinstance(extra,dict) or set(extra)&{'messages','tools','tool_choice','stream','model','n','max_tokens','temperature','top_p','seed','reasoning_effort'}:
            raise EndpointError('request_options cannot override the fixed conversation/tool contract or named settings')
        self.config['request_options']=extra
        if 'seed' in options:
            self.config['seed'] = options['seed']
            if type(options['seed']) is not int: raise EndpointError('seed must be an integer')
        for key, maximum in [('max_tokens',131072),('max_requests',64),('max_tool_steps',256)]:
            if type(self.config[key]) is not int or not 1 <= self.config[key] <= maximum:
                raise EndpointError('Invalid '+key)
        for key, maximum in [('temperature',2),('top_p',1)]:
            value = self.config[key]
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise EndpointError('Invalid '+key)
        instructions=self.workspace/'AGENTS.md'
        if instructions.exists():
            if instructions.is_symlink() or not instructions.is_file() or instructions.stat().st_size>65536: raise EndpointError('Invalid profile AGENTS.md')
            self.messages.append({'role':'system','content':instructions.read_text(encoding='utf-8')})
        self.save('endpoint-config.json', dict(self.config,track='text-only-diagnostic',tools=TOOLS,
            benchmark_protocol=req.get('benchmark_protocol'),system_prompt=self.messages[0] if self.messages else None,
            runtime_settings_status='Sent in request payload; effective runtime settings not independently attested'))
        self.save('endpoint-models.json',metadata)
        self.ready = True
        self.event('preflight.completed',status='passed',same_session_supported=True,
            model=self.model,endpoint=self.endpoint.url,request_configuration=self.config,
            controls='Text-only diagnostic track, not native primary track; metadata reachability/schema verified only; inference/tool-calling compatibility not yet proven. Exact user prompts, persistent actual message history; no shell, vision or helper agents. Profile AGENTS.md, when present, is a frozen system message. max_tokens='+str(self.config['max_tokens']))

    def path(self, name):
        if not isinstance(name,str) or not name or '\\' in name or '\x00' in name:
            raise EndpointError('Invalid workspace-relative path')
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or ':' in name:
            raise EndpointError('Absolute/traversal paths prohibited')
        path = self.workspace
        for part in relative.parts:
            path = path / part
            if path.is_symlink(): raise EndpointError('Symlinks prohibited')
        if not path.resolve().is_relative_to(self.workspace):
            raise EndpointError('Path escapes workspace')
        return path

    def tool(self, name, args):
        spec = next((x['function']['parameters'] for x in TOOLS if x['function']['name']==name),None)
        if spec is None or not isinstance(args,dict) or set(args)!=set(spec['required']):
            raise EndpointError('Invalid tool or arguments')
        path = self.path(args['path'])
        if name=='write_file':
            if path.relative_to(self.workspace).as_posix().casefold().rstrip(' .')=='agents.md':
                raise EndpointError('Profile AGENTS.md is read-only')
            content=args['content']
            if not isinstance(content,str) or len(content.encode('utf-8'))>MAX_FILE: raise EndpointError('Invalid/oversize file content')
            if path.exists() and not path.is_file(): raise EndpointError('Nonregular target')
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content,encoding='utf-8')
            return {'written_bytes':len(content.encode('utf-8'))}
        if name=='list_files':
            if not path.is_dir(): raise EndpointError('Not a directory')
            entries=[]
            for item in path.iterdir():
                if len(entries)>=1000: raise EndpointError('Directory exceeds 1000 entries')
                entries.append({'name':item.name,'kind':'symlink' if item.is_symlink() else 'directory' if item.is_dir() else 'file'})
            return sorted(entries,key=lambda x:x['name'])
        if not path.is_file() or path.stat().st_size>MAX_FILE: raise EndpointError('Missing/nonregular/oversize file')
        if name=='read_file': return {'content':path.read_text(encoding='utf-8')}
        if any(type(args[k]) is not int or not 1<=args[k]<=100000 for k in ('width','height')): raise EndpointError('Invalid dimensions')
        return inspect(path,args['width'],args['height'])

    def turn(self, req):
        if not self.ready or self.turns>=4: raise EndpointError('Preflight required; at most four turns')
        self.checkpoint=req['checkpoint']
        if req.get('session_id') != self.session: raise EndpointError('Session mismatch')
        prompt=base64.b64decode(req['prompt_base64'],validate=True)
        if hashlib.sha256(prompt).hexdigest()!=req['prompt_sha256']: raise EndpointError('Prompt hash mismatch')
        timeout=req['timeout_seconds']
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0: raise EndpointError('Invalid turn timeout')
        deadline=time.monotonic()+timeout
        self.session=self.session or 'endpoint-'+uuid.uuid4().hex
        self.turns+=1
        self.messages.append({'role':'user','content':prompt.decode('utf-8')})
        self.event('turn.started',session_id=self.session)
        usage=[]
        steps=0
        for index in range(self.config['max_requests']):
            remaining=deadline-time.monotonic()
            if remaining<=0: raise EndpointError('Turn deadline exceeded')
            payload={k:self.config[k] for k in ('model','max_tokens','temperature','top_p','seed','reasoning_effort') if k in self.config}
            payload.update(self.config.get('request_options',{}))
            payload.update(messages=self.messages,tools=TOOLS,tool_choice='auto',stream=False)
            self.serial+=1
            prefix=f'request-{self.serial:04d}'
            if len(json.dumps(payload,ensure_ascii=False).encode('utf-8')) > 8 * 1024 * 1024:
                raise EndpointError('Endpoint request/history exceeds 8 MiB')
            self.save(prefix+'.json',payload)
            response=self.endpoint.call('/chat/completions',payload,timeout=remaining)
            self.save(prefix+'-response.json',response)
            if time.monotonic() >= deadline: raise EndpointError('Turn deadline exceeded')
            raw_usage=response.get('usage')
            usage.append({'request':self.serial,'scope':'provider response for this request (includes replayed history)','raw':raw_usage})
            self.event('usage',**usage[-1])
            choices=response.get('choices')
            if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict): raise EndpointError('Expected exactly one response choice')
            choice=choices[0]
            if choice.get('finish_reason') in ('length','content_filter'): raise EndpointError('Incomplete generation: '+choice['finish_reason'])
            message=choice.get('message')
            if not isinstance(message,dict) or message.get('role')!='assistant' or not isinstance(message.get('content'),(str,type(None))): raise EndpointError('Invalid assistant message')
            calls=message.get('tool_calls') or []
            if not isinstance(calls,list): raise EndpointError('Invalid tool_calls')
            canonical={'role':'assistant','content':message.get('content')}
            if calls:
                if choice.get('finish_reason') not in ('tool_calls','stop'): raise EndpointError('Unexpected tool finish reason')
                if steps+len(calls)>self.config['max_tool_steps']: raise EndpointError('Tool step limit exceeded')
                normalized=[];argument_errors={}
                seen=set()
                for call in calls:
                    if not isinstance(call,dict) or call.get('type')!='function' or not isinstance(call.get('id'),str) or not call['id'] or call['id'] in seen: raise EndpointError('Invalid tool call identity')
                    seen.add(call['id'])
                    function=call.get('function')
                    if not isinstance(function,dict): raise EndpointError('Invalid tool function')
                    args=function.get('arguments')
                    original=args
                    if isinstance(args,str):
                        try: args=json.loads(args)
                        except json.JSONDecodeError: argument_errors[call['id']]='Tool arguments are not valid JSON'
                    if not isinstance(args,dict): argument_errors.setdefault(call['id'],'Tool arguments must be an object')
                    encoded=original if isinstance(original,str) else json.dumps(original,ensure_ascii=False)
                    normalized.append(dict(id=call['id'],type='function',function={'name':function.get('name'),'arguments':encoded}))
                canonical['tool_calls']=normalized
            self.messages.append(canonical)
            if canonical['content']: self.event('assistant.message',content=canonical['content'],session_id=self.session)
            if not calls:
                if choice.get('finish_reason')!='stop': raise EndpointError('Nonterminal/unsupported finish reason')
                self.event('turn.completed',status='completed',session_id=self.session,usage={'scope':'per-request provider usage; not summed','requests':usage})
                return
            for call in normalized:
                if time.monotonic()>=deadline: raise EndpointError('Turn deadline exceeded')
                steps+=1
                name=call['function']['name']
                self.event('tool.started',tool_call_id=call['id'],name=name,arguments=call['function']['arguments'])
                try:
                    if call['id'] in argument_errors: raise EndpointError(argument_errors[call['id']])
                    args=json.loads(call['function']['arguments'])
                    result=self.tool(name,args)
                except (EndpointError,OSError,UnicodeError) as error:
                    result={'error':str(error) if isinstance(error,EndpointError) else type(error).__name__,'status':'failed'}
                self.event('tool.completed',tool_call_id=call['id'],name=name,result=result)
                self.messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)})
        raise EndpointError('Request limit exceeded')


def main():
    def emit(value):
        encoded=json.dumps(value,ensure_ascii=False)
        if adapter.ready and adapter.endpoint.key: encoded=encoded.replace(adapter.endpoint.key,'[REDACTED]')
        if len(encoded.encode('utf-8'))>=MAX_LINE: raise EndpointError('Event exceeds JSONL limit')
        print(encoded,flush=True)
    adapter=Adapter(emit)
    while True:
        line=sys.stdin.buffer.readline(MAX_LINE+1)
        if not line: return 1
        req={}
        try:
            if len(line)>MAX_LINE: raise EndpointError('Request exceeds JSONL limit')
            req=json.loads(line)
            if not isinstance(req,dict): raise EndpointError('Request must be an object')
            adapter.checkpoint=req.get('checkpoint')
            if req.get('type')=='shutdown':
                adapter.checkpoint=None
                adapter.event('run.closed')
                return 0
            if req.get('type')=='preflight': adapter.preflight(req)
            elif req.get('type')=='turn': adapter.turn(req)
            else: raise EndpointError('Unsupported request type')
        except Exception as exc:
            # Never expose arbitrary exception text (e.g. provider values or credentials).
            message=str(exc) if isinstance(exc,EndpointError) else 'Invalid request, response or workspace operation ('+type(exc).__name__+')'
            adapter.checkpoint=req.get('checkpoint') if isinstance(req,dict) else None
            adapter.event('error',message=message)
            return 1

if __name__=='__main__':
    raise SystemExit(main())
