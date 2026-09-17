import json
from pathlib import Path
import tempfile
import unittest
from qwen_bench.telemetry import summarize, write_telemetry, SCHEMA


class TelemetryTests(unittest.TestCase):
    def test_weighted_rates_and_coverage(self):
        rows = [{'runtime':{'decode_token_count':90,'decode_seconds':1,'computed_prefill_tokens':100,'prefill_seconds':2},'client':{'time_to_first_model_delta_seconds':3},'usage':{'completion_tokens':91}},
                {'runtime':{'decode_token_count':90,'decode_seconds':3},'client':{'time_to_first_model_delta_seconds':1},'usage':{'completion_tokens':91}},
                {'runtime':{},'client':{},'usage':{}}]
        result=summarize(rows,10)
        self.assertEqual(result['decode_tokens_per_second'],{'value':45,'covered_requests':2,'total_requests':3})
        self.assertEqual(result['prefill_tokens_per_second']['covered_requests'],1)
        self.assertEqual(result['client_first_model_delta_seconds']['mean'],2)
        self.assertIsNone(result['server_ttft_seconds']['mean'])
        self.assertIsNone(summarize([],1)['completion_tokens']['value'])

    def test_failed_request_preserved_and_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            run=Path(folder);(run/'checkpoints/C0').mkdir(parents=True)
            (run/'status.json').write_text(json.dumps({'sections':[{'checkpoint':'C0','elapsed_seconds':5}]}))
            events=[{'type':'adapter.event','name':'inference.request.started','request':1,'trigger':'section_prompt'},
                    {'type':'telemetry','schema':SCHEMA,'request':1,'outcome':'length','usage':{'completion_tokens':10,'completion_tokens_details':{'reasoning_tokens':8}},'client':{'duration_seconds':2},'runtime':{},'tool_names':['=bad']},
                    {'type':'adapter.event','name':'inference.request.started','request':2,'trigger':'tool_continuation'}]
            (run/'events.jsonl').write_text(''.join(json.dumps({'origin':'adapter','phase':'C0','event':e})+'\n' for e in events))
            result=write_telemetry(run)
            self.assertEqual(result['sections'][0]['requests'],2)
            self.assertEqual(result['sections'][0]['reasoning_tokens']['value'],8)
            self.assertEqual(result['requests'][1]['outcome'],'interrupted_or_missing_telemetry')
            self.assertIn("'=bad",(run/'telemetry.csv').read_text())
            self.assertTrue((run/'checkpoints/C0/telemetry.json').exists())

    def test_nonfinite_values_never_aggregate(self):
        result=summarize([{'runtime':{'ttft_seconds':float('nan'),'decode_seconds':0,'decode_token_count':100},'usage':{'completion_tokens':-1}}],1)
        self.assertEqual(result['server_ttft_seconds']['count'],0)
        self.assertIsNone(result['completion_tokens']['value'])
        self.assertIsNone(result['decode_tokens_per_second']['value'])
