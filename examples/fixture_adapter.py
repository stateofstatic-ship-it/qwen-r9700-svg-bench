#!/usr/bin/env python3
"""Synthetic protocol fixture. Not a model and not an artistic benchmark result."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import time

workspace=None; session='synthetic-fixture-session'; turns=0; mode='success'

def emit(kind, checkpoint=None, **data):
    print(json.dumps({'type':kind,'checkpoint':checkpoint,**data}),flush=True)

for line in sys.stdin:
    request=json.loads(line);kind=request['type'];checkpoint=request.get('checkpoint')
    if kind=='preflight':
        workspace=Path(request['workspace']);mode=request.get('options',{}).get('fixture_mode','success')
        emit('preflight.completed',status='passed',same_session_supported=True,fixture=True,
             candidate_workspace=request['candidate_workspace'],controls='synthetic fixture; no model and no security enforcement claims')
    elif kind=='shutdown':
        emit('run.closed');break
    elif kind=='turn':
        prompt=base64.b64decode(request['prompt_base64'],validate=True)
        assert hashlib.sha256(prompt).hexdigest()==request['prompt_sha256']
        assert request['session_id'] in (None,session)
        if mode=='timeout': time.sleep(60)
        if mode=='malformed': print('not json',flush=True);continue
        if mode=='wrong_checkpoint': emit('turn.started',checkpoint='C9');continue
        if mode=='nonzero': sys.exit(7)
        if mode=='error': emit('turn.failed',checkpoint,message='synthetic error');continue
        if mode=='changed_session' and turns: session='changed'
        emit('turn.started',checkpoint,session_id=session)
        w,h=(1024,1024) if checkpoint=='C3' else (1440,960)
        if mode!='missing':
            (workspace/'output/scene.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" fill="#eee8ff"/><text x="80" y="120" font-size="40" fill="#382760">SYNTHETIC FIXTURE — {checkpoint}</text><rect x="80" y="170" width="300" height="160" rx="20" fill="#8151b8"/></svg>',encoding='utf-8')
        emit('adapter.event',checkpoint,observation='deterministic fixture artifact; no inference')
        emit('turn.completed',checkpoint,status='completed',session_id=session,usage=None)
        turns+=1
