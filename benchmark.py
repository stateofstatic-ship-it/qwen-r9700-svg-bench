#!/usr/bin/env python3
"""Qwen × R9700 SVG benchmark launcher; Python 3.11+, no pip install."""
import argparse
import json
from pathlib import Path
import sys


def main():
    if sys.version_info < (3,11):
        print("Python 3.11 or newer is required. No pip packages are needed.",file=sys.stderr);return 2
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--demo',action='store_true',help='Run the synthetic no-inference demo')
    mode.add_argument('--endpoint',help='OpenAI-compatible /v1 endpoint; supplies the built-in tool harness')
    mode.add_argument('--adapter',type=Path,help='External harness adapter JSON config')
    mode.add_argument('--harness',choices=['codex','opencode'],help='Use an installed configured native harness')
    mode.add_argument('--manual',action='store_true',help='Fallback: copy/paste prompts into any agent')
    mode.add_argument('--ui',action='store_true',help='Open the local browser launcher (default)')
    parser.add_argument('--model',help='Served model identifier; inferred only when exactly one is available')
    parser.add_argument('--label',default='unspecified',help='User-facing configuration label for manual mode')
    parser.add_argument('--profile',type=Path,help='Load a named profile JSON file')
    parser.add_argument('--runs-dir',type=Path,default=Path('runs'))
    parser.add_argument('--turn-seconds',type=float,default=900)
    parser.add_argument('--max-tokens',type=int,default=8192,help='Per-request output token ceiling in the built-in harness')
    parser.add_argument('--protocol',choices=['v0.3-portable','v0.2'],default='v0.3-portable')
    parser.add_argument('--no-open',action='store_true',help='Do not open the local browser')
    args=parser.parse_args()
    if args.turn_seconds<=0 or args.max_tokens<=0: parser.error('Budgets must be positive')
    from qwen_bench.core import KIT,execute
    if args.ui or not any((args.demo,args.endpoint,args.adapter,args.manual,args.harness)):
        from qwen_bench.ui import serve
        serve(args.runs_dir,open_browser=not args.no_open);return 0
    config={};selected='manual'
    if args.demo:
        config=json.loads((KIT/'examples/fixture.json').read_text());selected='fixture'
    elif args.endpoint:
        if args.protocol=='v0.2': parser.error('Built-in harness uses relative paths: choose v0.3-portable')
        config={'command':['{python}','-m','qwen_bench.endpoint_adapter'],
                'deployment':{'endpoint':args.endpoint,'model':args.model,'harness':'builtin-text-tools-v1'},
                'options':{'endpoint':args.endpoint,'model':args.model,'max_tokens':args.max_tokens}}
        # -m resolution must not depend on the adapter process's private cwd.
        config['command']=['{python}','{kit}/qwen_bench/endpoint_adapter.py']
        selected='adapter'
    elif args.adapter:
        config=json.loads(args.adapter.read_text(encoding='utf-8'));selected='adapter'
        config['command']=[arg.replace('{config_dir}',str(args.adapter.resolve().parent)) for arg in config.get('command',[])]
    elif args.harness:
        config={'command':['{python}','{kit}/qwen_bench/harness_adapter.py'],
                'deployment':{'harness':args.harness,'model':args.model,'server':'existing harness configuration'},
                'options':{'harness':args.harness,'model':args.model}}
        selected='adapter'
    try:
        if args.profile:
            if args.manual or args.demo or args.adapter: raise ValueError('Named profiles currently target direct-server or built-in native connectors')
            from qwen_bench.profiles import load_profile,apply_profile
            config=apply_profile(config,load_profile(args.profile),args.harness or 'direct')
        run,status=execute(runs_dir=args.runs_dir,mode=selected,config=config,protocol=args.protocol,
                           turn_seconds=args.turn_seconds,label=args.label)
    except (ValueError,OSError,json.JSONDecodeError) as error:
        parser.exit(2,str(error)+'\n')
    if not args.no_open:
        import webbrowser
        webbrowser.open((run/'report.html').as_uri())
    return 0 if status['trajectory']=='completed' and status['machine_verification']=='verified' else 1


if __name__=='__main__': raise SystemExit(main())
