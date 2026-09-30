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
        self.assertEqual([p['ms'] for p in self.db.photos('Northern cardinal')], [2000, 1000, 0])
        self.assertEqual(len(self.db.result(identity)['frames']), 21)
        for photo in self.db.photos('Northern cardinal'):
            self.assertTrue((self.db.root / photo['path']).is_file())
        self.db.close()
        self.db = Catalog(self.root / 'library')
        self.assertEqual(self.db.species()[0]['photos'], 3)

    def test_gallery_is_newest_first_before_pagination(self):
        names = ['20260925T120000Z_old.avi', '2026-09-26T01-00-00PM-04-00.avi',
                 '20260926T160000Z_middle.avi', 'unknown.avi']
        for index, name in enumerate(names):
            video = self.root / name
            video.write_bytes(self.video.read_bytes())
            identity = self.db.ingest(video)
            self.db.save_analysis(identity, self.result, threading.Event())
            # Quality must not outrank the recording date.
            self.db.db.execute('UPDATE photos SET quality=?, featured=1 WHERE video_id=?',
                               (100 - index, identity))
        expected = [(name, ms) for name in [names[1], names[2], names[0], names[3]]
                    for ms in [2000, 1000, 0]]
        for best_only in (False, True):
            rows = [row for offset in range(0, 12, 2)
                    for row in self.db.photos('Northern cardinal', offset, 2, best_only=best_only)]
            self.assertEqual([(row['name'], row['ms']) for row in rows], expected)

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

    def test_origin_is_recorded_when_a_recording_joins(self):
        self.db.close()
        self.db = Catalog(self.root / 'library', media_root=self.root)
        downloads = self.root / 'downloads' / 'camera'
        downloads.mkdir(parents=True)
        cloud = downloads / '20260919T033000Z_0123456789abcdef.mp4'
        cloud.write_bytes(b'cloud')
        (self.root / 'downloads' / 'download_manifest.jsonl').write_text(
            '{"file": "camera/20260919T033000Z_0123456789abcdef.mp4", "durationSeconds": 10}\n')
        card = self.root / 'sdcard' / '2026-09-18T11-30-00PM-04-00.mp4'
        card.parent.mkdir()
        card.write_bytes(b'card')
        live = self.root / 'downloads' / 'live' / 'clip.mp4'
        live.parent.mkdir()
        live.write_bytes(b'live')
        ids = {name: self.db.ingest(path) for name, path in
               [('cloud', cloud), ('card', card), ('live', live), ('other', self.video)]}
        ids['copy'] = self.db.ingest(self.video, copy=True)
        origins = {row['id']: row['origin'] for row in self.db.videos()}
        self.assertEqual({name: origins[identity] for name, identity in ids.items()},
                         {'cloud': 'cloud', 'card': 'sdcard', 'live': 'live', 'other': 'unknown', 'copy': 'unknown'})
        # A caller that knows better settles an unknown origin; a known one stays.
        self.db.ingest(self.video, origin='live')
        self.db.ingest(card, origin='cloud')
        origins = {row['id']: row['origin'] for row in self.db.videos()}
        self.assertEqual((origins[ids['other']], origins[ids['card']]), ('live', 'sdcard'))

    def test_a_changed_card_clip_is_measured_again(self):
        identity = self.db.ingest(self.video)
        self.db.set_media(identity, 2.1, None)
        self.db.ingest(self.video)
        self.assertEqual(self.db.videos()[0]['duration'], 2.1, 'unchanged file keeps its length')
        with self.video.open('ab') as stream:
            stream.write(b'more')
        self.db.ingest(self.video)
        self.assertIsNone(self.db.videos()[0]['duration'])

    def test_existing_catalog_learns_each_recordings_origin(self):
        import sqlite3
        folder = self.root / 'old'
        (folder / 'library').mkdir(parents=True)
        (folder / 'downloads').mkdir()
        (folder / 'downloads' / 'download_manifest.jsonl').write_text('{"file": "cam/a.mp4"}\n')
        with sqlite3.connect(folder / 'library' / 'library.sqlite3') as old:
            old.execute('''CREATE TABLE videos (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, modified INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '', result TEXT, added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                gallery_version INTEGER NOT NULL DEFAULT 0, animals TEXT)''')
            old.executemany('INSERT INTO videos(id,path,name,size,modified) VALUES(?,?,?,1,1)',
                            [('1', 'downloads/cam/a.mp4', 'a.mp4'), ('2', 'sdcard/DCIM/b.mp4', 'b.mp4'),
                             ('3', 'downloads/live/c.mp4', 'c.mp4'), ('4', 'downloads/d.mp4', 'd.mp4')])
        old.close()
        db = Catalog(folder / 'library', folder)
        try:
            self.assertEqual({row['id']: (row['origin'], row['duration'], row['always_show']) for row in db.videos()},
                             {'1': ('cloud', None, 0), '2': ('sdcard', None, 0),
                              '3': ('live', None, 0), '4': ('unknown', None, 0)})
        finally:
            db.close()

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

    def squirrel_result(self, views):
        squirrel = {'id': 2, 'category': 'bear', 'box': [.1, .1, .9, .9],
                    'ranked': [{'label': 'Eastern gray squirrel', 'similarity': .5}]}
        for frame in self.result['frames'][:views]:
            frame['animals'].append(dict(squirrel))
        return self.result

    def test_squirrels_tag_the_recording(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.squirrel_result(3), threading.Event())
        self.assertEqual(self.db.video_animals(), {identity: ['Squirrel']})
        self.assertEqual(self.db.species()[0]['species'], 'Northern cardinal')

    def test_a_brief_squirrel_box_tags_nothing(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.squirrel_result(2), threading.Event())
        self.assertEqual(self.db.video_animals(), {})

    def test_bird_sighting_read_as_a_squirrel_tags_the_recording(self):
        identity = self.db.ingest(self.video)
        for frame in self.result['frames']:
            frame['animals'][0]['ranked'] = [{'label': 'Eastern gray squirrel', 'similarity': .5}]
        self.db.save_analysis(identity, self.result, threading.Event())
        self.assertEqual(self.db.sightings(identity)[0]['ai_species'], 'Uncertain bird')
        self.assertEqual(self.db.video_animals(), {identity: ['Squirrel']})
        self.db.correct_sighting(self.db.sightings(identity)[0]['id'], 'Northern cardinal')
        self.assertEqual(self.db.video_animals(), {})

    def test_analyses_saved_before_tagging_are_tagged_on_upgrade(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.squirrel_result(3), threading.Event())
        self.db.db.execute('ALTER TABLE videos DROP COLUMN animals')
        self.db.close()
        self.db = Catalog(self.root / 'library')
        self.assertEqual(self.db.video_animals(), {identity: ['Squirrel']})

    def test_conflicting_animal_prediction_is_not_named_as_bird_species(self):
        self.assertEqual(gallery_species('Bison'), 'Uncertain bird (non-bird prediction)')
        self.assertEqual(gallery_species('Northern cardinal'), 'Northern cardinal')


if __name__ == '__main__':
    unittest.main()
