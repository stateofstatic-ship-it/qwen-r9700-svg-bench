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
