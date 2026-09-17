"""Portable request/section telemetry: retain provenance and never invent timings."""
import csv
import json
import math
from pathlib import Path

SCHEMA = 'svg-bench-telemetry/1'
WORK_TYPES = {'C0':'initial_creation', 'C1':'enhancement', 'C2':'enhancement', 'C3':'layout_revision'}
METRICS = ('ttft_seconds', 'prefill_seconds', 'decode_seconds', 'queue_seconds',
           'computed_prefill_tokens', 'cached_prompt_tokens', 'prefill_tokens_per_second',
           'decode_tokens_per_second')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def usage_counts(raw):
    """Bound public counters; full provider payload remains in the private trace."""
    if not isinstance(raw,dict): return {}
    result = {key:number(raw.get(key)) for key in ('prompt_tokens','completion_tokens','total_tokens') if number(raw.get(key)) is not None}
    for detail, fields in (('completion_tokens_details',('reasoning_tokens','accepted_prediction_tokens','rejected_prediction_tokens')),
                           ('prompt_tokens_details',('cached_tokens',))):
        value=raw.get(detail)
        if isinstance(value,dict):
            result[detail]={key:number(value.get(key)) for key in fields if number(value.get(key)) is not None}
    return result


def measured(values):
    values = [v for v in values if number(v) is not None]
    return {'count':len(values), 'mean':sum(values)/len(values) if values else None,
            'min':min(values) if values else None, 'max':max(values) if values else None}


def summarize(requests, wall_seconds):
    count = len(requests)
    def values(group, name):
        return [r.get(group, {}).get(name) for r in requests]
    def total(group, name):
        found = [v for v in values(group, name) if number(v) is not None]
        return {'value':sum(found) if found else None, 'covered_requests':len(found), 'total_requests':count}
    result = {'requests':count, 'wall_seconds':wall_seconds,
              'availability':'reported' if count else 'unavailable',
              'unavailable_reason':None if count else 'Adapter did not report request telemetry; turn wall time is not engine timing.',
              'client_first_model_delta_seconds':measured(values('client', 'time_to_first_model_delta_seconds')),
              'server_ttft_seconds':measured(values('runtime', 'ttft_seconds')),
              'request_duration_seconds':total('client', 'duration_seconds'),
              'prompt_tokens':total('usage', 'prompt_tokens'),
              'completion_tokens':total('usage', 'completion_tokens')}
    result['reasoning_tokens'] = total('token_details', 'reasoning_tokens')
    for key in ('prefill_seconds', 'decode_seconds', 'queue_seconds', 'computed_prefill_tokens', 'cached_prompt_tokens'):
        result[key] = total('runtime', key)
    # Weighted means, not the mean of request rates. Restrict to pairs with valid times.
    for name, tokens, seconds in (('prefill_tokens_per_second','computed_prefill_tokens','prefill_seconds'),
                                 ('decode_tokens_per_second','decode_token_count','decode_seconds')):
        pairs = [(r.get('runtime',{}).get(tokens),r.get('runtime',{}).get(seconds)) for r in requests]
        pairs = [(n,t) for n,t in pairs if number(n) is not None and number(t) is not None and t > 0]
        result[name] = {'value':sum(n for n,t in pairs)/sum(t for n,t in pairs) if pairs else None,
                        'covered_requests':len(pairs), 'total_requests':count}
    return result


def write_telemetry(run):
    """Build exports from the append-only recorder, including interrupted requests."""
    run = Path(run)
    status = json.loads((run/'status.json').read_text(encoding='utf-8'))
    requests = {}
    with (run/'events.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            event = row.get('event', {})
            phase = row.get('phase')
            if row.get('origin') != 'adapter' or phase not in WORK_TYPES:
                continue
            serial = event.get('request')
            if not isinstance(serial, (int, str)) or isinstance(serial, bool):
                continue
            key = (phase, str(serial))
            if event.get('type') == 'adapter.event' and event.get('name') == 'inference.request.started':
                requests[key] = {'checkpoint':phase, 'request':serial, 'work_type':WORK_TYPES[phase],
                                 'trigger':event.get('trigger'), 'outcome':'interrupted_or_missing_telemetry',
                                 'client':{}, 'runtime':{'status':'unavailable','unavailable_reason':'Request did not report terminal telemetry.'}, 'usage':{}}
            elif event.get('type') == 'telemetry' and event.get('schema') == SCHEMA:
                item = {k:event.get(k) for k in ('request','trigger','outcome','client','runtime','usage','tool_names')}
                item.update(checkpoint=phase, work_type=WORK_TYPES[phase])
                for name in ('client','runtime','usage'):
                    if not isinstance(item[name],dict): item[name] = {}
                details = item['usage'].get('completion_tokens_details', {})
                item['token_details'] = {'reasoning_tokens':details.get('reasoning_tokens') if isinstance(details,dict) else None}
                requests[key] = item
    rows = list(requests.values())
    sections = []
    for section in status['sections']:
        label = section['checkpoint']
        selected = [r for r in rows if r['checkpoint'] == label]
        summary = summarize(selected, section['elapsed_seconds'])
        summary.update(checkpoint=label, work_type=WORK_TYPES.get(label,'unknown'))
        sections.append(summary)
        (run/'checkpoints'/label/'telemetry.json').write_text(json.dumps({'schema':SCHEMA,'summary':summary,'requests':selected},indent=2)+'\n',encoding='utf-8')
    document = {'schema':SCHEMA, 'definitions':{
        'client_first_model_delta':'Request start to first nonempty model delta; includes transport, queue, parser buffering; not necessarily engine TTFT.',
        'server_ttft':'Runtime-reported request start to first generated token; see each source/attribution.',
        'prefill_rate':'Computed noncached prefill tokens / corresponding runtime prefill seconds; unavailable without the computed token count.',
        'decode_rate':'Runtime-defined decode tokens / corresponding runtime decode seconds; each request records its numerator definition.',
        'tokens':'Provider token counts, not characters/chunks; reasoning is a subset of completion tokens. Prompt totals include history replay, not peak context.',
        'coverage':'Rates use only valid paired measurements; partial coverage must not be compared as complete coverage.',
        'work_type':'Protocol section classification, not inferred hidden reasoning. Tool names and request trigger label finer request activity.',
        'missing':'Null/unavailable means not measured, never zero. Native/custom harnesses must report the same telemetry event contract.'},
        'sections':sections,'requests':rows}
    (run/'telemetry.json').write_text(json.dumps(document,indent=2)+'\n',encoding='utf-8')
    fields = ['checkpoint','work_type','request','trigger','outcome','tool_names','client_transport','client_measurement_scope','client_duration_seconds',
              'client_time_to_first_model_delta_seconds','runtime_status','runtime_source','runtime_unavailable_reason',
              *METRICS,'prompt_tokens','completion_tokens','reasoning_tokens']
    with (run/'telemetry.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for row in rows:
            flat = {key:row.get(key) for key in fields[:6]}
            flat['tool_names'] = ','.join(str(x) for x in (row.get('tool_names') or []))
            flat.update({f'client_{key}':row['client'].get(key) for key in ('transport','measurement_scope','duration_seconds','time_to_first_model_delta_seconds')})
            flat.update({f'runtime_{key}':row['runtime'].get(key) for key in ('status','source','unavailable_reason')})
            flat.update({key:row['runtime'].get(key) for key in METRICS})
            flat.update({key:row['usage'].get(key) for key in ('prompt_tokens','completion_tokens')})
            flat['reasoning_tokens'] = row.get('token_details',{}).get('reasoning_tokens')
            # Avoid spreadsheet formulas from custom-adapter labels when exporting CSV.
            writer.writerow({k:("'"+v if isinstance(v,str) and v and v[:1] in '=+-@\t\r' else v) for k,v in flat.items()})
    return document
