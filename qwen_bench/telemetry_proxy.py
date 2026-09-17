"""Authenticated, bounded loopback transport for native harness inference telemetry."""
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .endpoint import EndpointError
from .runtime_metrics import RuntimeCollector
from .telemetry import SCHEMA, WORK_TYPES, usage_counts

MAX_BODY = 8 * 1024 * 1024
PROTECTED = {'messages', 'tools', 'tool_choice', 'model', 'stream', 'n'}


class TelemetryProxy:
    def __init__(self, endpoint, model, state: Path, emit, overrides, max_requests=64):
        if type(max_requests) is not int or not 1 <= max_requests <= 64:
            raise ValueError('Invalid request budget')
        if not isinstance(overrides, dict) or PROTECTED.intersection(overrides):
            raise ValueError('Overrides cannot replace the native conversation contract')
        self.endpoint, self.model, self.state, self.emit = endpoint, model, Path(state), emit
        self.overrides, self.max_requests = dict(overrides), max_requests
        self.metrics = RuntimeCollector(endpoint, model)
        self.key = secrets.token_urlsafe(32)
        self.serial = 0
        self._condition = threading.Condition()
        self._phase = None
        self._active = False
        self._finishing = False
        self._cancel = threading.Event()
        self._upstream_socket = None
        self._writes_closed = False
        self._closed = False
        self._server = None
        self._connections = set()

    def _emit(self, value):
        with self._condition:
            if not self._closed:
                self.emit(value)

    def _redact(self, value):
        if isinstance(value, str):
            for secret in (self.endpoint.key, self.key):
                if secret: value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _save(self, name, value):
        with self._condition:
            if self._writes_closed:
                return
            self._save_locked(name, value)

    def _save_locked(self, name, value):
        data = json.dumps(self._redact(value), ensure_ascii=False, indent=2)
        fd = os.open(self.state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data + '\n')

    def start(self):
        if self._server or self._closed:
            raise EndpointError('Proxy already started or closed')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)
                with owner._condition:
                    owner._connections.add(self.connection)

            def finish(self):
                try:
                    super().finish()
                finally:
                    with owner._condition:
                        owner._connections.discard(self.connection)

            def reply(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                supplied = self.headers.get('Authorization', '')
                if not hmac.compare_digest(supplied.encode(), ('Bearer ' + owner.key).encode()):
                    self.reply(401, {'error': 'Proxy authorization required'})
                    return False
                return True

            def do_GET(self):
                if not self.authorized(): return
                if self.path != '/v1/models':
                    self.reply(404, {'error': 'Unsupported proxy route'})
                    return
                self.reply(200, {'object': 'list', 'data': [{'id': owner.model, 'object': 'model'}]})

            def do_POST(self):
                if not self.authorized(): return
                if self.path != '/v1/chat/completions':
                    self.reply(404, {'error': 'Unsupported proxy route'})
                    return
                lengths = self.headers.get_all('Content-Length', [])
                if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit():
                    self.reply(400, {'error': 'A single Content-Length is required'})
                    return
                length = int(lengths[0])
                if length > MAX_BODY:
                    self.reply(413, {'error': 'Request exceeds 8 MiB'})
                    return
                with owner._condition:
                    phase = owner._phase
                    while (owner._active and owner._finishing and phase is not None
                           and owner._phase is phase and not owner._closed
                           and time.monotonic() < phase[1]):
                        owner._condition.wait(max(.001, phase[1] - time.monotonic()))
                    if owner._closed or phase is None or owner._phase is not phase or time.monotonic() >= phase[1]:
                        self.reply(409, {'error': 'No active inference section'})
                        return
                    if owner._active:
                        self.reply(409, {'error': 'Concurrent inference prohibited'})
                        return
                    if owner.serial >= owner.max_requests:
                        self.reply(429, {'error': 'Inference request budget exhausted'})
                        return
                    owner._active = True
                    owner._finishing = False
                try:
                    self.connection.settimeout(max(.001, min(5, phase[1] - time.monotonic())))
                    chunks, received = [], 0
                    while received < length:
                        remaining = phase[1] - time.monotonic()
                        if remaining <= 0: raise ValueError()
                        self.connection.settimeout(min(5, remaining))
                        block = self.rfile.read1(min(65536, length - received))
                        if not block: raise ValueError()
                        chunks.append(block)
                        received += len(block)
                    raw = b''.join(chunks)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list): raise ValueError()
                    if 'stream' in payload and type(payload['stream']) is not bool: raise ValueError()
                    payload.update(owner.overrides)
                    payload['model'] = owner.model
                    if payload.get('stream'):
                        options = payload.get('stream_options') or {}
                        if not isinstance(options, dict): raise ValueError()
                        payload['stream_options'] = dict(options, include_usage=True)
                    if len(json.dumps(payload).encode()) > MAX_BODY:
                        self.reply(413, {'error': 'Effective request exceeds 8 MiB'})
                        return
                    owner._infer(self, payload, phase)
                except (ValueError, UnicodeError):
                    self.reply(400, {'error': 'Invalid completion request'})
                except (OSError, EndpointError):
                    # Never include provider bodies, exceptions, or keys in HTTP errors.
                    self.close_connection = True
                finally:
                    with owner._condition:
                        owner._active = False
                        owner._finishing = False
                        owner._upstream_socket = None
                        owner._condition.notify_all()

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self._thread.start()
        return f'http://127.0.0.1:{self._server.server_port}/v1'

    def begin_section(self, checkpoint, timeout_seconds):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Invalid section timeout')
        with self._condition:
            if self._closed or self._phase is not None or self._active:
                raise EndpointError('Proxy section is already active or closed')
            self._phase = (checkpoint, time.monotonic() + timeout_seconds)
            self._section_requests = 0

    def end_section(self):
        with self._condition:
            phase, self._phase = self._phase, None
            until = (phase[1] if phase else time.monotonic()) + 3
            while self._active and time.monotonic() < until:
                self._condition.wait(min(.1, until - time.monotonic()))
            if self._active:
                raise EndpointError('Inference remains active after section deadline')

    def _infer(self, handler, payload, phase):
        self.serial += 1
        serial = self.serial
        prefix = f'request-{serial:04d}'
        checkpoint, deadline = phase
        trigger = 'section_prompt' if self._section_requests == 0 else 'native_continuation'
        self._section_requests += 1
        self._save(prefix + '.json', payload)
        self._emit(dict(type='adapter.event', checkpoint=checkpoint, name='inference.request.started', request=serial, trigger=trigger))
        before = self.metrics.before()
        response, client, failure, sent = None, {}, None, False

        def opened(response):
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            with self._condition:
                self._upstream_socket = sock
                if self._closed and sock is not None:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass

        def terminal():
            with self._condition:
                self._finishing = True
                self._condition.notify_all()

        def headers(status, content_type):
            nonlocal sent
            if self._closed: raise EndpointError('Proxy closed')
            handler.send_response(status)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Connection', 'close')
            handler.end_headers()
            sent = True

        def data(block):
            if self._closed: raise EndpointError('Proxy closed')
            handler.connection.settimeout(max(.001, deadline - time.monotonic()))
            handler.wfile.write(block)
            handler.wfile.flush()

        try:
            response, client = self.endpoint.call_measured('/chat/completions', payload,
                timeout=deadline - time.monotonic(), on_response=headers, on_data=data,
                on_terminal=terminal, on_open=opened, cancel_event=self._cancel)
        except EndpointError as exc:
            failure, client, response = exc, exc.telemetry or {}, exc.response
        finally:
            runtime = ({'status': 'unavailable', 'source': None, 'unavailable_reason': 'proxy closed during inference'}
                       if self._cancel.is_set() else self.metrics.after(before, response))
            snapshots = runtime.pop('raw_snapshots', None)
            if snapshots is not None: self._save(prefix + '-runtime-snapshots.json', snapshots)
            self._save(prefix + '-response.json', response)
            choices = response.get('choices', []) if isinstance(response, dict) else []
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message = choice.get('message') or {}
            calls = message.get('tool_calls') or [] if isinstance(message, dict) else []
            names = [c['function']['name'][:128] for c in calls[:64] if isinstance(c, dict) and isinstance(c.get('function'), dict) and isinstance(c['function'].get('name'), str)] if isinstance(calls, list) else []
            client['measurement_scope'] = 'proxy_to_upstream'
            client['measurement_definition'] = 'Arrival at proxy before downstream writes; excludes harness-to-proxy transport. Downstream backpressure may delay later reads and total duration.'
            telemetry = dict(schema=SCHEMA, checkpoint=checkpoint, request=serial, work_type=WORK_TYPES.get(checkpoint),
                trigger=trigger, outcome='transport_failed' if failure else (choice.get('finish_reason') if choice.get('finish_reason') in ('stop', 'length', 'tool_calls', 'function_call', 'content_filter') else 'invalid_response'),
                client=client, runtime=runtime, usage=usage_counts(response.get('usage')) if response else {}, tool_names=names)
            self._save(prefix + '-telemetry.json', telemetry)
            public = dict(telemetry, client={k: v for k, v in client.items() if k != 'delta_arrival_seconds'})
            # Event labels may have been reflected by the provider as well.
            self._emit(self._redact(dict(type='telemetry', **public)))
            if not failure:
                self._emit(dict(type='usage', checkpoint=checkpoint, request=serial,
                    scope='provider response for this request (includes replayed history)',
                    raw=telemetry['usage']))
        if failure and not sent:
            handler.reply(failure.status if failure.status and 400 <= failure.status <= 599 else 502,
                          {'error': 'Upstream inference failed'})
        handler.close_connection = True

    def close(self):
        """Interrupt open upstream reads and allow bounded private finalization.

        DNS/connect/header stalls before response registration are not cancellable
        by urllib; the owning adapter must retain its outer process deadline.
        No private writes or public events are allowed after this method returns.
        """
        with self._condition:
            self._closed, self._phase = True, None
            self._cancel.set()
            self._condition.notify_all()
            sockets = list(self._connections)
            if self._upstream_socket is not None:
                sockets.append(self._upstream_socket)
            for connection in sockets:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=1)
        with self._condition:
            until = time.monotonic() + 2.5
            while self._active and time.monotonic() < until:
                self._condition.wait(max(.001, until - time.monotonic()))
            self._writes_closed = True
