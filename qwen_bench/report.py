"""Offline, script-free local report. Browser previews are not fixed-render checks."""
import base64
import html
import json
from pathlib import Path


def write_report(run):
    run = Path(run)
    status = json.loads((run/'status.json').read_text())
    manifest = json.loads((run/'manifest.json').read_text())
    esc = lambda value: html.escape(str(value), quote=True)
    failure = f'<p role="alert">Run stopped: {esc(status["error"])}</p>' if status.get('error') else ''
    if 'Incomplete generation: length' in str(status.get('error', '')):
        failure += '<p>The model reached its output-token limit. Reasoning may consume that budget before an SVG is written. Preserve this attempt; use a separately labeled higher-budget profile or run to retry.</p>'
    rows, cards = [], []
    for result in status['sections']:
        label = result['checkpoint']
        check = result.get('checks', {})
        rows.append(f'<tr><td>{label}</td><td>{esc(result["execution"])}</td><td>{esc(check.get("status","not_run"))}</td><td>{result["elapsed_seconds"]:.1f}</td></tr>')
        preview = '<p>No safe SVG preview available.</p>'
        source = run/'checkpoints'/label/'scene.svg'
        if source.is_file() and check.get('safe_preview'):
            uri = 'data:image/svg+xml;base64,'+base64.b64encode(source.read_bytes()).decode('ascii')
            w,h = check['expected_dimensions']
            preview = f'<p>Full-size view (scroll horizontally on small screens)</p><div class="scroll"><img alt="{label} full-size SVG" src="{uri}" width="{w}" height="{h}"></div><p>Half-size view</p><div class="scroll"><img alt="{label} half-size SVG" src="{uri}" width="{w//2}" height="{h//2}"></div>'
        cards.append(f'<section><h2>{label}</h2>{preview}<p>Checks: {esc(check.get("status","not_run"))}. <a href="checkpoints/{label}/scene.svg" download>SVG source</a> · <a href="checkpoints/{label}/result.json">section log</a></p></section>')
    performance = '<p>Performance telemetry unavailable for this older run.</p>'
    telemetry_path = run/'telemetry.json'
    if telemetry_path.exists():
        telemetry = json.loads(telemetry_path.read_text(encoding='utf-8'))
        timing_rows = []
        def metric(value, suffix=''):
            if value is None: return 'unavailable'
            return esc(f'{value:,.2f}')+suffix
        def rate(value):
            return metric(value['value'])+f" <small>({value['covered_requests']}/{value['total_requests']})</small>"
        for item in telemetry['sections']:
            first = item['client_first_model_delta_seconds']
            server = item['server_ttft_seconds']
            timing_rows.append('<tr>'+''.join('<td>'+cell+'</td>' for cell in (
                esc(item['checkpoint']+' · '+item['work_type']), str(item['requests']),
                rate(item['completion_tokens']),
                metric(first['mean'])+f" <small>({first['count']}/{item['requests']})</small>",
                metric(server['mean'])+f" <small>({server['count']}/{item['requests']})</small>",
                rate(item['prefill_tokens_per_second']), rate(item['decode_tokens_per_second'])))+'</tr>')
        performance = ('<h2>Performance by task section</h2><div class="scroll"><table><tr>'
            '<th>Section / work</th><th>Recorded requests</th><th>Output tokens</th><th>Client first delta (s)</th>'
            '<th>Server TTFT (s)</th><th>Prefill tok/s</th><th>Decode tok/s</th></tr>'+''.join(timing_rows)+'</table></div>'
            '<p>Parentheses show measured requests / total requests. Latencies are request means; token rates are weighted by measured engine time. '
            'Output includes reasoning when the provider includes it. Missing measurements are not zero.</p>'
            '<p>Client first delta includes transport and possible parser buffering; it is not engine TTFT. '
            'vLLM shared-metric deltas are conditionally attributed only after idle/count/token checks; use a dedicated idle server. '
            'Native harness timings remain unavailable unless the connector reports them.</p>'
            '<p><a href="telemetry.json">Full telemetry and definitions (JSON)</a> · '
            '<a href="telemetry.csv" download>Per-request telemetry (CSV)</a>. '
            'Exports include timing sources, availability, token counts and request/tool classifications. Detailed arrival timings and runtime snapshots are retained in private adapter state.</p>')
    requirement_rows = json.loads((run/'frozen/protocol/REQUIREMENTS.json').read_text())['requirements']
    checklist = ''.join('<li><label><input type="checkbox"> '+esc(row['criterion'])+'</label></li>' for row in requirement_rows)
    document = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'">
<title>Qwen × R9700 SVG test</title><style>body{{font:17px system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;background:#f6f5fa;color:#202034}}h1,h2{{line-height:1.2}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;border-bottom:1px solid #bbb;text-align:left}}section{{background:white;padding:16px;margin-top:24px;border:1px solid #ccc;border-radius:10px}}.scroll{{overflow:auto}}img{{display:block;background:white}}li{{margin:10px 0}}a{{color:#4636a4}}</style></head><body>
<h1>Qwen × R9700 SVG test</h1><p><strong>{esc(status['execution'])}</strong> · protocol {esc(manifest['protocol'])} · {esc(manifest['mode'])} · {esc(manifest['deployment'])}</p>
{failure}
<p>Four-section completion: <strong>{esc(status['trajectory'])}</strong>. Machine checks do not grade artwork, prove tool compliance, or establish a model ranking.</p>
<table><tr><th>Section</th><th>Execution</th><th>Machine checks</th><th>Seconds</th></tr>{''.join(rows)}</table>
{performance}
<p>{esc(status['timing_scope'])} Usage, tool calls and context are unavailable unless the adapter reports them. No hidden reasoning is inferred.</p>
<p>These are your browser's offline SVG image previews, not standardized renderer screenshots or evidence that the candidate inspected pixels. Primary-track controls: <strong>{esc(status['controls'])}</strong>. Keep different protocols, tools and runtimes separate.</p>
<details><summary>Visual review checklist — expand when ready</summary><p>Check full and half size. Use pass / partial / fail / not observed per requirement in <a href="visual-review.json">visual-review.json</a>; these temporary checkboxes are not saved grades.</p><ul>{checklist}</ul></details>
<p><a href="status.json">Run data</a> · <a href="manifest.json">Configuration and hashes</a>. Raw logs and private state may contain sensitive data; this report is not an automatic sanitized sharing bundle.</p>{''.join(cards)}</body></html>'''
    target = run/'report.html'; target.write_text(document, encoding='utf-8')
    review = run/'visual-review.json'
    if not review.exists():
        template = {r['checkpoint']: {row['id']: {'status':'not_observed','evidence':'','confidence':None} for row in requirement_rows} for r in status['sections']}
        review.write_text(json.dumps({'instructions':'Human/independent artifact review; do not infer missing process evidence.', 'sections':template},indent=2)+'\n',encoding='utf-8')
    return target
