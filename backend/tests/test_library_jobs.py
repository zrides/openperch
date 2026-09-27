import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kiwibit_hub import downloader as cloud
from kiwibit_hub.catalog import Catalog
from kiwibit_hub.jobs import LibraryJob
from kiwibit_hub.engine import Cancelled


class FetchTests(unittest.TestCase):
    def test_fetch_orders_newest_first_and_catalogs_verified_files(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Catalog(Path(folder) / 'library')
            video = Path(folder) / 'recording.mp4'
            video.write_bytes(b'fake verified media')
            engine = Mock()
            job = LibraryJob('fetch', [], None, None, db.root, engine)
            client = Mock()
            client.devices.return_value = [{'serialNumber': 'test-camera'}]
            events = [{'startTime': 1}, {'startTime': 9}]
            with patch.object(cloud, 'load_config', return_value={}), \
                 patch.object(cloud, 'KiwibitClient', return_value=client), \
                 patch.object(cloud, 'load_recordings', return_value=(events, 2, 2)), \
                 patch.object(cloud, 'read_manifest', return_value={}), \
                 patch.object(cloud, 'download_one', return_value=('saved', video)) as download:
                self.assertEqual(job.fetch(db), 0)
                self.assertEqual([call.args[5]['startTime'] for call in download.call_args_list], [9, 1])
                self.assertEqual(len(db.videos()), 1)
            db.close()

    def test_fetch_processes_new_recordings_but_not_past_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            labels = root / 'labels.txt'
            labels.write_text('bird\nsquirrel')
            db = Catalog(root / 'library', root)
            ids = {}
            for name in ('new', 'failed', 'done'):
                video = root / f'{name}.mp4'
                video.write_bytes(name.encode())
                ids[name] = db.ingest(video)
            db.state(ids['failed'], 'failed', 'decoder error')
            db.state(ids['done'], 'ready')
            engine = Mock()
            engine.analyze.side_effect = RuntimeError('stop here')
            for action, expected in (('fetch', {'new'}), ('process', {'new', 'failed'})):
                engine.analyze.reset_mock()
                db.state(ids['new'], 'pending')
                db.state(ids['failed'], 'failed', 'decoder error')
                job = LibraryJob(action, [], root, labels, db.root, engine, media_root=root, downloads=root / 'downloads')
                with patch.object(job, 'fetch', return_value=0):
                    job.run()
                analyzed = {Path(call.args[0]).stem for call in engine.analyze.call_args_list}
                self.assertEqual(analyzed, expected, action)
            db.close()

    def test_reanalyze_all_runs_every_recording_fresh_and_says_how_long_is_left(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            labels = root / 'labels.txt'
            labels.write_text('bird\nsquirrel')
            db = Catalog(root / 'library', root)
            for name in ('a', 'b', 'c'):
                video = root / f'{name}.mp4'
                video.write_bytes(name.encode())
                db.ingest(video)
            db.state(db.videos()[0]['id'], 'ready')
            def analyze(video, model, labels, status, progress, cancel, force):
                status('Analyzing')
                raise RuntimeError('stop here')
            engine = Mock()
            engine.analyze.side_effect = analyze
            messages = []
            job = LibraryJob('reanalyze_all', [], root, labels, db.root, engine,
                             lambda kind, **data: messages.append(data['text']) if kind == 'message' else None, media_root=root)
            job.run()
            self.assertEqual(engine.analyze.call_count, 3)
            self.assertTrue(all(call.kwargs['force'] for call in engine.analyze.call_args_list))
            progress = [text for text in messages if text.startswith('Video ')]
            self.assertNotIn('left', progress[0])
            self.assertIn('Video 2/3 · under a minute left', progress[1])
            db.close()

    def test_time_left_reads_naturally(self):
        from kiwibit_hub.jobs import remaining
        self.assertEqual([remaining(20), remaining(600), remaining(125*60)],
                         ['under a minute', 'about 10 min', 'about 2 h 05 min'])

    def test_scan_adds_sd_card_clips_but_only_process_analyzes_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            labels = root / 'labels.txt'
            labels.write_text('bird\nsquirrel')
            (root / 'downloads').mkdir()
            (root / 'downloads' / 'cloud.mp4').write_bytes(b'cloud')
            day = root / 'sdcard' / 'DCIM' / '2026-09-18UTC-04-00'
            day.mkdir(parents=True)
            (day / '2026-09-18T12-52-18PM-04-00.mp4').write_bytes(b'card')
            (day / '2026-09-18T12-52-18PM-04-00.thumbnail.jpg').write_bytes(b'jpg')
            db = Catalog(root / 'library', root)
            engine = Mock()
            engine.analyze.side_effect = RuntimeError('stop here')
            def job(action):
                return LibraryJob(action, [], root, labels, db.root, engine, media_root=root, downloads=root / 'downloads')
            job('scan').run()
            self.assertEqual(sorted(row['path'] for row in db.videos()),
                             ['downloads/cloud.mp4', 'sdcard/DCIM/2026-09-18UTC-04-00/2026-09-18T12-52-18PM-04-00.mp4'])
            engine.analyze.assert_not_called()
            # A scheduled fetch never starts hours of card analysis; "process" is the deliberate start.
            for action, expected in (('fetch', {'cloud'}), ('process', {'cloud', '2026-09-18T12-52-18PM-04-00'})):
                engine.analyze.reset_mock()
                for row in db.videos():
                    db.state(row['id'], 'pending')
                running = job(action)
                with patch.object(running, 'fetch', return_value=0):
                    running.run()
                self.assertEqual({Path(call.args[0]).stem for call in engine.analyze.call_args_list}, expected, action)
            db.close()

    def test_cancelled_queue_keeps_video_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / 'video.mp4'
            video.write_bytes(b'video')
            labels = root / 'labels.txt'
            labels.write_text('bird\nsquirrel')
            db = Catalog(root / 'library')
            db.ingest(video)
            engine = Mock()
            engine.analyze.side_effect = Cancelled()
            job = LibraryJob('process', [], root, labels, db.root, engine)
            job.run()
            self.assertEqual(db.videos()[0]['status'], 'pending')
            self.assertEqual(db.species(), [])
            db.close()


if __name__ == '__main__':
    unittest.main()
