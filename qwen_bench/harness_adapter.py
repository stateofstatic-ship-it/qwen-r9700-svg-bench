"""Diagnostic native CLI connectors. Invoke with python -m qwen_bench.harness_adapter."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

MAX_LOG = 64 * 1024 * 1024
MAX_LINE = 1024 * 1024


class HarnessError(RuntimeError):
    pass


class Harness:
    def __init__(self, emit):
        self.emit = emit
        self.session = None
        self.turns = 0
        self.ready = False

    def execute(self, args, label, prompt=b'', timeout=10):
        paths = [self.state / (label + '.' + stream + '.jsonl') for stream in ('stdout', 'stderr')]
        with paths[0].open('xb') as out, paths[1].open('xb') as err:
            process = subprocess.Popen(args, cwd=self.workspace, env=self.env, stdin=subprocess.PIPE,
                                       stdout=out, stderr=err, start_new_session=os.name == 'posix')
            deadline = time.monotonic() + timeout
            try:
                first = True
                while True:
                    if any(p.stat().st_size > MAX_LOG for p in paths):
                        raise HarnessError('Native log exceeded size limit')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HarnessError('Native CLI timed out; raw logs retained')
                    try:
                        process.communicate(input=prompt if first else None, timeout=min(.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                if process.returncode:
                    raise HarnessError('Native CLI exited with code ' + str(process.returncode))
            finally:
                if os.name == 'posix':
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                elif process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
        if any(p.stat().st_size > MAX_LOG for p in paths):
            raise HarnessError('Native log exceeded size limit')
        return paths[0]

    def preflight(self, request):
        if self.ready or request.get('benchmark_protocol') != 'v0.3-portable':
            raise HarnessError('Native harness connector requires a fresh v0.3-portable run')
        options = request.get('options', {})
        unsupported = set(options) - {'harness', 'executable', 'model', 'endpoint', 'effort'}
        if unsupported:
            raise HarnessError('Unsupported harness options: ' + ', '.join(sorted(unsupported)))
        self.kind = options.get('harness')
        if self.kind not in ('codex', 'opencode'):
            raise HarnessError('harness must be codex or opencode')
        if options.get('endpoint'):
            raise HarnessError('Configure endpoint in the native harness first; automatic endpoint mapping is unsupported. Codex needs a Responses-compatible provider, not chat-only mapping.')
        self.exe = shutil.which(options.get('executable') or self.kind)
        if not self.exe:
            raise HarnessError('Configured harness executable not found')
        self.effort = options.get('effort')
        if self.effort is not None and (not isinstance(self.effort, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', self.effort)):
            raise HarnessError('effort must be a native effort/variant identifier')
        self.model = options.get('model')
        if self.model is not None and (not isinstance(self.model, str) or not self.model or self.model.startswith('-')):
            raise HarnessError('model must be a nonempty native model identifier')
        self.workspace = Path(request['workspace']).resolve(strict=True)
        self.state = Path(request['state_dir']).resolve(strict=True) / 'native-harness'
        self.state.mkdir(exist_ok=False)
        self.env = os.environ.copy()
        if self.kind == 'opencode':
            config = json.loads(self.env.get('OPENCODE_CONFIG_CONTENT') or '{}')
            config['share'] = 'disabled'
            config.setdefault('permission', {}).update({k: 'deny' for k in ('task', 'webfetch', 'websearch', 'external_directory')})
            self.env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
        version = self.execute([self.exe, '--version'], 'preflight-version').read_text()[:1000].strip()
        help_args = ['exec', '--help'] if self.kind == 'codex' else ['run', '--help']
        help_text = self.execute([self.exe] + help_args, 'preflight-help').read_text()
        required = ('--json', '--skip-git-repo-check', '--config') if self.kind == 'codex' else ('--format', '--session', '--model')
        if self.kind == 'opencode' and self.effort and '--variant' not in help_text:
            raise HarnessError('Installed OpenCode does not advertise --variant')
        if not all(flag in help_text for flag in required):
            raise HarnessError('Installed CLI help lacks required connector flags')
        if self.kind == 'codex':
            resume = self.execute([self.exe, 'exec', 'resume', '--help'], 'preflight-resume-help').read_text()
            if '--json' not in resume or 'SESSION_ID' not in resume:
                raise HarnessError('Installed Codex does not advertise JSON session resume')
        self.ready = True
        self.emit({'type': 'preflight.completed', 'checkpoint': None, 'status': 'passed',
                   'same_session_supported': True, 'candidate_workspace': '.', 'harness': self.kind,
                   'version': version, 'requested': {'model': self.model, 'effort': self.effort},
                   'applied_arguments': self.command()[1:], 'externally_unverified': ['provider/runtime configuration', 'plugins/custom tools', 'native instruction loading', 'effective sampling settings'], 'controls': 'diagnostic, harness-owned not independently verified; inherited native auth/config/tools; requested web/helper restrictions are not a security boundary',
                   'capability_validation': 'version/help only; no inference, provider compatibility and native event schema unverified until execution'})

    def command(self):
        if self.kind == 'codex':
            args = [self.exe, '-a', 'never', 'exec']
            if self.session: args += ['resume']
            args += ['--skip-git-repo-check', '--json', '-c', 'web_search="disabled"',
                     '-c', 'sandbox_mode="workspace-write"', '-c', 'sandbox_workspace_write.network_access=false',
                     '-c', 'agents.enabled=false', '--disable', 'multi_agent', '--disable', 'image_generation']
            if self.model: args += ['-m', self.model]
            if self.effort: args += ['-c', 'model_reasoning_effort=' + json.dumps(self.effort)]
            args += [self.session, '-'] if self.session else ['-C', str(self.workspace), '-']
            return args
        args = [self.exe, 'run', '--format', 'json']
        if self.model: args += ['--model', self.model]
        if self.effort: args += ['--variant', self.effort]
        if self.session: args += ['--session', self.session]
        return args

    def turn(self, request):
        checkpoint = request.get('checkpoint')
        if not self.ready or self.turns >= 4 or checkpoint != 'C' + str(self.turns):
            raise HarnessError('Expected next checkpoint in C0..C3 after preflight')
        if request.get('session_id') != self.session:
            raise HarnessError('Outer session mismatch')
        prompt = base64.b64decode(request['prompt_base64'], validate=True)
        if hashlib.sha256(prompt).hexdigest() != request.get('prompt_sha256'):
            raise HarnessError('Prompt digest mismatch')
        prompt.decode('utf-8')
        path = self.execute(self.command(), checkpoint, prompt, float(request['timeout_seconds']))
        observed = None
        terminal = False
        usage = None
        with path.open('rb') as stream:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line: break
                if len(line) > MAX_LINE: raise HarnessError('Native event exceeds record limit')
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    raise HarnessError('Malformed native event')
                kind = event['type']
                sid = event.get('thread_id') if self.kind == 'codex' and kind == 'thread.started' else event.get('sessionID') if self.kind == 'opencode' else None
                if sid is not None:
                    if self.kind == 'codex': uuid.UUID(sid)
                    elif not isinstance(sid, str) or not re.fullmatch(r'ses_[A-Za-z0-9]+', sid):
                        raise HarnessError('Invalid OpenCode session identifier')
                    if (observed and sid != observed) or (self.session and sid != self.session):
                        raise HarnessError('Native session changed')
                    observed = sid
                if kind in ('error', 'turn.failed'):
                    raise HarnessError('Native CLI reported failure; inspect retained raw trace')
                item = event.get('item', {}) if self.kind == 'codex' else event.get('part', {})
                if self.kind == 'codex' and kind == 'turn.completed':
                    terminal = True
                    usage = event.get('usage')
                elif self.kind == 'opencode' and kind == 'step_finish':
                    terminal = item.get('reason') == 'stop'
                    usage = item.get('tokens')
                if kind == 'tool_use' or (kind == 'item.completed' and item.get('type') in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search')):
                    self.emit({'type': 'tool.completed', 'checkpoint': checkpoint, 'native': item})
                elif kind == 'text' or (kind == 'item.completed' and item.get('type') == 'agent_message'):
                    self.emit({'type': 'assistant.message', 'checkpoint': checkpoint, 'text': item.get('text', '')})
        if not observed or not terminal:
            raise HarnessError('Missing native session or successful terminal event')
        self.session = observed
        self.turns += 1
        self.emit({'type': 'turn.completed', 'checkpoint': checkpoint, 'status': 'completed', 'session_id': observed,
                   'usage': usage, 'terminal_source': 'native_cli_event_and_exit_zero', 'raw_trace': str(path)})


def main():
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    def interrupted(signum, frame):
        raise HarnessError('Adapter interrupted; native process cleanup requested')
    if os.name == 'posix':
        signal.signal(signal.SIGTERM, interrupted)
    harness = Harness(emit)
    checkpoint = None
    try:
        for line in sys.stdin.buffer:
            if len(line) > MAX_LINE: raise HarnessError('Outer request exceeds record limit')
            request = json.loads(line)
            checkpoint = request.get('checkpoint')
            if request['type'] == 'shutdown':
                emit({'type': 'run.closed', 'checkpoint': None})
                return 0
            if request['type'] == 'preflight': harness.preflight(request)
            elif request['type'] == 'turn': harness.turn(request)
            else: raise HarnessError('Unknown request type')
        return 0
    except Exception as error:
        emit({'type': 'error', 'checkpoint': checkpoint, 'message': str(error)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
