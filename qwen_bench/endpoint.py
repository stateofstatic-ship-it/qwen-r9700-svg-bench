"""Bounded stdlib OpenAI-compatible transport; never writes authorization headers."""
import json
import os
import time
import math
from urllib import request, error, parse

MAX_RESPONSE = 2 * 1024 * 1024
MAX_STREAM = 64 * 1024 * 1024

class EndpointError(RuntimeError):
    def __init__(self, message, telemetry=None):
        super().__init__(message)
        self.telemetry = telemetry

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise EndpointError('Endpoint redirects are prohibited')

class Endpoint:
    def __init__(self, url, api_key_env='SVG_BENCH_API_KEY'):
        parts = parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise EndpointError('Endpoint must be an http(s) base URL without credentials, query or fragment')
        self.url = url.rstrip('/')
        self.key = os.environ.get(api_key_env, '')
        self.opener = request.build_opener(NoRedirect)

    def call(self, path, payload=None, timeout=20):
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if data is not None and len(data) > 8 * 1024 * 1024:
            raise EndpointError('Endpoint request/history exceeds 8 MiB')
        req = request.Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except error.HTTPError as exc:
            raise EndpointError('Endpoint returned HTTP ' + str(exc.code)) from None
        except (error.URLError, TimeoutError, OSError):
            raise EndpointError('Endpoint connection failed or timed out') from None
        if len(raw) > MAX_RESPONSE:
            raise EndpointError('Endpoint response exceeds 2 MiB')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise EndpointError('Endpoint response is not JSON') from None
        if not isinstance(value, dict) or 'error' in value:
            raise EndpointError('Endpoint returned an invalid object or API error')
        return value

    def call_measured(self, path, payload=None, timeout=20):
        """Measure client arrivals, not engine TTFT or per-token latency.

        No retry is attempted. JSON fallback has no observable delta timings.
        An SSE response must terminate with [DONE], after finished choices.
        """
        started = time.monotonic()
        arrivals = []
        telemetry = {
            'transport': None, 'duration_seconds': 0.0,
            'time_to_first_model_delta_seconds': None,
            'model_delta_span_seconds': None, 'model_delta_events': 0,
            'delta_arrival_seconds': arrivals, 'observation_status': 'unavailable',
            'unavailable_reason': 'request_not_completed',
        }

        def finish():
            telemetry['duration_seconds'] = time.monotonic() - started
            telemetry['model_delta_events'] = len(arrivals)
            if arrivals:
                telemetry['time_to_first_model_delta_seconds'] = arrivals[0]
                telemetry['model_delta_span_seconds'] = arrivals[-1] - arrivals[0]

        def remaining():
            left = timeout - (time.monotonic() - started)
            if left <= 0:
                raise EndpointError('Endpoint overall deadline exceeded')
            return left

        def invalid():
            raise EndpointError('Endpoint returned malformed streaming data')

        try:
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise EndpointError('Endpoint timeout must be finite and positive')
            headers = {'Content-Type': 'application/json'}
            if self.key:
                headers['Authorization'] = 'Bearer ' + self.key
            data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
            if data is not None and len(data) > 8 * 1024 * 1024:
                raise EndpointError('Endpoint request/history exceeds 8 MiB')
            req = request.Request(self.url + path, data=data, headers=headers)
            with self.opener.open(req, timeout=remaining()) as response:
                remaining()
                streaming = response.headers.get_content_type() == 'text/event-stream'
                telemetry['transport'] = 'sse' if streaming else 'json'
                raw = bytearray()
                wire_bytes = 0
                assembled_text_bytes = 0
                pending = bytearray()
                event_lines = []
                assembled = {'choices': []}
                choices = {}
                done = False

                def event():
                    nonlocal done, assembled_text_bytes
                    if not event_lines:
                        return
                    text = '\n'.join(event_lines)
                    event_lines.clear()
                    if done:
                        invalid()
                    if text == '[DONE]':
                        if not choices or any(c['finish_reason'] is None for c in choices.values()):
                            invalid()
                        done = True
                        return
                    chunk = json.loads(text)
                    if not isinstance(chunk, dict) or 'error' in chunk or not isinstance(chunk.get('choices'), list):
                        invalid()
                    for key in ('id', 'object', 'created', 'model', 'system_fingerprint', 'timings'):
                        if key in chunk:
                            assembled[key] = chunk[key]
                    if chunk.get('usage') is not None:
                        if not isinstance(chunk['usage'], dict):
                            invalid()
                        assembled['usage'] = chunk['usage']
                    meaningful = False
                    for item in chunk['choices']:
                        if not isinstance(item, dict):
                            invalid()
                        index = item.get('index')
                        if type(index) is not int or index < 0:
                            invalid()
                        choice = choices.setdefault(index, {'index': index, 'message': {'role': 'assistant', 'content': None}, 'finish_reason': None})
                        delta = item.get('delta')
                        if not isinstance(delta, dict) or choice['finish_reason'] is not None:
                            invalid()
                        message = choice['message']
                        if 'role' in delta:
                            if not isinstance(delta['role'], str):
                                invalid()
                            message['role'] = delta['role']
                        for key in ('content', 'reasoning', 'reasoning_content', 'thinking', 'refusal'):
                            if key in delta:
                                value = delta[key]
                                if value is not None and not isinstance(value, str):
                                    invalid()
                                message.setdefault(key, None)
                                if value is not None:
                                    assembled_text_bytes += len(value.encode('utf-8'))
                                    if assembled_text_bytes > MAX_RESPONSE: raise EndpointError('Assembled endpoint response exceeds 2 MiB')
                                    message[key] = (message[key] or '') + value
                                    meaningful = meaningful or bool(value)
                        if delta.get('tool_calls') is not None:
                            if not isinstance(delta['tool_calls'], list):
                                invalid()
                            calls = message.setdefault('tool_calls', {})
                            for part in delta['tool_calls']:
                                if not isinstance(part, dict) or type(part.get('index')) is not int or part['index'] < 0:
                                    invalid()
                                tool = calls.setdefault(part['index'], {'function': {}})
                                for key in ('id', 'type'):
                                    value = part.get(key)
                                    if value is not None:
                                        if not isinstance(value, str):
                                            invalid()
                                        assembled_text_bytes += len(value.encode('utf-8'))
                                        if assembled_text_bytes > MAX_RESPONSE: raise EndpointError('Assembled endpoint response exceeds 2 MiB')
                                        tool[key] = tool.get(key, '') + value
                                function = part.get('function', {})
                                if not isinstance(function, dict):
                                    invalid()
                                for key in ('name', 'arguments'):
                                    value = function.get(key)
                                    if value is not None:
                                        if not isinstance(value, str):
                                            invalid()
                                        assembled_text_bytes += len(value.encode('utf-8'))
                                        if assembled_text_bytes > MAX_RESPONSE: raise EndpointError('Assembled endpoint response exceeds 2 MiB')
                                        tool['function'][key] = tool['function'].get(key, '') + value
                                        meaningful = meaningful or bool(value)
                        reason = item.get('finish_reason')
                        if reason is not None and not isinstance(reason, str):
                            invalid()
                        choice['finish_reason'] = reason
                    if meaningful:
                        arrivals.append(time.monotonic() - started)

                while True:
                    # read1 returns available bytes without waiting to fill a buffer.
                    # Reset the socket timeout to the remaining total body budget.
                    left = remaining()
                    sock = getattr(getattr(response.fp, 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(left)
                    limit = MAX_STREAM if streaming else MAX_RESPONSE
                    block = response.read1(min(65536, limit + 1 - wire_bytes))
                    remaining()
                    if not block:
                        break
                    wire_bytes += len(block)
                    if wire_bytes > limit:
                        raise EndpointError('Endpoint stream exceeds 64 MiB' if streaming else 'Endpoint response exceeds 2 MiB')
                    if not streaming: raw.extend(block)
                    if streaming:
                        pending.extend(block)
                        while b'\n' in pending:
                            line, _, rest = pending.partition(b'\n')
                            pending[:] = rest
                            line = line.rstrip(b'\r').decode('utf-8')
                            if not line:
                                event()
                            elif line.startswith('data:'):
                                event_lines.append(line[5:].removeprefix(' '))
                        if done:
                            break
                if streaming:
                    if pending or event_lines or not done:
                        raise EndpointError('Endpoint returned a truncated streaming response')
                    for index in sorted(choices):
                        choice = choices[index]
                        calls = choice['message'].get('tool_calls')
                        if calls is not None:
                            choice['message']['tool_calls'] = [calls[i] for i in sorted(calls)]
                        assembled['choices'].append(choice)
                    value = assembled
                    if len(json.dumps(value,ensure_ascii=False).encode('utf-8')) > MAX_RESPONSE:
                        raise EndpointError('Assembled endpoint response exceeds 2 MiB')
                    telemetry['observation_status'] = 'observed' if arrivals else 'unavailable'
                    telemetry['unavailable_reason'] = None if arrivals else 'no_meaningful_model_delta'
                else:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or 'error' in value:
                        raise EndpointError('Endpoint returned an invalid object or API error')
                    telemetry['observation_status'] = 'buffered'
                    telemetry['unavailable_reason'] = 'buffered_json_response'
            finish()
            return value, telemetry
        except Exception as exc:
            finish()
            telemetry['observation_status'] = 'partial' if arrivals else 'unavailable'
            telemetry['unavailable_reason'] = 'request_failed'
            if isinstance(exc, EndpointError):
                message = str(exc)
            elif isinstance(exc, error.HTTPError):
                message = 'Endpoint returned HTTP ' + str(exc.code)
            elif isinstance(exc, (ValueError, UnicodeError, TypeError)):
                message = 'Endpoint returned malformed data or request is invalid'
            else:
                message = 'Endpoint connection failed or timed out'
            raise EndpointError(message, telemetry=telemetry) from None

    def select_model(self, model=None):
        metadata = self.call('/models')
        data = metadata.get('data')
        if not isinstance(data, list) or not data or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in data):
            raise EndpointError('GET /models has no valid model list')
        ids = [x['id'] for x in data]
        if model is None:
            if len(ids) != 1:
                raise EndpointError('Specify model when endpoint lists multiple models')
            model = ids[0]
        if model not in ids:
            raise EndpointError('Requested model is not listed by endpoint')
        return model, metadata


def list_models(endpoint, api_key_env='SVG_BENCH_API_KEY', timeout=5):
    """Metadata-only model IDs; does not test inference/tool-call compatibility."""
    metadata = Endpoint(endpoint, api_key_env).call('/models', timeout=timeout)
    data = metadata.get('data')
    if not isinstance(data, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) or not x['id'] for x in data):
        raise EndpointError('GET /models has no valid model list')
    return [x['id'] for x in data]
