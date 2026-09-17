import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from qwen_bench.endpoint import Endpoint, EndpointError


def event(delta=None, finish=None, usage=None):
    value = {'choices': [] if delta is None else [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return ('data: ' + json.dumps(value) + '\n\n').encode()


@contextmanager
def server(parts, content_type='text/event-stream', status=200, delay=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            try:
                for part in parts:
                    self.wfile.write(part)
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Endpoint('http://127.0.0.1:' + str(httpd.server_port))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class StreamTests(unittest.TestCase):
    def test_reasoning_tools_usage(self):
        parts = [b': heartbeat\n\n', event({'role': 'assistant', 'content': ''}),
                 event({'reasoning': None, 'thinking': 'plan', 'reasoning_content': 'why'}),
                 event({'content': 'ok', 'tool_calls': [
                     {'index': 1, 'id': 'b', 'type': 'function', 'function': {'name': 'second', 'arguments': '{'}},
                     {'index': 0, 'id': 'a', 'type': 'function', 'function': {'name': 'first', 'arguments': '{'}}]}),
                 event({'tool_calls': [
                     {'index': 0, 'type': 'function', 'function': {'arguments': '}'}},
                     {'index': 1, 'function': {'arguments': '}'}}]}),
                 event({}, 'tool_calls'), event(usage={'completion_tokens': 17}), b'data: [DONE]\n\n']
        with server(parts, delay=.01) as endpoint:
            value, measured = endpoint.call_measured('/chat/completions', {'stream': True})
        message = value['choices'][0]['message']
        self.assertEqual(message['reasoning_content'], 'why')
        self.assertEqual(message['tool_calls'][0]['type'], 'function')
        self.assertEqual(message['thinking'], 'plan')
        self.assertIsNone(message['reasoning'])
        self.assertEqual([t['id'] for t in message['tool_calls']], ['a', 'b'])
        self.assertEqual([t['function']['arguments'] for t in message['tool_calls']], ['{}', '{}'])
        self.assertEqual(value['usage']['completion_tokens'], 17)
        self.assertEqual(value['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(measured['model_delta_events'], 3)
        self.assertEqual(measured['transport'], 'sse')
        self.assertEqual(measured['observation_status'], 'observed')
        self.assertGreater(measured['model_delta_span_seconds'], 0)

    def test_json_fallback_and_old_call(self):
        with server([b'{"choices": [], "usage": {}}'], 'application/json') as endpoint:
            value, measured = endpoint.call_measured('/chat/completions', {'stream': True})
            self.assertEqual(endpoint.call('/chat/completions', {}), value)
        self.assertEqual(measured['observation_status'], 'buffered')
        self.assertIsNone(measured['time_to_first_model_delta_seconds'])
        self.assertEqual(measured['delta_arrival_seconds'], [])

    def test_truncation_and_malformed_retain_partial(self):
        for tail in (b'data: invalid\n\n', b'', b'data: [DONE]\n\n'):
            with self.subTest(tail=tail), server([event({'content': 'hello'}), tail]) as endpoint:
                with self.assertRaises(EndpointError) as caught:
                    endpoint.call_measured('/chat/completions', {'stream': True})
                self.assertEqual(caught.exception.telemetry['observation_status'], 'partial')
                self.assertEqual(caught.exception.telemetry['model_delta_events'], 1)

    def test_secret_safe_http_and_api_failure(self):
        with patch.dict(os.environ, {'SVG_BENCH_API_KEY': 'secret-token'}):
            for status, body, mime in [(401, b'secret-token', 'text/plain'),
                                       (200, b'{"error":"secret-token"}', 'application/json'),
                                       (200, b'data: {"error":"secret-token"}\n\n', 'text/event-stream')]:
                with self.subTest(status=status, mime=mime), server([body], mime, status) as endpoint:
                    with self.assertRaises(EndpointError) as caught:
                        endpoint.call_measured('/chat/completions', {'stream': True})
                    self.assertNotIn('secret-token', str(caught.exception))
                    self.assertNotIn('secret-token', json.dumps(caught.exception.telemetry))

    def test_deadline_is_total_not_per_chunk(self):
        with server([event({'content': 'x'})] * 30, delay=.03) as endpoint:
            started = time.monotonic()
            with self.assertRaises(EndpointError) as caught:
                endpoint.call_measured('/chat/completions', {'stream': True}, timeout=.12)
            self.assertLess(time.monotonic() - started, .4)
            self.assertGreater(caught.exception.telemetry['model_delta_events'], 0)

    def test_size_bounds(self):
        with server([b'x' * (2 * 1024 * 1024 + 1)], 'application/json') as endpoint:
            with self.assertRaisesRegex(EndpointError, '2 MiB'):
                endpoint.call_measured('/chat/completions', {})
            with self.assertRaisesRegex(EndpointError, '8 MiB') as caught:
                endpoint.call_measured('/chat/completions', {'x': 'x' * (8 * 1024 * 1024)})
            self.assertEqual(caught.exception.telemetry['model_delta_events'], 0)

    def test_assembled_content_is_bounded_before_done(self):
        with server([event({'content':'x'*(2*1024*1024+1)})]) as endpoint:
            with self.assertRaisesRegex(EndpointError,'Assembled endpoint response exceeds'):
                endpoint.call_measured('/chat/completions',{'stream':True})

    def test_wire_overhead_and_private_arrival_count(self):
        # Tiny generated content can have much larger SSE framing overhead.
        parts=[event({'content':'x'})]*26000+[event({},'stop'),b'data: [DONE]\n\n']
        self.assertGreater(sum(map(len,parts)),2*1024*1024)
        with server(parts) as endpoint:
            value,measured=endpoint.call_measured('/chat/completions',{'stream':True})
        self.assertEqual(len(value['choices'][0]['message']['content']),26000)
        self.assertEqual(len(measured['delta_arrival_seconds']),26000)

    def test_split_utf8_and_lines(self):
        stream = event({'content': 'é'}) + event({}, 'stop') + b'data: [DONE]\n\n'
        with server([stream[i:i + 1] for i in range(len(stream))]) as endpoint:
            result, _ = endpoint.call_measured('/chat/completions', {'stream': True})
        self.assertEqual(result['choices'][0]['message']['content'], 'é')


if __name__ == '__main__':
    unittest.main()
