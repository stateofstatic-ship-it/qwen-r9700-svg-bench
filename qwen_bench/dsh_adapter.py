"""Persistent native DeepSeek Harness connector; no global DSH configuration writes.

Native file policy is not a read/network security sandbox. Process-tree cleanup is
best effort: service-manager-reparented descendants can escape it; Windows has no Job
Object containment here. Private captures contain prompts, tool content and paths.
"""
import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_bench.endpoint import Endpoint

MAX_LINE = 1024 * 1024
MAX_LOG = 64 * 1024 * 1024
SUPPORTED_VERSION = '0.1.5-rc.2'
DISABLED = ('session-title-llm', 'tool-subagent', 'tool-subagent-fork',
            'tool-subagent-control', 'tool-subagent-list-agents',
            'tool-workflow', 'tool-ralph', 'tool-web')


class DshError(RuntimeError):
    pass


def install_for(executable=None):
    """Resolve the real installed launcher, without invoking profile boot/npm."""
    launcher = shutil.which(executable or 'dsh')
    node = shutil.which('node')
    if not launcher or not node:
        raise DshError('Installed dsh and Node.js are required; nothing is installed automatically')
    launcher = Path(launcher).resolve(strict=True)
    for parent in launcher.parents:
        package = parent / 'package.json'
        if package.is_file():
            try:
                manifest = json.loads(package.read_text())
            except (ValueError, OSError):
                continue
            if manifest.get('name') == '@deepseek-ai/dsh':
                version = manifest.get('version')
                if not isinstance(version, str) or not version:
                    raise DshError('DSH package has no version')
                return str(node), str(launcher), package, version
    raise DshError('Cannot locate installed @deepseek-ai/dsh package from launcher; use its real launcher path')


def positive_integer(value, field, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise DshError('Invalid ' + field)
    return value


def resolve_options(options):
    allowed = {'harness', 'executable', 'endpoint', 'model', 'effort', 'max_tokens',
               'temperature', 'top_p', 'seed', 'request_options', 'max_requests',
               'context_window', 'api_key_env'}
    if not isinstance(options, dict) or set(options) - allowed:
        raise DshError('Unknown DSH option')
    if options.get('harness', 'dsh') != 'dsh':
        raise DshError('This connector requires harness=dsh')
    if not isinstance(options.get('endpoint'), str):
        raise DshError('DSH requires an endpoint base URL')
    model = options.get('model')
    if model is not None and (not isinstance(model, str) or not model):
        raise DshError('Invalid model')
    effort = options.get('effort', 'xhigh')
    if effort not in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise DshError('Invalid DSH reasoning effort')
    config = dict(options)
    config.update(effort=effort,
                  max_tokens=positive_integer(options.get('max_tokens', 65536), 'max_tokens', 1048576),
                  max_requests=positive_integer(options.get('max_requests', 64), 'max_requests', 64))
    for field, default, maximum in (('temperature', .8, 2), ('top_p', .95, 1)):
        value = options.get(field, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise DshError('Invalid ' + field)
        config[field] = value
    extra = options.get('request_options', {})
    forbidden = {'messages', 'tools', 'tool_choice', 'model', 'stream', 'stream_options', 'n',
                 'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'seed', 'reasoning_effort'}
    if not isinstance(extra, dict) or set(extra) & forbidden:
        raise DshError('request_options cannot override conversation/tools or named settings')
    try:
        if len(json.dumps(extra, allow_nan=False).encode()) > 32768:
            raise DshError('request_options exceeds 32 KiB')
    except (TypeError, ValueError):
        raise DshError('request_options must be finite JSON') from None
    config['request_options'] = extra
    if 'seed' in options and type(options['seed']) is not int:
        raise DshError('seed must be an integer')
    if 'context_window' in options:
        positive_integer(options['context_window'], 'context_window', 16777216)
    return config


def enable_child_reaping():
    """Adopt native grandchildren on Linux so cancellation can reap them too."""
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise DshError('Cannot enable native descendant reaping')


def direct_children(pid=None):
    """Read all thread-owned direct children in the Linux host PID namespace."""
    children = set()
    if sys.platform.startswith('linux'):
        for task in (Path('/proc') / str(pid or os.getpid()) / 'task').iterdir():
            try:
                children.update(int(value) for value in (task / 'children').read_text().split())
            except FileNotFoundError:
                continue
    return children


def stop_tree(process, child_baseline=None):
    """Freeze a Linux descendant tree before killing detached stock tool groups.

    This is not cgroup containment: children already reparented to an external
    service manager cannot be discovered through this tree.
    """
    captured = []
    if sys.platform.startswith('linux'):
        pending = [process.pid] if process.poll() is None else []
        if child_baseline is not None:
            pending.extend(direct_children() - child_baseline)
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGSTOP)
                captured.append(pid)
                pending.extend(direct_children(pid))
            except (ProcessLookupError, FileNotFoundError):
                continue
    for pid in reversed(captured):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.kill()
    process.wait(timeout=.2)
    # Adopted descendants die asynchronously; do not wait on unrelated children.
    until = time.monotonic() + .15
    pending = set(captured) - {process.pid}
    while pending and time.monotonic() < until:
        for pid in list(pending):
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    pending.remove(pid)
            except ChildProcessError:
                pending.remove(pid)
        if pending:
            time.sleep(.001)
    if pending:
        raise DshError('Native descendants did not exit within cleanup deadline')


def dump(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    path.chmod(0o600)


class Harness:
    def __init__(self, emit):
        self._public_sink = emit
        self.public_secrets = set()
        self.child_baseline = None
        self.session = None
        self.native_session = None
        self.turns = 0
        self.ready = False
        self.process = None
        self.proxy = None
        self.readers = []
        self.events = queue.Queue(maxsize=512)
        self.reader_stop = threading.Event()
        self.reader_failure = None

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self.public_secrets, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def emit(self, event):
        self._public_sink(self.redact(event))

    def preflight(self, request):
        if self.ready or self.process or request.get('benchmark_protocol') != 'v0.3-portable':
            raise DshError('DSH requires a fresh v0.3-portable run')
        self.config = resolve_options(request.get('options', {}))
        node, launcher, package, version = install_for(self.config.get('executable'))
        if version != SUPPORTED_VERSION:
            raise DshError('Unsupported DSH version: ' + version + '; expected ' + SUPPORTED_VERSION)
        self.workspace = Path(request['workspace']).resolve(strict=True)
        parent = Path(request['state_dir']).resolve(strict=True)
        if not self.workspace.is_dir() or not parent.is_dir() or parent == self.workspace or self.workspace in parent.parents:
            raise DshError('Private adapter state must be outside candidate workspace')
        self.state = parent / 'dsh-native'
        self.state.mkdir(mode=0o700)
        self.home = self.state / 'home'
        self.home.mkdir(mode=0o700)
        dump(self.state / 'preflight-request.json', request)
        endpoint = Endpoint(self.config['endpoint'], self.config.get('api_key_env', 'SVG_BENCH_API_KEY'))
        self.public_secrets.add(endpoint.key)
        metadata = endpoint.call('/models', timeout=8)
        data = metadata.get('data')
        if not isinstance(data, list) or not data or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in data):
            raise DshError('GET /models must return a nonempty model list')
        model = self.config.get('model')
        if model is None:
            if len(data) != 1:
                raise DshError('Specify model when endpoint lists multiple models')
            model = data[0]['id']
        selected = next((item for item in data if item['id'] == model), None)
        if selected is None:
            raise DshError('Requested model is not listed by endpoint')
        context = self.config.get('context_window', selected.get('max_model_len'))
        if context is None:
            raise DshError('Set context_window: selected /models entry has no max_model_len')
        positive_integer(context, 'context_window', 16777216)
        self.config.update(model=model, context_window=context)
        dump(self.state / 'resolved-options.json', self.config)
        overrides = dict(self.config['request_options'], temperature=self.config['temperature'],
                         top_p=self.config['top_p'], max_tokens=self.config['max_tokens'],
                         reasoning_effort=self.config['effort'])
        if 'seed' in self.config:
            overrides['seed'] = self.config['seed']
        from qwen_bench.telemetry_proxy import TelemetryProxy
        self.proxy = TelemetryProxy(endpoint, model, self.state, self.emit, overrides,
                                    max_requests=self.config['max_requests'])
        self.public_secrets.add(self.proxy.key)
        local_url = self.proxy.start()
        profile = self.home / 'profiles' / 'svg-benchmark'
        profile.mkdir(parents=True)
        dump(profile / 'package.json', {'name': 'svg-benchmark-dsh', 'private': True,
             'dependencies': {}, 'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless']}}})
        dump(profile / 'cordis.yml', [])
        patches = [{'id': key, 'disabled': True} for key in (*DISABLED, 'headless-startup', 'headless-runner')]
        patches += [{'id': 'tools', 'config': {'mode': 'native'}},
                    {'id': 'sandbox-policy', 'config': {'mode': 'workspace-write', 'workspaceRoot': str(self.workspace)}},
                    {'id': 'approval', 'config': {'policy': 'never'}},
                    {'id': 'permission', 'config': {'presets': {'workspace-write': {'sandbox': 'workspace-write', 'approval': 'never'}}, 'defaultPreset': 'workspace-write'}},
                    {'insert': [{'id': 'svg-benchmark-runner', 'name': Path(__file__).with_name('dsh_runner.mjs').as_uri()}]}]
        # Installed stock base/headless bundles do not include MCP or image generation.
        # Refuse unexpected added MCP/image integrations rather than silently inherit them.
        base_patch = package.parent / 'node_modules/@deepseek-ai/dsh-base/cordis.patch.yml'
        if not base_patch.is_file():
            raise DshError('Installed DSH base bundle layout is unsupported')
        for line in base_patch.read_text().splitlines():
            if 'name:' in line and any(term in line.lower() for term in ('mcp', 'image-gen', 'imagegen')):
                raise DshError('Installed base includes an unreviewed MCP/image integration')
        dump(profile / 'cordis.patch.yml', patches)
        effort = self.config['effort']
        efforts = {effort: effort} if effort != 'off' else False
        dump(self.home / 'settings.yaml', {
            'llm-pi-ai': {'providers': {'svg-benchmark-local': {
                'apiKeyEnv': 'SVG_BENCH_DSH_KEY', 'api': 'openai-completions', 'baseURL': local_url,
                'models': [{'id': model, 'contextWindow': context, 'maxTokens': self.config['max_tokens'],
                            'input': ['text'], 'reasoningEfforts': efforts,
                            'compat': {'maxTokensField': 'max_tokens'}}]}}},
            'agent-default-model': {'provider': 'svg-benchmark-local', 'model': model,
                                    **({'reasoningEffort': effort} if effort != 'off' else {})}})
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('DSH_', 'SVG_BENCH_DSH_'))}
        self.env.pop(self.config.get('api_key_env', 'SVG_BENCH_API_KEY'), None)
        self.env.update(DSH_HOME=str(self.home), DSH_TELEMETRY_DISABLED='1',
                        SVG_BENCH_DSH_PACKAGE=str(package), SVG_BENCH_DSH_KEY=self.proxy.key,
                        SVG_BENCH_DSH_RAW=str(self.state / 'native-events.jsonl'),
                        SVG_BENCH_DSH_MAX_TOKENS=str(self.config['max_tokens']))
        self.command = [node, launcher, '--profile', 'svg-benchmark']
        self.child_baseline = direct_children()
        self.process = subprocess.Popen(self.command, cwd=self.workspace, env=self.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == 'posix')
        for name in ('stdout', 'stderr'):
            thread = threading.Thread(target=self._read, args=(name,), daemon=True)
            self.readers.append(thread)
            thread.start()
        event = self._receive('native.ready', None, time.monotonic() + 15)
        self.native_session = event.get('session_id')
        if not isinstance(self.native_session, str) or not self.native_session.startswith('session-'):
            raise DshError('Missing native DSH session identity')
        self.ready = True
        self.emit({'type': 'preflight.completed', 'checkpoint': None, 'status': 'passed',
                   'same_session_supported': True, 'candidate_workspace': '.', 'harness': 'dsh',
                   'version': version, 'requested': request.get('options', {}), 'resolved': self.config,
                   'native_session_id': self.native_session, 'applied_arguments': self.command[1:],
                   'disabled_plugins': list(DISABLED),
                   'controls': 'Diagnostic native DSH tools with workspace-write file policy and approval never; not a read/network security sandbox. Isolated DSH configuration; no global writes. Linux descendant-tree cleanup is best effort, not service-manager/cgroup containment.',
                   'capability_validation': 'Installed package and real native agent startup verified without inference; model/tool compatibility unverified until execution',
                   'externally_unverified': ['provider compatibility', 'effective native file-policy enforcement', 'native instruction loading']})

    def _read(self, name):
        stream = getattr(self.process, name)
        size = 0
        try:
            with (self.state / ('native-' + name + '.log')).open('xb') as sink:
                while not self.reader_stop.is_set():
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        if name == 'stdout':
                            self._queue(('eof', None))
                        return
                    size += len(line)
                    if len(line) > MAX_LINE or size > MAX_LOG:
                        raise DshError('Native ' + name + ' capture exceeded size limit')
                    sink.write(line)
                    sink.flush()
                    if name == 'stdout':
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue  # Native launcher/logging output remains in private stdout capture.
                        if not isinstance(event, dict) or event.get('__dsh_bench__') != 1:
                            continue
                        if not isinstance(event.get('type'), str):
                            raise DshError('Malformed native runner event')
                        del event['__dsh_bench__']
                        self._queue(('event', event))
        except Exception as error:
            self.reader_failure = DshError('Native stream failed: ' + str(error)[:512])
            self._queue(('error', None))

    def _queue(self, item):
        while not self.reader_stop.is_set():
            try:
                self.events.put(item, timeout=.1)
                return
            except queue.Full:
                continue

    def _receive(self, expected, checkpoint, deadline):
        while True:
            if self.reader_failure:
                raise self.reader_failure
            left = deadline - time.monotonic()
            if left <= 0:
                raise DshError('Native DSH timed out; private traces retained')
            try:
                kind, event = self.events.get(timeout=min(.1, left))
            except queue.Empty:
                continue
            if kind != 'event':
                raise DshError('Native DSH exited before successful terminal event')
            if event['type'] == 'error':
                raise DshError('Native DSH failed: ' + str(event.get('message', 'unknown'))[:2048])
            if event.get('checkpoint') != checkpoint:
                raise DshError('Native event checkpoint mismatch')
            if self.native_session is not None and event.get('session_id') != self.native_session:
                raise DshError('Native DSH session changed')
            if event['type'] == expected:
                return event
            if event['type'] not in ('turn.started', 'tool.started', 'tool.completed', 'assistant.message'):
                raise DshError('Unexpected native runner event')
            self.emit(event)

    def _send(self, request):
        raw = (json.dumps(request, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > MAX_LINE:
            raise DshError('Native request exceeds 1 MiB')
        if self.process.poll() is not None:
            raise DshError('Native process already exited')
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def turn(self, request):
        checkpoint = request.get('checkpoint')
        if not self.ready or self.turns >= 4 or checkpoint != 'C' + str(self.turns):
            raise DshError('Expected next checkpoint C0..C3 after preflight')
        if request.get('session_id') != self.session:
            raise DshError('Outer session mismatch')
        prompt = base64.b64decode(request['prompt_base64'], validate=True)
        if hashlib.sha256(prompt).hexdigest() != request.get('prompt_sha256'):
            raise DshError('Prompt digest mismatch')
        prompt.decode('utf-8')
        timeout = request.get('timeout_seconds')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise DshError('Invalid section timeout')
        deadline = time.monotonic() + timeout
        self.proxy.begin_section(checkpoint, timeout)
        try:
            self._send(dict(request, type='turn', session_id=self.native_session))
            event = self._receive('turn.completed', checkpoint, deadline)
            terminal = event.get('native_terminal', {})
            if event.get('status') != 'completed' or terminal.get('type') != 'turn/end' or terminal.get('data', {}).get('reason', {}).get('kind') != 'completed':
                raise DshError('Missing successful native turn/end evidence')
            if event.get('prompt_sha256') != request['prompt_sha256']:
                raise DshError('Native exact-prompt receipt mismatch')
            if self.process.poll() is not None:
                raise DshError('Persistent native process exited during turn')
        except BaseException:
            # Never wait for an upstream deadline before terminating native work.
            self.stop_native()
            raise
        self.proxy.end_section()
        self.session = self.native_session
        self.turns += 1
        self.emit(event)

    def stop_native(self):
        """Immediate cancellation, before proxy waits and the parent's kill deadline."""
        if self.process is None:
            return
        stop_tree(self.process, self.child_baseline)

    def close(self, graceful=False):
        """Return only after process exit and captured streams drain, or raise."""
        error = None
        if self.process:
            try:
                if graceful:
                    self._send({'type': 'shutdown'})
                    self._receive('native.closed', None, time.monotonic() + 2)
                    self.process.wait(timeout=1)
                    if self.process.returncode != 0:
                        raise DshError('Native DSH failed during shutdown')
            except Exception as exc:
                error = exc
            finally:
                try:
                    self.stop_native()
                except Exception as exc:
                    error = error or exc
                for thread in self.readers:
                    thread.join(timeout=.25)
                self.reader_stop.set()
                if any(thread.is_alive() for thread in self.readers):
                    error = error or DshError('Native capture failed to drain; detached descendant may remain')
                if self.reader_failure:
                    error = error or self.reader_failure
                if graceful and error is None:
                    while not self.events.empty():
                        if self.events.get_nowait()[0] != 'eof':
                            error = DshError('Unexpected native output after shutdown receipt')
                            break
                self.process.stdin.close()
                for stream, thread in zip((self.process.stdout, self.process.stderr), self.readers):
                    if not thread.is_alive():
                        stream.close()
                self.process = None
        if self.proxy:
            try:
                self.proxy.close()
            except Exception as exc:
                error = error or exc
            self.proxy = None
        if error:
            raise error


def main():
    lock = threading.Lock()

    def emit(event):
        line = json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n'
        if len(line.encode('utf-8')) > MAX_LINE:
            raise DshError('Adapter event exceeds 1 MiB')
        with lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def interrupted(signum, frame):
        harness.stop_native()
        raise DshError('Adapter interrupted; native cleanup requested')

    enable_child_reaping()
    harness = Harness(emit)
    if os.name == 'posix':
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
    checkpoint = None
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                raise DshError('Parent input closed before shutdown')
            if len(line) > MAX_LINE:
                raise DshError('Outer request exceeds 1 MiB')
            request = json.loads(line)
            checkpoint = request.get('checkpoint')
            if request.get('type') == 'preflight':
                harness.preflight(request)
            elif request.get('type') == 'turn':
                harness.turn(request)
            elif request.get('type') == 'shutdown':
                harness.close(graceful=True)
                harness.emit({'type': 'run.closed', 'checkpoint': None})
                return 0
            else:
                raise DshError('Unknown request type')
    except (Exception, KeyboardInterrupt) as error:
        try:
            harness.close()
        except Exception as cleanup:
            error = DshError(str(error) + '; cleanup: ' + str(cleanup))
        harness.emit({'type': 'error', 'checkpoint': checkpoint, 'message': harness.redact(str(error))[:4096]})
        return 1


if __name__ == '__main__':
    sys.exit(main())
