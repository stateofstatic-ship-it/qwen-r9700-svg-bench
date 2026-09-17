"""Versioned profile loading; never confuse requested settings with runtime facts."""
import copy
import hashlib
import json
from pathlib import Path
from .core import safe_config

ALLOWED_SETTINGS={'temperature','top_p','seed','max_tokens','max_requests','max_tool_steps','effort','request_options','stream','stream_usage','runtime_metrics'}


def load_profile(path):
    path=Path(path).expanduser().resolve()
    data=path.read_bytes();profile=json.loads(data)
    if not isinstance(profile,dict) or profile.get('format')!='svg-bench-profile/1':
        raise ValueError('Profile must use format svg-bench-profile/1')
    if set(profile)-{'format','name','harness','protocol','settings','agents_md','agents_md_file','external_requirements','note'}:
        raise ValueError('Unknown profile field; do not silently assume settings apply')
    safe_config(profile)
    settings=profile.get('settings',{})
    if not isinstance(settings,dict) or set(settings)-ALLOWED_SETTINGS:raise ValueError('Unsupported profile setting')
    if profile.get('harness') not in ('direct','codex','opencode'):raise ValueError('Unsupported profile harness')
    if profile.get('protocol','v0.3-portable')!='v0.3-portable':raise ValueError('Portable profiles use their separately versioned relative-path protocol')
    if not isinstance(profile.get('external_requirements',{}),dict):raise ValueError('External requirements must be an object')
    text=profile.get('agents_md')
    if profile.get('agents_md_file'):
        if text is not None:raise ValueError('Use agents_md or agents_md_file, not both')
        text=(path.parent/profile['agents_md_file']).read_text(encoding='utf-8')
    if text is not None and (not isinstance(text,str) or len(text.encode('utf-8'))>65536):raise ValueError('AGENTS.md must be at most 64 KiB of UTF-8 text')
    return {'definition':profile,'source_sha256':hashlib.sha256(data).hexdigest(),'agents_md_text':text,
            'agents_md_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest() if text is not None else None}


def apply_profile(config,loaded,harness):
    profile=loaded['definition']
    if profile['harness']!=harness:raise ValueError('Profile harness does not match selected harness')
    result=copy.deepcopy(config);options=result.setdefault('options',{})
    settings=profile.get('settings',{})
    if harness!='direct' and set(settings)-{'effort'}:
        raise ValueError('This native harness connector currently applies effort only; sampling must be configured in the harness and recorded as an external requirement')
    for key,value in settings.items():
        options['reasoning_effort' if key=='effort' and harness=='direct' else key]=value
    result['profile']=loaded
    result['profile_receipt']={'name':profile.get('name'),'profile_sha256':loaded['source_sha256'],
        'agents_md_sha256':loaded['agents_md_sha256'],'requested_settings':settings,
        'settings_delivery':'Direct: frozen request payload; native: explicit CLI arguments. Runtime application requires runtime evidence; not assumed.',
        'runtime_effective_settings':'not_independently_attested',
        'external_requirements':{key:{'requested':value,'status':'unverified_external_requirement'} for key,value in profile.get('external_requirements',{}).items()},
        'comparability':'Separate profile/system condition; not a model-only comparison or validated real-world improvement'}
    return result
