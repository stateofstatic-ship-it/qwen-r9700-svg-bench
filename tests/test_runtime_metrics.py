import json
import unittest
from unittest.mock import Mock, MagicMock
from qwen_bench.runtime_metrics import RuntimeCollector, parse_snapshot, HISTOGRAMS, llama_timings


def snapshot(after=False):
    values = {'num_requests_running': 0, 'num_requests_waiting': 0,
              'request_success_total': 1 if after else 0,
              'prompt_tokens_total': 10 if after else 0,
              'generation_tokens_total': 5 if after else 0,
              'prompt_tokens_cached_total': 2 if after else 0}
    for metric in HISTOGRAMS.values():
        values[metric + '_count'] = int(after)
        values[metric + '_sum'] = (8 if metric.endswith('computed_tokens') else 2) if after else 0
    return parse_snapshot('\n'.join('vllm:' + k + '{model_name="m",engine="0"} ' + str(v) for k,v in values.items()), 'm')


class RuntimeTests(unittest.TestCase):
    def collect(self, pre=None, post=None, response=None):
        collector = RuntimeCollector(Mock(), 'm')
        collector.before = Mock(return_value=post or snapshot(True))
        return collector.after(pre or snapshot(), response or {'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})

    def mutate(self, name, value):
        post = snapshot(True)
        next(r for r in post['samples'] if r['name'] == name)['value'] = value
        return post

    def test_valid(self):
        result = self.collect()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['prefill_tokens_per_second'], 4)
        self.assertEqual(result['decode_tokens_per_second'], 2)
        self.assertEqual(result['decode_token_count'], 4)
        self.assertIn('conditional', result['attribution'])
        json.dumps(result, allow_nan=False)

    def test_contamination(self):
        for name,value in [('request_success_total',2), ('num_requests_running',1),
                           ('num_requests_waiting',1), ('generation_tokens_total',6),
                           ('request_decode_time_seconds_count',2)]:
            with self.subTest(name=name):
                self.assertEqual(self.collect(post=self.mutate(name,value))['status'], 'unavailable')

    def test_reset(self):
        pre = snapshot()
        pre['samples'][2]['value'] = 2
        self.assertEqual(self.collect(pre=pre)['status'], 'unavailable')

    def test_absent(self):
        post = snapshot(True)
        post['samples'].pop()
        self.assertEqual(self.collect(post=post)['status'], 'unavailable')

    def test_optional_computed_missing(self):
        pre, post = snapshot(), snapshot(True)
        for snap in (pre,post):
            snap['samples'] = [r for r in snap['samples'] if 'computed_tokens' not in r['name']]
        result = self.collect(pre,post)
        self.assertEqual(result['status'], 'available')
        self.assertNotIn('prefill_tokens_per_second',result)

    def test_bad_metrics(self):
        for text in ['vllm:prompt_tokens_total{model_name="m",engine="0"} NaN',
                     'vllm:prompt_tokens_total{model_name="m",engine="0"} +Inf',
                     'vllm:prompt_tokens_total{bad} 1', 'vllm: malformed']:
            with self.assertRaises(ValueError):
                parse_snapshot(text,'m')

    def test_engine_ambiguous(self):
        with self.assertRaises(ValueError):
            parse_snapshot('vllm:num_requests_running{model_name="m",engine="0"} 0\nvllm:num_requests_running{model_name="m",engine="1"} 0','m')

    def test_llama(self):
        result = llama_timings(dict(prompt_n=8,cache_n=2,prompt_ms=2000,predicted_n=5,predicted_ms=2000))
        self.assertEqual(result['prefill_tokens_per_second'],4)
        self.assertEqual(result['decode_tokens_per_second'],2.5)
        self.assertNotIn('ttft_seconds',result)
        self.assertEqual(result['prompt_tokens'],10)
        self.assertEqual(llama_timings({'prompt_n':float('nan')})['status'],'unavailable')

    def test_off_and_failed_probe(self):
        endpoint = MagicMock(url='http://localhost/v1', key='secret')
        endpoint.opener.open.side_effect = OSError('secret')
        result = RuntimeCollector(endpoint,'m').before()
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(RuntimeCollector(endpoint,'m','off').after({},None)['status'],'unavailable')

    def test_probe_path_and_bound(self):
        endpoint = MagicMock(url='https://host/prefix/v1',key='key')
        response = endpoint.opener.open.return_value.__enter__.return_value
        response.read1.side_effect = [b'vllm:num_requests_running{model_name="m",engine="0"} 0',b'']
        self.assertEqual(RuntimeCollector(endpoint,'m').before()['status'],'available')
        args,kwargs = endpoint.opener.open.call_args
        self.assertEqual(args[0].full_url,'https://host/prefix/metrics')
        self.assertEqual(kwargs['timeout'],1)


if __name__ == '__main__':
    unittest.main()
