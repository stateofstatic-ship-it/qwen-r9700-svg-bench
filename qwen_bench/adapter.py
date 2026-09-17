"""One persistent JSONL adapter process; no model SDK or shell interpolation."""
import json
import os
import queue
import signal
import subprocess
import threading
import time

MAX_LINE = 1024 * 1024
MAX_LOG = 64 * 1024 * 1024


class AdapterError(RuntimeError): pass


class CommandAdapter:
    def __init__(self, command, run, record, cancel_event=None):
        self.run, self.record = run, record
        self.phase = 'preflight'
        self.queue = queue.Queue(maxsize=128)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.sent = None
        self.cancel_event = cancel_event
        self.cleanup_warnings = []
        self.process = subprocess.Popen(command, cwd=run/'private-adapter-state', stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == 'posix',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        self.threads = []
        for stream, name in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
            thread = threading.Thread(target=self.read, args=(stream, name), daemon=True)
            thread.start(); self.threads.append(thread)

    def put(self, item):
        while not self.stopping.is_set():
            try: self.queue.put(item, timeout=.1); return
            except queue.Full: pass

    def read(self, stream, name):
        total = 0
        try:
            with (self.run/'raw'/f'adapter.{name}.log').open('xb') as raw:
                while not self.stopping.is_set():
                    data = stream.readline(MAX_LINE+1)
                    if not data: break
                    raw.write(data); raw.flush(); total += len(data)
                    with self.lock:
                        phase = self.phase
                        if phase.startswith('C'):
                            with (self.run/'checkpoints'/phase/f'adapter.{name}.log').open('ab') as section:
                                section.write(data)
                    if len(data) > MAX_LINE or total > MAX_LOG:
                        self.put(('error', f'{name} exceeds bounded logging limit')); return
                    if name == 'stdout': self.put(('line', data))
        except Exception as error:
            self.put(('error', f'{name} reader: {error}'))
        finally:
            if name == 'stdout': self.put(('eof', None))

    def send(self, request):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError('Run stopped before dispatch')
        if self.sent is not None and self.sent.is_alive():
            raise AdapterError('Previous request was not consumed')
        encoded = json.dumps(request, ensure_ascii=False).encode('utf-8')+b'\n'
        if len(encoded) > MAX_LINE: raise AdapterError('Request exceeds protocol record limit')
        self.record('request', request)
        def write():
            try: self.process.stdin.write(encoded); self.process.stdin.flush()
            except Exception as error: self.put(('error', f'adapter stdin: {error}'))
        self.sent = threading.Thread(target=write, daemon=True); self.sent.start()

    def receive(self, terminal_type, checkpoint, timeout):
        deadline = time.monotonic()+timeout
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise InterruptedError('Run stopped by user')
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError(f'{checkpoint or "preflight"} exceeded {timeout} seconds')
            try: kind, value = self.queue.get(timeout=min(.2, remaining))
            except queue.Empty: continue
            if kind == 'error': raise AdapterError(value)
            if kind == 'eof': raise AdapterError('Adapter stdout closed before required terminal event')
            try: event = json.loads(value)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise AdapterError(f'Malformed adapter JSONL: {error}') from error
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise AdapterError('Adapter event must be an object with a type')
            self.record('adapter', event)
            if event.get('checkpoint') != checkpoint:
                raise AdapterError('Event checkpoint does not match active section')
            if event['type'] in ('error', 'turn.failed', 'preflight.failed'):
                raise AdapterError('Adapter reported failure: '+str(event.get('message', event['type'])))
            if event['type'] == terminal_type:
                if self.sent is not None:
                    self.sent.join(timeout=min(.1, max(0, deadline-time.monotonic())))
                    if self.sent.is_alive(): raise AdapterError('Terminal event arrived before request was consumed')
                return event
            if event['type'] not in ('turn.started','tool.started','tool.completed','assistant.message','usage','adapter.event','telemetry'):
                raise AdapterError('Unexpected adapter event type: '+event['type'])

    def set_phase(self, phase):
        with self.lock: self.phase = phase

    def shutdown(self):
        self.set_phase('shutdown')
        self.send({'type':'shutdown','checkpoint':None})
        event = self.receive('run.closed', None, 5)
        self.process.stdin.close()
        code = self.process.wait(timeout=5)
        if code != 0: raise AdapterError(f'Adapter exited {code} after run.closed')
        # Any extra event (especially a delayed error) prevents clean completion.
        for thread in self.threads: thread.join(timeout=.5)
        if any(thread.is_alive() for thread in self.threads):
            raise AdapterError('Adapter readers did not reach EOF after run.closed')
        saw_eof=False
        while not self.queue.empty():
            kind, value = self.queue.get_nowait()
            if kind != 'eof': raise AdapterError('Unexpected output/error after run.closed')
            saw_eof=True
        if not saw_eof: raise AdapterError('Missing stdout EOF after run.closed')
        return event

    def close(self):
        self.stopping.set()
        # POSIX process group cleanup; Windows taskkill is a best-effort tree stop.
        if os.name == 'posix':
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(self.process.pid, sig)
                except ProcessLookupError: pass
                except OSError as error:
                    self.cleanup_warnings.append(f'Process-group signal {sig} failed: {error}')
                if sig == signal.SIGTERM:
                    try: self.process.wait(timeout=.5)
                    except (OSError, subprocess.TimeoutExpired): pass
        elif self.process.poll() is None:
            try: subprocess.run(['taskkill','/PID',str(self.process.pid),'/T','/F'],capture_output=True,timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                self.cleanup_warnings.append(f'Windows tree stop failed: {error}')
        if self.process.poll() is None:
            try: self.process.kill()
            except ProcessLookupError: pass
            except OSError as error:
                self.cleanup_warnings.append(f'Direct-child stop failed: {error}')
        try: self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.cleanup_warnings.append(f'Direct-child exit not confirmed: {error}')
        for thread in self.threads: thread.join(timeout=1)
        if self.sent is not None: self.sent.join(timeout=1)
        owners=[self.sent,*self.threads]
        for stream,owner in zip((self.process.stdin,self.process.stdout,self.process.stderr),owners):
            if owner is not None and owner.is_alive():
                self.cleanup_warnings.append('I/O owner still alive; buffered stream left open to avoid deadlock')
            elif not stream.closed:
                try: stream.close()
                except OSError as error:
                    self.cleanup_warnings.append(f'Buffered stream close failed: {error}')
