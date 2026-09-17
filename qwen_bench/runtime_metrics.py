"""Best-effort runtime telemetry, never client elapsed time masquerading as phases.

Semantics: vLLM v1/metrics/stats.py (first/last token timestamps), and
https://github.com/vllm-project/vllm/blob/main/docs/design/metrics.md
llama.cpp: https://github.com/ggml-org/llama.cpp/tree/master/tools/server#timings-and-context-usage
Shared counters permit only conditional attribution, never proof of exclusivity.
"""
import json
import math
import re
import time
from urllib import parse, request

MAX_BYTES = 2 * 1024 * 1024
HISTOGRAMS = {
    'ttft_seconds': 'time_to_first_token_seconds',
    'prefill_seconds': 'request_prefill_time_seconds',
    'decode_seconds': 'request_decode_time_seconds',
    'queue_seconds': 'request_queue_time_seconds',
    'computed_prefill_tokens': 'request_prefill_kv_computed_tokens',
}
COUNTERS = ('prompt_tokens_total', 'generation_tokens_total', 'request_success_total', 'prompt_tokens_cached_total')
GAUGES = ('num_requests_running', 'num_requests_waiting')
SELECTED = set(COUNTERS + GAUGES) | {v + s for v in HISTOGRAMS.values() for s in ('_sum', '_count')}
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z_0-9]*)="((?:[^"\\]|\\[\\"n])*)"(?:,|$)')
SAMPLE = re.compile(r'^vllm:([a-zA-Z_][a-zA-Z_0-9]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$')


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def parse_snapshot(body, model):
    rows = []
    engines = set()
    for line in body.splitlines():
        if not line or line.startswith('#'):
            continue
        match = SAMPLE.fullmatch(line)
        if not match:
            if line.startswith('vllm:'):
                raise ValueError('malformed metrics')
            continue
        name, label_text, raw = match.groups()
        if name not in SELECTED:
            continue
        labels = {}
        pos = 0
        for label in LABEL.finditer(label_text or ''):
            if label.start() != pos or label[1] in labels:
                raise ValueError('malformed labels')
            labels[label[1]] = json.loads('"' + label[2] + '"')
            pos = label.end()
        if pos != len(label_text or ''):
            raise ValueError('malformed labels')
        if labels.get('model_name') != model:
            continue
        if set(labels) - {'model_name', 'engine', 'finished_reason'} or ('finished_reason' in labels and name != 'request_success_total'):
            raise ValueError('ambiguous metric labels')
        engine = labels.get('engine')
        if engine is None:
            raise ValueError('missing engine identity')
        engines.add(engine)
        value = float(raw)
        if not finite(value):
            raise ValueError('nonfinite or negative metric')
        rows.append({'name': name, 'labels': labels, 'value': value})
    if len(engines) != 1:
        raise ValueError('missing or ambiguous model/engine metrics')
    identities = [(r['name'], tuple(sorted(r['labels'].items()))) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate metric series')
    return {'status': 'available', 'model': model, 'engine': next(iter(engines)), 'samples': rows}


class RuntimeCollector:
    def __init__(self, endpoint, model, runtime='auto'):
        if runtime not in ('auto', 'vllm', 'off'):
            raise ValueError('runtime must be auto, vllm, or off')
        self.endpoint, self.model, self.runtime = endpoint, model, runtime

    def before(self):
        if self.runtime == 'off':
            return {'status': 'unavailable', 'unavailable_reason': 'runtime collection disabled'}
        parts = parse.urlsplit(self.endpoint.url)
        if not parts.path.rstrip('/').endswith('/v1'):
            return {'status': 'unavailable', 'unavailable_reason': 'metrics base path not recognized'}
        path = parts.path.rstrip('/')[:-3] + '/metrics'
        url = parse.urlunsplit((parts.scheme, parts.netloc, path, '', ''))
        headers = {'Accept': 'text/plain'}
        if self.endpoint.key:
            headers['Authorization'] = 'Bearer ' + self.endpoint.key
        start = time.monotonic()
        try:
            with self.endpoint.opener.open(request.Request(url, headers=headers), timeout=1) as response:
                # read1 prevents a trickling peer from extending a single read indefinitely.
                chunks, size = [], 0
                while True:
                    if time.monotonic() - start >= 1:
                        raise TimeoutError()
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(max(0.001, 1 - (time.monotonic() - start)))
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('oversized metrics')
            return parse_snapshot(b''.join(chunks).decode('utf-8'), self.model)
        except Exception:
            # Exception text may contain credentials or response content.
            return {'status': 'unavailable', 'unavailable_reason': 'metrics inaccessible, oversized, malformed, or ambiguous'}

    def after(self, before_snapshot, response):
        if self.runtime == 'off':
            return {'status': 'unavailable', 'unavailable_reason': 'runtime collection disabled', 'source': None}
        if isinstance(response, dict) and isinstance(response.get('timings'), dict):
            return llama_timings(response['timings'])
        after = self.before()
        result = {'status': 'unavailable', 'source': 'server_shared_metrics_delta',
                  'raw_snapshots': {'before': before_snapshot, 'after': after}}
        try:
            measured = delta_metrics(before_snapshot, after, response)
        except (ValueError, KeyError, TypeError):
            result['unavailable_reason'] = 'cannot attribute metrics: missing data, concurrency, usage mismatch, or counter reset'
            return result
        result.update(measured)
        result.update(status='available', attribution='conditional: idle boundaries and matching single completion; perfectly overlapping unrelated work cannot be ruled out')
        return result


def delta_metrics(before, after, response):
    if before['status'] != 'available' or after['status'] != 'available' or (before['model'], before['engine']) != (after['model'], after['engine']):
        raise ValueError()
    def mapping(snapshot):
        return {(r['name'], tuple(sorted(r['labels'].items()))): r['value'] for r in snapshot['samples']}
    pre, post = mapping(before), mapping(after)
    if pre.keys() != post.keys():
        raise ValueError()
    totals, delta = {}, {}
    for key in pre:
        name = key[0]
        a, b = pre[key], post[key]
        if not finite(a) or not finite(b) or b < a:
            raise ValueError()
        if name in GAUGES and (a != 0 or b != 0):
            raise ValueError()
        totals[name] = totals.get(name, 0) + b
        delta[name] = delta.get(name, 0) + b - a
    if not all(x in totals for x in GAUGES) or delta['request_success_total'] != 1:
        raise ValueError()
    usage = response['usage']
    p, g = usage['prompt_tokens'], usage['completion_tokens']
    if not finite(p) or not finite(g) or int(p) != p or int(g) != g or delta['prompt_tokens_total'] != p or delta['generation_tokens_total'] != g:
        raise ValueError()
    out = {'prompt_tokens': p, 'generated_tokens': g}
    for field, metric in HISTOGRAMS.items():
        if metric + '_count' not in delta and field == 'computed_prefill_tokens':
            continue
        if delta[metric + '_count'] != 1:
            raise ValueError()
        out[field] = delta[metric + '_sum']
    computed = out.get('computed_prefill_tokens')
    cached = delta.get('prompt_tokens_cached_total')
    if cached is not None:
        if cached > p or int(cached) != cached:
            raise ValueError()
        out['cached_prompt_tokens'] = cached
    if computed is not None:
        if int(computed) != computed or computed > p or (cached is not None and computed + cached != p):
            raise ValueError()
        if out['prefill_seconds'] > 0:
            out['prefill_tokens_per_second'] = computed / out['prefill_seconds']
    if g > 1 and out['decode_seconds'] > 0:
        out['decode_token_count'] = g - 1
        out['decode_tokens_per_second'] = (g - 1) / out['decode_seconds']
    out['definitions'] = {'prefill_tokens_per_second': 'new KV tokens computed / prefill seconds; excludes cached prompt tokens',
                          'decode_tokens_per_second': '(generated_tokens - 1) / (last output token timestamp - first output token timestamp)',
                          'source': 'vLLM v1/metrics/stats.py; docs/design/metrics.md'}
    return out


def llama_timings(timings):
    out = {'status': 'unavailable', 'source': 'llama_cpp_response_timings', 'unavailable_reason': 'missing or invalid response timings'}
    keys = ('prompt_n', 'prompt_ms', 'predicted_n', 'predicted_ms')
    if not all(finite(timings.get(k)) for k in keys):
        return out
    if any(int(timings[k]) != timings[k] for k in ('prompt_n', 'predicted_n')):
        return out
    out.pop('unavailable_reason')
    out.update(status='available', attribution='per_response', computed_prefill_tokens=timings['prompt_n'], generated_tokens=timings['predicted_n'],
               prefill_seconds=timings['prompt_ms'] / 1000, decode_seconds=timings['predicted_ms'] / 1000)
    if finite(timings.get('cache_n')) and int(timings['cache_n']) == timings['cache_n']:
        out.update(cached_prompt_tokens=timings['cache_n'], prompt_tokens=timings['prompt_n'] + timings['cache_n'])
    if timings['prompt_ms'] > 0:
        out['prefill_tokens_per_second'] = timings['prompt_n'] * 1000 / timings['prompt_ms']
    if timings['predicted_ms'] > 0:
        out['decode_token_count'] = timings['predicted_n']
        out['decode_tokens_per_second'] = timings['predicted_n'] * 1000 / timings['predicted_ms']
    out['definitions'] = {'prefill_tokens_per_second': 'response timings.prompt_n / (prompt_ms / 1000); processed prompt tokens only',
                          'decode_tokens_per_second': 'response timings.predicted_n / (predicted_ms / 1000); llama.cpp predicted-token rate, not vLLM first-to-last-token rate',
                          'source': 'https://github.com/ggml-org/llama.cpp/tree/master/tools/server#timings-and-context-usage',
                          'ttft_seconds': 'unavailable: llama.cpp prompt_ms is not TTFT'}
    return out
