import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from qwen_bench.adapter import CommandAdapter,AdapterError
from qwen_bench.core import KIT,execute,capture


class CleanupTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','POSIX process-group signaling')
    def test_denied_group_cleanup_preserves_original_failure_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config=json.loads((KIT/'examples/fixture.json').read_text())
            config['options']={'fixture_mode':'nonzero'}
            start=time.monotonic()
            with patch('qwen_bench.adapter.os.killpg',side_effect=PermissionError(1,'Operation not permitted')):
                run,status=execute(runs_dir=directory,mode='fixture',config=config,print_fn=lambda _:None)
            self.assertLess(time.monotonic()-start,10)
            self.assertEqual(status['execution'],'failed')
            self.assertIn('Adapter stdout closed',status['error'])
            self.assertTrue(any('Process-group signal' in w for w in status['cleanup_warnings']))
            self.assertEqual(json.loads((run/'status.json').read_text()),status)
            self.assertTrue((run/'report.html').is_file())
            self.assertTrue((run/'checkpoints/C0/result.json').is_file())

    @unittest.skipUnless(os.name=='posix','POSIX process-group signaling')
    def test_denied_group_cleanup_cannot_report_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            config=json.loads((KIT/'examples/fixture.json').read_text())
            with patch('qwen_bench.adapter.os.killpg',side_effect=PermissionError(1,'Operation not permitted')):
                run,status=execute(runs_dir=directory,mode='fixture',config=config,print_fn=lambda _:None)
            self.assertEqual(len(status['sections']),4)
            self.assertEqual(status['execution'],'failed')
            self.assertEqual(status['error'],'Adapter cleanup incomplete')
            self.assertNotEqual(status['trajectory'],'completed')
            self.assertTrue((run/'report.html').is_file())

    def test_unexpected_cleanup_error_cannot_prevent_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config=json.loads((KIT/'examples/fixture.json').read_text())
            config['options']={'fixture_mode':'nonzero'}
            original=CommandAdapter.close
            def close_then_raise(adapter):
                original(adapter)
                raise OSError('cleanup regression fixture')
            with patch.object(CommandAdapter,'close',close_then_raise):
                run,status=execute(runs_dir=directory,mode='fixture',config=config,print_fn=lambda _:None)
            self.assertIn('Adapter stdout closed',status['error'])
            self.assertTrue(any('cleanup regression fixture' in w for w in status['cleanup_warnings']))
            self.assertEqual(json.loads((run/'status.json').read_text()),status)
            self.assertTrue((run/'report.html').is_file())

    def test_nonfinite_deadlines_rejected(self):
        for value in (float('nan'),float('inf'),0,-1):
            with self.assertRaises(ValueError):execute(runs_dir='unused',turn_seconds=value,print_fn=lambda _:None)

    def test_stop_during_capture_does_not_dispatch_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            cancelled=threading.Event();config=json.loads((KIT/'examples/fixture.json').read_text())
            def capture_then_stop(*args):
                result=capture(*args);cancelled.set();return result
            with patch('qwen_bench.core.capture',side_effect=capture_then_stop):
                run,status=execute(runs_dir=directory,mode='fixture',config=config,cancel_event=cancelled,print_fn=lambda _:None)
            self.assertEqual(status['execution'],'interrupted');self.assertEqual(len(status['sections']),1)
            requests=[json.loads(x)['event'] for x in (run/'events.jsonl').read_text().splitlines() if json.loads(x)['origin']=='request']
            self.assertEqual(len([r for r in requests if r['type']=='turn']),1)

    @unittest.skipUnless(os.name=='posix','Escaped POSIX process-group fixture')
    def test_retained_pipe_cannot_pass_shutdown_or_hang_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);(run/'private-adapter-state').mkdir();(run/'raw').mkdir()
            pidfile=run/'escaped.pid'
            child='import os,pathlib,time; pathlib.Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(30)'
            code='import json,subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+'],start_new_session=True); sys.stdin.readline(); print(json.dumps({"type":"run.closed","checkpoint":None}),flush=True)'
            adapter=CommandAdapter([sys.executable,'-c',code],run,lambda *_:None)
            try:
                start=time.monotonic()
                with self.assertRaises(AdapterError):adapter.shutdown()
                adapter.close()
                self.assertLess(time.monotonic()-start,6)
                self.assertTrue(adapter.cleanup_warnings)
            finally:
                if pidfile.exists():
                    try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
                    except ProcessLookupError:pass
                for thread in adapter.threads:thread.join(timeout=2)
                for stream in (adapter.process.stdout,adapter.process.stderr):
                    if not stream.closed:stream.close()


if __name__=='__main__':unittest.main()
