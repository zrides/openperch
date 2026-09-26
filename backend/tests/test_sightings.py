import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch, Mock

import cv2
import numpy as np

from catalog import Catalog, NONBIRD_LABELS
from engine import Cancelled
from sightings import collect_sightings, summarize, photo_quality, best_photos, sighting_matches, UNCERTAIN
import test_catalog as fixtures


def view(label='House finch', alternate='Purple finch', ms=0, track=1, box=None):
    return {'ms': ms, 'quality': 80, 'animal': {'id': track, 'category': 'bird', 'detection': .9,
            'box': box or [.1, .1, .4, .5],
            'ranked': [{'label': label, 'similarity': .6}, {'label': alternate, 'similarity': .3}]}}


class SightingLogicTests(unittest.TestCase):
    def test_single_outlier_does_not_flip_consistent_identification(self):
        views = [view(ms=i*1000) for i in range(5)]
        views[2] = view('Purple finch', 'House finch', ms=2000)
        evidence = summarize(views, NONBIRD_LABELS)
        self.assertEqual(evidence['species'], 'House finch')
        self.assertEqual(evidence['support'], 4)

    def test_uncertain_when_sparse_conflicting_or_close(self):
        self.assertEqual(summarize([view()], NONBIRD_LABELS)['species'], UNCERTAIN)
        conflicting = [view(), view(), view('Purple finch', 'House finch'), view('Purple finch', 'House finch')]
        self.assertEqual(summarize(conflicting, NONBIRD_LABELS)['species'], UNCERTAIN)
        close = [view(ms=i*1000) for i in range(4)]
        for v in close:
            v['animal']['ranked'][1]['similarity'] = .59
        self.assertEqual(summarize(close, NONBIRD_LABELS)['species'], UNCERTAIN)

    def test_nonbird_consensus_remains_uncertain(self):
        evidence = summarize([view('Bison', ms=i*1000) for i in range(5)], NONBIRD_LABELS)
        self.assertEqual(evidence['species'], UNCERTAIN)

    def test_one_sharp_outlier_cannot_overrule_majority_of_views(self):
        views = [view(), view('Purple finch', 'House finch'), view('Purple finch', 'House finch')]
        views[0]['quality'] = 100
        for item in views[1:]:
            item['quality'] = 0
            item['animal']['detection'] = .1
        self.assertEqual(summarize(views, NONBIRD_LABELS)['species'], UNCERTAIN)

    def test_frames_do_not_count_as_independent_votes_and_tracks_remain_separate(self):
        frames = [{'ms': i*100, 'animals': [view(track=1)['animal'], view(track=2)['animal']]} for i in range(21)]
        groups = collect_sightings(frames)
        self.assertEqual(len(groups), 2)
        self.assertEqual([len(g['views']) for g in groups], [3, 3])
        frames.append({'ms': 5000, 'animals': [view(track=1)['animal']]})
        self.assertEqual(len(collect_sightings(frames)), 3)

    def test_quality_favors_sharp_larger_unclipped_crops(self):
        rng = np.random.default_rng(4)
        sharp = rng.integers(0, 256, (128, 128, 3), dtype=np.uint8)
        blurred = cv2.GaussianBlur(sharp, (21, 21), 8)
        box = [.2, .2, .8, .8]
        self.assertGreater(photo_quality(sharp, box)[0], photo_quality(blurred, box)[0])
        self.assertGreater(photo_quality(sharp, box)[0], photo_quality(sharp, [0, 0, .03, .03])[0])

    def test_duplicates_collapse_to_best_but_original_list_is_preserved(self):
        views = [{'ms': 0, 'path': 'poor', 'quality': 10, 'fingerprint': '0'},
                 {'ms': 1000, 'path': 'best', 'quality': 90, 'fingerprint': '1'},
                 {'ms': 2000, 'path': 'different', 'quality': 70, 'fingerprint': 'ffffffffffffffff'}]
        self.assertEqual(best_photos(views), {'best', 'different'})
        self.assertEqual(len(views), 3)

    def test_matching_uses_position_not_track_number(self):
        group = {'start_ms': 0, 'end_ms': 2000, 'views': [view(ms=i*1000, track=88) for i in range(3)]}
        old = [{'id': 'left', 'start_ms': 0, 'end_ms': 2000,
                'anchors': [{'ms': i*1000, 'box': [.1, .1, .4, .5]} for i in range(3)]},
               {'id': 'right', 'start_ms': 0, 'end_ms': 2000,
                'anchors': [{'ms': i*1000, 'box': [.6, .1, .9, .5]} for i in range(3)]}]
        self.assertEqual(sighting_matches([group], old), {0: 'left'})
        self.assertEqual(sighting_matches([group, copy.deepcopy(group)], old), {})


class CorrectionTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    tearDown = fixtures.CatalogTests.tearDown
    def test_correction_survives_reprocess_track_renumbering_and_restart(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        sighting = self.db.photos('Northern cardinal')[0]['sighting_id']
        self.db.correct_sighting(sighting, 'Purple finch')
        for frame in self.result['frames']:
            frame['animals'][0]['id'] = 99
        self.db.save_analysis(identity, self.result, threading.Event())
        self.db.close()
        self.db = Catalog(self.root / 'library')
        photos = self.db.photos('Purple finch')
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(p['sighting_id'] == sighting for p in photos))
        result = self.db.result(identity)
        self.assertEqual(result['frames'][0]['animals'][0]['display_label'], 'Purple finch')
        self.assertEqual(result['frames'][0]['animals'][0]['ranked'][0]['label'], 'Northern cardinal')
        self.assertNotIn('display_label', self.db.result(identity, annotated=False)['frames'][0]['animals'][0])
        self.db.correct_sighting(sighting, None)
        self.assertEqual(len(self.db.photos('Northern cardinal')), 3)

    def test_ambiguous_reprocessing_preserves_unmatched_correction(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        sighting = self.db.photos('Northern cardinal')[0]['sighting_id']
        self.db.correct_sighting(sighting, 'Purple finch')
        # Two overlapping new tracks cannot safely inherit the old correction.
        changed = copy.deepcopy(self.result)
        for frame in changed['frames']:
            second = copy.deepcopy(frame['animals'][0])
            second['id'] = 2
            frame['animals'].append(second)
        self.db.save_analysis(identity, changed, threading.Event())
        self.assertEqual(self.db.photos('Purple finch'), [])
        self.assertEqual(self.db.unmatched_corrections()[0]['label'], 'Purple finch')

    def test_best_view_hides_duplicates_all_photos_keeps_them(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        self.assertEqual(len(self.db.photos('Northern cardinal')), 3)
        self.assertEqual(len(self.db.photos('Northern cardinal', best_only=True)), 1)

    def test_cancel_mid_extraction_preserves_previous_gallery_and_correction(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        original = self.db.photos('Northern cardinal')
        self.db.correct_sighting(original[0]['sighting_id'], 'Purple finch')
        cancel = threading.Event()
        from catalog import photo_quality as real_quality
        def stop_after_crop(*args):
            cancel.set()
            return real_quality(*args)
        with patch('catalog.photo_quality', side_effect=stop_after_crop):
            with self.assertRaises(Cancelled):
                self.db.save_analysis(identity, self.result, cancel)
        self.assertEqual([p['path'] for p in self.db.photos('Purple finch')], [p['path'] for p in original])

    def test_corrections_do_not_follow_replaced_source(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        sighting = self.db.photos('Northern cardinal')[0]['sighting_id']
        self.db.correct_sighting(sighting, 'Purple finch')
        with self.video.open('ab') as sink:
            sink.write(b'changed-source')
        self.db.ingest(self.video)
        self.assertEqual(self.db.unmatched_corrections()[0]['label'], 'Purple finch')
        self.assertEqual(self.db.photos('Purple finch'), [])

    def test_correcting_one_bird_does_not_relabel_other_bird_in_same_frames(self):
        changed = copy.deepcopy(self.result)
        for frame in changed['frames']:
            left = copy.deepcopy(frame['animals'][0])
            left['box'] = [.05, .1, .45, .9]
            right = copy.deepcopy(left)
            right.update(id=2, box=[.55, .1, .95, .9])
            frame['animals'] = [left, right]
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, changed, threading.Event())
        first = self.db.photos('Northern cardinal')[0]['sighting_id']
        self.db.correct_sighting(first, 'Purple finch')
        self.assertEqual(len(self.db.photos('Purple finch')), 3)
        self.assertEqual(len(self.db.photos('Northern cardinal')), 3)

    def test_correction_made_during_extraction_wins_at_publish(self):
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        first = self.db.photos('Northern cardinal')[0]['sighting_id']
        from catalog import photo_quality as real_quality
        def edit_while_processing(*args):
            # The crop scorer now runs in a worker. Simulate a concurrent editor
            # with its own thread-local SQLite connection, as the UI uses.
            other = Catalog(self.root / 'library')
            try:
                other.correct_sighting(first, 'Purple finch')
            finally:
                other.close()
            return real_quality(*args)
        with patch('catalog.photo_quality', side_effect=edit_while_processing):
            self.db.save_analysis(identity, self.result, threading.Event())
        self.assertEqual(len(self.db.photos('Purple finch')), 3)

    def test_saved_results_upgrade_does_not_run_models(self):
        from bird_library import LibraryJob
        from sightings import SMART_VERSION
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        self.db.db.execute('UPDATE videos SET gallery_version=0 WHERE id=?', (identity,))
        self.db.db.commit()
        engine = Mock()
        job = LibraryJob('upgrade', [], None, None, self.db.root, engine)
        job.run()
        engine.analyze.assert_not_called()
        self.assertEqual(self.db.videos()[0]['gallery_version'], SMART_VERSION)
        self.assertEqual(self.db.videos()[0]['status'], 'ready')


class MigrationTests(unittest.TestCase):
    def test_old_catalog_is_backed_up_and_photos_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'library.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''CREATE TABLE videos(id TEXT PRIMARY KEY,path TEXT UNIQUE,name TEXT,size INTEGER,
                    modified INTEGER,status TEXT DEFAULT 'pending',error TEXT DEFAULT '',result TEXT,added TEXT);
                    CREATE TABLE photos(video_id TEXT,species TEXT,ms REAL,track INTEGER,similarity REAL,path TEXT);
                    INSERT INTO photos VALUES('v','Bird',0,1,.4,'existing.jpg');''')
            catalog = Catalog(folder)
            self.assertEqual(catalog.db.execute('SELECT path FROM photos').fetchone()[0], 'existing.jpg')
            self.assertTrue((Path(folder)/'library-before-smart-v1.sqlite3').is_file())
            catalog.close()


if __name__ == '__main__':
    unittest.main()
