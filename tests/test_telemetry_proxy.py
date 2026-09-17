import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error, parse
from unittest.mock import patch

from qwen_bench.endpoint import Endpoint, EndpointError
from qwen_bench.telemetry_proxy import TelemetryProxy


def event(delta=None, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}) + '\n\n').encode()


@contextmanager
def setup(parts=None, status=200, gate=None, maximum=64):
    received = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(404); self.end_headers()
        def do_POST(self):
            received.append((self.headers.get('Authorization'), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(status)
            self.send_header('Content-Type', 'text/event-stream' if parts else 'application/json')
            self.end_headers()
            try:
                if parts:
                    for index, block in enumerate(parts):
                        self.wfile.write(block); self.wfile.flush()
                        if index == 0 and gate: gate.wait(3)
                else:
                    self.wfile.write(json.dumps({'choices': [{'message': {'content': 'upstream-secret'}, 'finish_reason': 'stop'}], 'usage': {'completion_tokens': 2, 'private': 'hidden'}}).encode())
            except OSError: pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'SVG_BENCH_API_KEY': 'upstream-secret'}):
        events = []
        proxy = TelemetryProxy(Endpoint(f'http://127.0.0.1:{server.server_port}/v1'), 'chosen', Path(temp), events.append, {'temperature': .25}, maximum)
        url = proxy.start()
        try: yield proxy, url, Path(temp), events, received
        finally:
            if gate: gate.set()
            proxy.close(); server.shutdown(); server.server_close(); thread.join(1)


def post(proxy, url, payload=None, key=None):
    body = json.dumps(payload or {'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
    return request.urlopen(request.Request(url + '/chat/completions', data=body,
        headers={'Authorization': 'Bearer ' + (proxy.key if key is None else key), 'Content-Type': 'application/json'}), timeout=5)


class ProxyTests(unittest.TestCase):
    def test_sections_private_trace_and_missing_metrics(self):
        with setup() as (proxy, url, state, events, received):
            for phase in ('C0', 'C1'):
                proxy.begin_section(phase, 5)
                with post(proxy, url) as response: self.assertEqual(response.status, 200); response.read()
                proxy.end_section()
            self.assertEqual(proxy.serial, 2)
            self.assertEqual([r[0] for r in received], ['Bearer upstream-secret'] * 2)
            self.assertTrue(all(r[1]['model'] == 'chosen' and r[1]['temperature'] == .25 for r in received))
            telemetry = [e for e in events if e['type'] == 'telemetry']
            self.assertEqual([e['checkpoint'] for e in telemetry], ['C0', 'C1'])
            self.assertTrue(all(e['runtime']['status'] == 'unavailable' for e in telemetry))
            self.assertNotIn('delta_arrival_seconds', telemetry[0]['client'])
            self.assertEqual(telemetry[0]['client']['measurement_scope'], 'proxy_to_upstream')
            self.assertNotIn('private', telemetry[0]['usage'])
            self.assertNotIn('raw_snapshots', telemetry[0]['runtime'])
            for path in state.iterdir():
                if os.name == 'posix':
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('upstream-secret', path.read_text())

    def test_true_stream_and_concurrency(self):
        gate = threading.Event()
        parts = [event({'content': 'first'}), event({}, 'stop'), b'data: [DONE]\n\n']
        with setup(parts, gate=gate) as (proxy, url, state, events, received):
            proxy.begin_section('C0', 5)
            with post(proxy, url, {'messages': [], 'stream': True, 'tools': [], 'tool_choice': 'auto'}) as response:
                self.assertEqual(response.readline(), parts[0].splitlines(keepends=True)[0])
                self.assertFalse(gate.is_set())
                boundary = threading.Thread(target=proxy.end_section)
                boundary.start()
                time.sleep(.02)
                self.assertTrue(boundary.is_alive())
                with self.assertRaises(EndpointError): proxy.begin_section('C1', 5)
                with self.assertRaises(error.HTTPError) as caught: post(proxy, url)
                self.assertEqual(caught.exception.code, 409)
                gate.set(); response.read(); boundary.join(2)
                self.assertFalse(boundary.is_alive())
            proxy.end_section()
            self.assertEqual(received[0][1]['stream_options'], {'include_usage': True})
            self.assertEqual(json.loads((state/'request-0001-telemetry.json').read_text())['client']['model_delta_events'], 1)

    def test_auth_phase_budget_routes_and_body(self):
        with setup(maximum=1) as (proxy, url, state, events, received):
            for key, code in [('', 401), (proxy.key, 409)]:
                with self.assertRaises(error.HTTPError) as caught: post(proxy, url, key=key)
                self.assertEqual(caught.exception.code, code)
            with self.assertRaises(error.HTTPError): request.urlopen(url + '/models')
            req = request.Request(url + '/models', headers={'Authorization': 'Bearer ' + proxy.key})
            with request.urlopen(req) as result: self.assertEqual(json.load(result)['data'][0]['id'], 'chosen')
            proxy.begin_section('C0', 5)
            conn = http.client.HTTPConnection(parse.urlsplit(url).netloc)
            conn.request('POST', '/v1/chat/completions', headers={'Authorization': 'Bearer ' + proxy.key, 'Content-Length': str(8*1024*1024+1)})
            self.assertEqual(conn.getresponse().status, 413); conn.close()
            with post(proxy, url) as result: result.read()
            proxy.end_section(); proxy.begin_section('C1', 5)
            with self.assertRaises(error.HTTPError) as caught: post(proxy, url)
            self.assertEqual(caught.exception.code, 429)
            proxy.end_section()

    def test_http_failure_safe_and_partial_stream(self):
        with setup(status=503) as (proxy, url, state, events, received):
            proxy.begin_section('C0', 5)
            with self.assertRaises(error.HTTPError) as caught: post(proxy, url)
            self.assertEqual(caught.exception.code, 503)
            self.assertNotIn(b'upstream-secret', caught.exception.read())
            proxy.end_section()
            self.assertEqual(events[-1]['outcome'], 'transport_failed')
        with setup([event({'content': 'hello'})]) as (proxy, url, state, events, received):
            proxy.begin_section('C0', 5)
            with post(proxy, url, {'messages': [], 'stream': True}) as response: response.read()
            proxy.end_section()
            self.assertEqual(events[-1]['client']['observation_status'], 'partial')
            self.assertEqual(json.loads((state/'request-0001-response.json').read_text())['choices'][0]['message']['content'], 'hello')

    def test_terminal_handoff_waits_for_previous_runtime_finalization(self):
        parts = [event({'content': 'first'}), event({}, 'stop'), b'data: [DONE]\n\n']
        finalized, release = threading.Event(), threading.Event()
        with setup(parts) as (proxy, url, state, events, received):
            original_after = proxy.metrics.after
            def delayed_after(before, response):
                if len(received) == 1:
                    finalized.set()
                    release.wait(2)
                return original_after(before, response)
            with patch.object(proxy.metrics, 'after', side_effect=delayed_after):
                proxy.begin_section('C0', 5)
                first = post(proxy, url, {'messages': [], 'stream': True})
                while first.readline().strip() != b'data: [DONE]': pass
                self.assertTrue(finalized.wait(1))
                result = []
                def continuation():
                    try:
                        with post(proxy, url, {'messages': [], 'stream': True}) as response:
                            result.append(response.status)
                            response.read()
                    except Exception as exc: result.append(exc)
                worker = threading.Thread(target=continuation)
                worker.start(); time.sleep(.05)
                self.assertTrue(worker.is_alive())
                self.assertEqual(len(received), 1)
                release.set(); worker.join(2); first.close()
                self.assertEqual(result, [200])
                proxy.end_section()
                self.assertEqual(len(received), 2)

    def test_endpoint_arrival_precedes_slow_or_failed_downstream_write(self):
        parts = [event({'content': 'first'}), event({}, 'stop'), b'data: [DONE]\n\n']
        with setup(parts) as (proxy, url, state, events, received):
            def slow_write(block): time.sleep(.15)
            response, timing = proxy.endpoint.call_measured('/chat/completions', {'stream': True}, on_data=slow_write)
            self.assertGreater(timing['duration_seconds'] - timing['time_to_first_model_delta_seconds'], .14)
            def failed_write(block):
                time.sleep(.15)
                raise BrokenPipeError()
            with self.assertRaises(EndpointError) as caught:
                proxy.endpoint.call_measured('/chat/completions', {'stream': True}, on_data=failed_write)
            timing = caught.exception.telemetry
            self.assertEqual(timing['model_delta_events'], 1)
            self.assertEqual(timing['observation_status'], 'partial')
            self.assertGreater(timing['duration_seconds'] - timing['time_to_first_model_delta_seconds'], .14)
            self.assertEqual(caught.exception.response['choices'][0]['message']['content'], 'first')

    def test_close_interrupts_open_upstream_stall_and_freezes_artifacts(self):
        gate = threading.Event()
        parts = [event({'content': 'first'}), event({}, 'stop'), b'data: [DONE]\n\n']
        with setup(parts, gate=gate) as (proxy, url, state, events, received):
            proxy.begin_section('C0', 900)
            response = post(proxy, url, {'messages': [], 'stream': True})
            self.assertIn(b'first', response.readline())
            started = time.monotonic(); proxy.close()
            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(proxy._active)
            telemetry = json.loads((state/'request-0001-telemetry.json').read_text())
            self.assertEqual(telemetry['client']['observation_status'], 'partial')
            snapshot = {p.name: p.stat().st_mtime_ns for p in state.iterdir()}
            event_count = len(events)
            gate.set(); time.sleep(.05)
            self.assertEqual(snapshot, {p.name: p.stat().st_mtime_ns for p in state.iterdir()})
            self.assertEqual(len(events), event_count)
            response.close()

    def test_deadline_and_overrides(self):
        with setup() as (proxy, url, state, events, received):
            proxy.begin_section('C0', .01)
            # Wait for the clock used by the deadline, not a sub-tick sleep on Windows.
            deadline = proxy._phase[1]
            while time.monotonic() <= deadline:
                time.sleep(.01)
            with self.assertRaises(error.HTTPError) as caught: post(proxy, url)
            self.assertEqual(caught.exception.code, 409)
            proxy.end_section()
            with self.assertRaises(ValueError): TelemetryProxy(proxy.endpoint, 'x', state, events.append, {'messages': []})
            start = time.monotonic(); proxy.close()
            self.assertLess(time.monotonic() - start, 1)


if __name__ == '__main__': unittest.main()
