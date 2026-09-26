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
