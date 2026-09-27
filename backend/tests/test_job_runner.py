import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kiwibit_hub import downloader as cloud
from kiwibit_hub.catalog import Catalog
from kiwibit_hub.engine import Engine
from kiwibit_hub.jobs import JobRunner, LibraryJob
from kiwibit_hub.video_pipeline import VideoFrames


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.runner = JobRunner(self.root / 'library', self.downloads, self.root)
        self.events = []
        self.runner.subscribe(lambda kind, **data: self.events.append((kind, data)))

    def tearDown(self):
        self.runner.shutdown(5)
        self.temp.cleanup()

    def test_scan_runs_in_background_and_reports_state(self):
        (self.downloads / 'a.mp4').write_bytes(b'video')
        self.runner.start('scan')
        self.runner.thread.join(5)
        states = [data['running'] for kind, data in self.events if kind == 'state']
        self.assertEqual(states, [True, False])
        self.assertFalse(self.runner.running)
        db = Catalog(self.root / 'library', self.root)
        self.assertEqual([row['path'] for row in db.videos()], ['downloads/a.mp4'])
        db.close()

    def test_only_one_job_at_a_time(self):
        release = threading.Event()
        with patch.object(LibraryJob, 'run', lambda job: release.wait(5)):
            self.runner.start('scan')
            with self.assertRaisesRegex(RuntimeError, 'Another library job'):
                self.runner.start('scan')
            with self.assertRaisesRegex(RuntimeError, 'while a job is running'):
                self.runner.configure_batches(8, 8)
            release.set()

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.runner.start('format-disk')

    def test_batch_settings_persist(self):
        self.runner.configure_batches(8, 16)
        again = JobRunner(self.root / 'library', self.downloads, self.root)
        self.assertEqual(again.batch_settings()['detector_batch'], 8)
        self.assertEqual(again.batch_settings()['classifier_batch'], 16)
        with self.assertRaises(ValueError):
            self.runner.configure_batches(3, 8)


class FetchFailureReportingTests(unittest.TestCase):
    def test_failures_are_logged_and_summarized(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            messages = []
            job = LibraryJob('fetch', [], None, None, root / 'library', Mock(),
                             lambda kind, **data: kind == 'message' and messages.append(data['text']),
                             downloads=root / 'downloads')
            client = Mock()
            client.devices.return_value = [{'serialNumber': 'cam'}]
            failure = RuntimeError('ffmpeg exited 1 (Server returned 404); empty partial removed')
            with patch.object(cloud, 'load_config', return_value={}), \
                 patch.object(cloud, 'KiwibitClient', return_value=client), \
                 patch.object(cloud, 'load_recordings', return_value=([{'startTime': 5}], 1, 1)), \
                 patch.object(cloud, 'read_manifest', return_value={}), \
                 patch.object(cloud, 'download_one', side_effect=failure), \
                 patch('shutil.which', return_value='ffmpeg'), \
                 patch('kiwibit_hub.jobs.read_labels', return_value=['a', 'b']), \
                 self.assertLogs('kiwibit_hub.jobs', 'WARNING') as logs:
                job.run()
            self.assertIn('startTime=5', logs.output[0])
            self.assertIn('Server returned 404', logs.output[0])
            self.assertIn('1 failure(s). Last: RuntimeError', messages[-1])


class DecoderRaceTests(unittest.TestCase):
    def test_end_marker_queued_as_producer_exits_is_not_an_error(self):
        frames = VideoFrames('unused.mp4', threading.Event())
        frames.thread = threading.Thread(target=lambda: None)
        frames.thread.start()
        frames.thread.join()
        frames.queue.put(('end', None))
        # The blocking get times out just before the producer's final put lands.
        real_get = frames.queue.get
        def timed_out_once(block=True, timeout=None):
            if block:
                raise queue.Empty
            return real_get(block, timeout)
        with patch.object(frames.queue, 'get', side_effect=timed_out_once):
            self.assertEqual(frames._get(), ('end', None))

    def test_dead_producer_with_empty_queue_is_still_an_error(self):
        frames = VideoFrames('unused.mp4', threading.Event())
        frames.thread = threading.Thread(target=lambda: None)
        frames.thread.start()
        frames.thread.join()
        with self.assertRaisesRegex(RuntimeError, 'without an end marker'):
            frames._get()



class GpuMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'downloads').mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, idle_seconds):
        runner = JobRunner(self.root / 'library', self.root / 'downloads', self.root, idle_seconds=idle_seconds)
        runner.engine = Mock()
        runner.engine.release.return_value = True
        self.addCleanup(runner.shutdown, 5)
        return runner

    @staticmethod
    def eventually(condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            time.sleep(.01)
        return condition()

    def test_models_are_freed_after_the_idle_time(self):
        runner = self.runner(.05)
        runner.start('scan')
        runner.thread.join(5)
        self.assertTrue(self.eventually(lambda: runner.engine.release.called))
        runner.engine.release.assert_called_once()

    def test_a_job_within_the_idle_time_keeps_the_models_loaded(self):
        runner = self.runner(.4)
        for _ in range(2):
            runner.start('scan')
            runner.thread.join(5)
        time.sleep(.2)
        runner.engine.release.assert_not_called()
        self.assertTrue(self.eventually(lambda: runner.engine.release.called))
        runner.engine.release.assert_called_once()

    def test_models_are_never_freed_while_a_job_runs(self):
        runner = self.runner(60)
        runner.state['running'] = True
        runner._release_if_idle()
        runner.engine.release.assert_not_called()

    def test_release_drops_the_models_so_the_next_analysis_reloads_them(self):
        engine = Engine()
        engine.model, engine.detector, engine.model_identity, engine.labels = object(), object(), ('weights',), ['bird']
        torch = Mock()
        torch.cuda.is_available.return_value = True
        with patch.dict(sys.modules, {'torch': torch}):
            self.assertTrue(engine.release())
            self.assertFalse(engine.release())
        torch.cuda.empty_cache.assert_called_once()
        self.assertEqual((engine.model, engine.detector, engine.model_identity, engine.labels), (None, None, None, None))


if __name__ == '__main__':
    unittest.main()
