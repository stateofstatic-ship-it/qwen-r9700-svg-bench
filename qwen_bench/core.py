"""Portable recording and execution. The adapter owns candidate containment."""
import base64
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import time
import uuid
from .adapter import CommandAdapter, AdapterError
from .checks import inspect, MAX_SVG_BYTES
from .report import write_report

KIT = Path(__file__).resolve().parent.parent
EXPECTED = ['C0','C1','C2','C3']
PROMPT_FILES = ['INITIAL_PROMPT.md','C1.txt','C2.txt','CHANGE_REQUEST.md']


def save(path, value):
    # Only runner-owned metadata is replaced; prior run directories are never reused.
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def safe_config(value):
    forbidden = {'api_key','apikey','password','secret','access_token','authorization','auth_token'}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in forbidden: raise ValueError('Credentials must use the adapter environment, not config: '+key)
            safe_config(child)
    elif isinstance(value, list):
        for child in value: safe_config(child)
    elif isinstance(value, str):
        from urllib.parse import urlsplit
        if '://' in value and urlsplit(value).username is not None:
            raise ValueError('Credential-bearing URLs must not be recorded')
        if value.lower().startswith(('--api-key','--password','authorization:')):
            raise ValueError('Credential-bearing command arguments must not be recorded')


def aggregate(status, checkpoint_names):
    sections = status['sections']
    complete = (checkpoint_names == EXPECTED and [r['checkpoint'] for r in sections] == EXPECTED
                and all(r['execution'] == 'completed' for r in sections)
                and status['execution'] == 'completed')
    status['trajectory'] = 'completed' if complete else 'incomplete'
    status['machine_verification'] = ('verified' if complete and all(r.get('checks',{}).get('machine_gates_pass') for r in sections) else 'partial')


def capture(workspace, checkpoint, dimensions):
    source = workspace/'output/scene.svg'
    if source.parent.is_symlink() or source.is_symlink() or not source.is_file():
        return {'status':'failed','reason':'Missing/nonregular/symlink output/scene.svg','visual_quality':'not_observed'}
    if not source.resolve().is_relative_to(workspace.resolve()):
        return {'status':'failed','reason':'Artifact escaped workspace','visual_quality':'not_observed'}
    if source.stat().st_size > MAX_SVG_BYTES:
        return {'status':'failed','reason':'SVG exceeds 8 MiB safety limit','visual_quality':'not_observed'}
    before = source.stat()
    data = source.read_bytes()
    after = source.stat()
    if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
        return {'status':'failed','reason':'Artifact changed during checkpoint capture','visual_quality':'not_observed'}
    target = checkpoint/'scene.svg'
    with target.open('xb') as out: out.write(data)
    return inspect(target,*dimensions)


def close_adapter(adapter, status):
    # Cleanup must not replace the original failure or prevent its saved report.
    try: adapter.close()
    except Exception as error:
        adapter.cleanup_warnings.append(f'Cleanup raised {type(error).__name__}: {error}')
    status['process_exit'] = adapter.process.returncode
    status['cleanup_warnings'] = adapter.cleanup_warnings
    if adapter.cleanup_warnings and status['execution'] == 'completed':
        status.update(execution='failed', error='Adapter cleanup incomplete')


def execute(*, runs_dir, mode='manual', config=None, protocol='v0.3-portable',
            turn_seconds=900, input_fn=input, print_fn=print, label='unspecified', cancel_event=None):
    if not isinstance(turn_seconds,(int,float)) or not math.isfinite(turn_seconds) or turn_seconds <= 0:
        raise ValueError('Turn seconds must be finite and positive')
    if mode not in ('manual','adapter','fixture'): raise ValueError('Unknown execution mode')
    if mode == 'manual' and protocol != 'v0.3-portable':
        raise ValueError('Easy/manual mode uses the portable relative-path edition; v0.2 requires an adapter mapping /work')
    config = config or {}
    safe_config(config)
    if mode != 'manual' and (not isinstance(config.get('command'),list) or not config['command'] or
                             not all(isinstance(arg,str) for arg in config['command'])):
        raise ValueError('Adapter command must be a nonempty JSON argv array')
    protocol_dir = KIT/'protocol'/protocol
    index = json.loads((protocol_dir/'manifest.json').read_text())
    for name, expected in index['files'].items():
        if sha(protocol_dir/name) != expected: raise ValueError('Protocol hash mismatch: '+name)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    runs_dir = Path(runs_dir).expanduser().resolve(); runs_dir.mkdir(parents=True,exist_ok=True)
    run = runs_dir/(stamp+'-'+uuid.uuid4().hex[:8]); run.mkdir(mode=0o700)
    for name in ('workspace','private-adapter-state','raw','checkpoints','frozen'):
        (run/name).mkdir(mode=0o700)
    workspace = run/'workspace'; (workspace/'output').mkdir()
    shutil.copytree(protocol_dir,run/'frozen/protocol')
    if config.get('profile'):
        loaded=config['profile']
        save(run/'frozen/profile.json',loaded)
        save(run/'profile-receipt.json',config['profile_receipt'])
        if loaded.get('agents_md_text') is not None:
            (workspace/'AGENTS.md').write_bytes(loaded['agents_md_text'].encode('utf-8'))
    source_hashes = {str(p.relative_to(KIT)):sha(p) for p in [KIT/'benchmark.py',*sorted((KIT/'qwen_bench').glob('*.py'))]}
    for name in source_hashes:
        target=run/'frozen/kit'/name; target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(KIT/name,target)
    command = [arg.replace('{python}',sys.executable).replace('{kit}',str(KIT)) for arg in config.get('command',[])]
    adapter_files = {str(Path(arg).resolve()):sha(Path(arg)) for arg in command if Path(arg).is_file()}
    prompts = [(run/'frozen/protocol'/name).read_bytes() for name in PROMPT_FILES]
    for prompt in prompts: prompt.decode('utf-8')
    manifest = {'format':'svg-bench/1','protocol':protocol,'mode':mode,'deployment':config.get('deployment',label),
                'config':config,'source_hashes':source_hashes,'adapter_file_hashes':adapter_files,
                'protocol_hashes':index['files'],'prompt_sha256':[hashlib.sha256(p).hexdigest() for p in prompts],
                'software':{'python':sys.version,'platform':platform.platform()},'turn_seconds':turn_seconds,
                'sampling_runtime_hardware':'Record exact values in deployment metadata; unspecified is unknown.',
                'privacy':'No environment dump; adapter raw logs/private state can still contain sensitive data.'}
    manifest['profile_receipt']=config.get('profile_receipt')
    save(run/'manifest.json',manifest)
    status = {'execution':'running','trajectory':'incomplete','machine_verification':'not_run','sections':[],
              'controls':'not_verified (manual)' if mode=='manual' else 'adapter_reported; not independently certified',
              'timing_scope':'Human-mediated section wall time; not inference latency.' if mode=='manual' else 'Runner dispatch-to-terminal section wall time; not engine latency.',
              'usage':'unavailable unless explicitly reported; raw counters retain their adapter-defined scope',
              'process_exit':None,'error':None}
    save(run/'status.json',status)
    adapter=None; current=None; phase='preflight'; sequence=0
    events=(run/'events.jsonl').open('x',encoding='utf-8')

    def record(origin,event):
        nonlocal sequence
        sequence += 1
        row={'seq':sequence,'utc':dt.datetime.now(dt.timezone.utc).isoformat(),'monotonic_ns':time.monotonic_ns(),
             'phase':phase,'origin':origin,'event':event}
        line=json.dumps(row,ensure_ascii=False)+'\n';events.write(line);events.flush()
        if phase in EXPECTED:
            with (run/'checkpoints'/phase/'events.jsonl').open('a',encoding='utf-8') as section: section.write(line)

    session=None
    try:
        print_fn('Run folder: '+str(run))
        if mode == 'manual':
            print_fn('Open a NEW conversation in the agent you already use, with this workspace:\n'+str(workspace))
            print_fn('Keep that same conversation for all four prompts. Save each SVG to output/scene.svg in this workspace. No helpers, image generation, internet or external assets.')
            input_fn('Press Enter when the fresh workspace/conversation is ready (Ctrl-C stops and retains this attempt): ')
        else:
            adapter=CommandAdapter(command,run,record,cancel_event=cancel_event)
            adapter.send({'type':'preflight','checkpoint':None,'protocol':'svg-bench/1','benchmark_protocol':protocol,
                          'workspace':str(workspace),'state_dir':str(run/'private-adapter-state'),
                          'candidate_workspace':'/work' if protocol=='v0.2' else '.',
                          'options':config.get('options',{}),'no_inference':True})
            preflight=adapter.receive('preflight.completed',None,30)
            save(run/'preflight.json',preflight)
            manifest['adapter_preflight']=preflight
            save(run/'manifest.json',manifest)
            if preflight.get('status') != 'passed' or preflight.get('same_session_supported') is not True:
                raise AdapterError('Adapter compatibility preflight did not pass')
            if protocol=='v0.2' and preflight.get('candidate_workspace') != '/work':
                raise AdapterError('v0.2 requires candidate-visible /work mapping')
            status['controls']=preflight.get('controls','not_observed (adapter omitted controls)')
            if mode=='fixture' and preflight.get('fixture') is not True: raise AdapterError('Fixture mode requires synthetic adapter')
        for turn,prompt in enumerate(prompts):
            if cancel_event is not None and cancel_event.is_set(): raise InterruptedError("Run stopped before next section")
            phase=EXPECTED[turn]; checkpoint=run/'checkpoints'/phase;checkpoint.mkdir()
            (checkpoint/'prompt.txt').write_bytes(prompt)
            dimensions=(1024,1024) if turn==3 else (1440,960)
            current={'checkpoint':phase,'execution':'running','elapsed_seconds':0,'session_id':session,
                     'usage':None,'prompt_sha256':hashlib.sha256(prompt).hexdigest()}
            start=time.monotonic()
            if mode!='manual': print_fn(phase+' started — waiting for the same-session adapter.')
            record('runner',{'type':'section.started','checkpoint':phase,'prompt_sha256':current['prompt_sha256']})
            if mode=='manual':
                print_fn('\n'+phase+' — paste this exact prompt into the SAME conversation:\n\n'+prompt.decode('utf-8'))
                print_fn('\nPrompt file: '+str(checkpoint/'prompt.txt'))
                response=input_fn('After the agent finishes and output/scene.svg is saved, press Enter (or type stop): ')
                if response.strip().lower()=='stop': raise KeyboardInterrupt('User stopped the manual attempt')
                current.update(execution='completed',prompt_delivery='user_confirmed; byte-exact delivery not independently observed',
                               terminal_source='user_confirmation',session_id='manual-unverified')
            else:
                adapter.set_phase(phase)
                adapter.send({'type':'turn','checkpoint':phase,'prompt_base64':base64.b64encode(prompt).decode('ascii'),
                              'prompt_sha256':current['prompt_sha256'],'session_id':session,'timeout_seconds':turn_seconds})
                terminal=adapter.receive('turn.completed',phase,turn_seconds)
                next_session=terminal.get('session_id')
                if not isinstance(next_session,str) or not next_session or (session is not None and next_session!=session):
                    raise AdapterError('Missing or changed conversation identifier')
                if terminal.get('status')!='completed': raise AdapterError('Terminal status is not completed')
                session=next_session
                current.update(execution='completed',session_id=session,usage=terminal.get('usage'),
                               terminal_source='adapter_reported',terminal_event=terminal)
            current['elapsed_seconds']=time.monotonic()-start
            current['checks']=capture(workspace,checkpoint,dimensions)
            save(checkpoint/'result.json',current);status['sections'].append(current)
            record('runner',{'type':'section.finished','checkpoint':phase,'execution':current['execution'],'checks':current['checks']})
            save(run/'status.json',status)
            print_fn(phase+': '+current['checks']['status']+' machine checks; checkpoint saved.')
            current=None
        phase='shutdown'
        if adapter is not None:
            adapter.shutdown();status['process_exit']=adapter.process.returncode
        status['execution']='completed'
    except BaseException as error:
        status['execution']='interrupted' if isinstance(error,(KeyboardInterrupt,InterruptedError)) else 'failed'
        status['error']=f'{type(error).__name__}: {error}'
        record('runner',{'type':'run.stopped','error':status['error']})
        if adapter is not None:
            close_adapter(adapter,status);adapter=None
        if current is not None:
            current.update(execution=status['execution'],elapsed_seconds=time.monotonic()-start,error=status['error'])
            current['checks']=capture(workspace,run/'checkpoints'/phase,dimensions)
            save(run/'checkpoints'/phase/'result.json',current);status['sections'].append(current)
    finally:
        if adapter is not None:
            close_adapter(adapter,status)
        events.close()
        aggregate(status,sorted(p.name for p in (run/'checkpoints').iterdir()))
        save(run/'status.json',status)
        report=write_report(run)
        print_fn('Report: '+str(report))
        print_fn('Execution: '+status['execution']+'; four-section trajectory: '+status['trajectory']+'; machine verification: '+status['machine_verification'])
    return run,status
