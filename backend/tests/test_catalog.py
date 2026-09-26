import tempfile
import threading
import unittest
from pathlib import Path

import cv2
import numpy as np

from kiwibit_hub.catalog import Catalog, discover, gallery_species
from kiwibit_hub.engine import Cancelled


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'bird.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        for _ in range(21):
            writer.write(np.full((48, 64, 3), 120, dtype=np.uint8))
        writer.release()
        self.db = Catalog(self.root / 'library')
        animal = {'id': 1, 'category': 'bird', 'box': [.1, .1, .9, .9],
                  'ranked': [{'label': 'Northern cardinal', 'similarity': .4}]}
        self.result = {'complete': True, 'frames': [{'ms': n*100, 'animals': [animal]} for n in range(21)]}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_import_copies_and_deduplicates_without_modifying_source(self):
        original = self.video.read_bytes()
        first = self.db.ingest(self.video, copy=True)
        second = self.db.ingest(self.video, copy=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.db.videos()), 1)
        retained = self.db.video_path(self.db.videos()[0])
        self.assertNotEqual(retained, self.video)
        self.assertEqual(retained.read_bytes(), original)
        self.assertEqual(self.video.read_bytes(), original)

    def test_gallery_is_persistent_timestamped_and_idempotent(self):
        identity = self.db.ingest(self.video)
        for _ in range(2):
            self.db.save_analysis(identity, self.result, threading.Event())
        self.assertEqual(self.db.videos()[0]['status'], 'ready')
        self.assertEqual([p['ms'] for p in self.db.photos('Northern cardinal')], [0, 1000, 2000])
        self.assertEqual(len(self.db.result(identity)['frames']), 21)
        for photo in self.db.photos('Northern cardinal'):
            self.assertTrue((self.db.root / photo['path']).is_file())
        self.db.close()
        self.db = Catalog(self.root / 'library')
        self.assertEqual(self.db.species()[0]['photos'], 3)

    def test_cancellation_and_restart_do_not_publish_partial_gallery(self):
        identity = self.db.ingest(self.video)
        self.db.state(identity, 'processing')
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            self.db.save_analysis(identity, self.result, event)
        self.assertEqual(self.db.species(), [])
        self.db.recover()
        self.assertEqual(self.db.videos()[0]['status'], 'pending')

    def test_modified_source_invalidates_saved_results(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        with self.video.open('ab') as sink:
            sink.write(b'changed')
        with self.assertRaises(ValueError):
            self.db.result(identity)
        self.db.ingest(self.video)
        self.assertIsNone(self.db.result(identity))
        self.assertEqual(self.db.species(), [])

    def result_folders(self):
        return sorted(p.name for p in (self.db.root / 'results').iterdir())

    def test_reprocessing_removes_superseded_result_folder(self):
        identity = self.db.ingest(self.video)
        for _ in range(3):
            self.db.save_analysis(identity, self.result, threading.Event())
        folders = self.result_folders()
        self.assertEqual(len(folders), 1)
        self.assertEqual(Path(self.db.videos()[0]['result']).parent.name, folders[0])

    def test_changed_source_removes_its_stale_results(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        with self.video.open('ab') as sink:
            sink.write(b'changed')
        self.db.ingest(self.video)
        self.assertEqual(self.result_folders(), [])

    def test_failure_before_extraction_leaves_no_folder(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        kept = self.result_folders()
        broken = {'complete': True, 'frames': [{'ms': 0}]}  # missing 'animals'
        with self.assertRaises(KeyError):
            self.db.save_analysis(identity, broken, threading.Event())
        self.assertEqual(self.result_folders(), kept)

    def test_video_paths_are_relative_to_media_root(self):
        self.db.close()
        self.db = Catalog(self.root / 'library', media_root=self.root)
        self.db.ingest(self.video)
        row = self.db.videos()[0]
        self.assertEqual(row['path'], 'bird.avi')
        self.assertEqual(self.db.video_path(row), self.video.resolve())

    def test_partial_and_empty_files_are_excluded(self):
        (self.root / 'clip.part.mp4').write_bytes(b'partial')
        (self.root / 'empty.mp4').touch()
        self.assertEqual(discover(self.root), [self.video])

    def test_nonbirds_are_not_in_gallery(self):
        identity = self.db.ingest(self.video)
        self.result['frames'][0]['animals'][0]['category'] = 'squirrel'
        self.db.save_analysis(identity, self.result, threading.Event())
        self.assertEqual(self.db.species(), [])
        self.assertEqual(self.db.videos()[0]['status'], 'ready')

    def test_conflicting_animal_prediction_is_not_named_as_bird_species(self):
        self.assertEqual(gallery_species('Bison'), 'Uncertain bird (non-bird prediction)')
        self.assertEqual(gallery_species('Northern cardinal'), 'Northern cardinal')


if __name__ == '__main__':
    unittest.main()
