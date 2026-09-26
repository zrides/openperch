import copy
import itertools
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
import test_catalog as fixtures

from tracking import TemporalTracker, global_assignment, retrack_result, TRACKER_VERSION
from engine import Engine, Cancelled, VERSION
from sightings import collect_sightings


def detection(x, score=.9, kind='bird', appearance=None, width=.12):
    return {'box': [x-width/2, .3, x+width/2, .5], 'category': kind, 'detection': score,
            'appearance': appearance, 'ranked': [{'label': 'bird', 'similarity': .5}]}


class AssignmentTests(unittest.TestCase):
    def test_global_assignment_avoids_greedy_order_error(self):
        self.assertEqual(set(global_assignment([[.9, .8], [.85, -1]], .4)), {(0, 1), (1, 0)})

    def test_optimal_matches_equal_exhaustive_search_for_small_random_cases(self):
        rng = random.Random(7)
        for rows, columns in itertools.product(range(1, 5), range(1, 5)):
            for _ in range(8):
                scores = [[rng.random() for _ in range(columns)] for _ in range(rows)]
                pairs = global_assignment(scores, .4)
                actual = sum(scores[i][j]-.4 for i, j in pairs)
                expected = 0
                for selection in itertools.product(range(-1, columns), repeat=rows):
                    used = [c for c in selection if c >= 0]
                    if len(used) != len(set(used)):
                        continue
                    if any(scores[i][c] < .4 for i, c in enumerate(selection) if c >= 0):
                        continue
                    expected = max(expected, sum(scores[i][c]-.4 for i, c in enumerate(selection) if c >= 0))
                self.assertAlmostEqual(actual, expected)


class TrackerTests(unittest.TestCase):
    def test_velocity_preserves_fast_bird_without_old_box_overlap(self):
        tracker = TemporalTracker()
        first = tracker.update([detection(.15)], 0)[0]['id']
        self.assertEqual(tracker.update([detection(.20)], 100)[0]['id'], first)
        self.assertEqual(tracker.update([detection(.30)], 300)[0]['id'], first)

    def test_missing_frames_have_no_invented_boxes_and_short_gap_recovers(self):
        tracker = TemporalTracker()
        first = tracker.update([detection(.2, appearance=[1., 0.])], 0)[0]['id']
        tracker.update([detection(.2, appearance=[1., 0.])], 100)
        self.assertEqual(tracker.update([], 300), [])
        recovered = tracker.update([detection(.2, appearance=[1., 0.])], 800)[0]
        self.assertEqual(recovered['id'], first)
        self.assertTrue(recovered['reacquired'])
        self.assertEqual(recovered['box'], detection(.2)['box'])
        self.assertNotEqual(tracker.update([detection(.2)], 2201)[0]['id'], first)

    def test_low_scores_only_continue_confirmed_tracks_and_do_not_live_forever(self):
        tracker = TemporalTracker()
        self.assertEqual(tracker.update([detection(.6, score=.15)], 0), [])
        first = tracker.update([detection(.2)], 100)[0]['id']
        tracker.update([detection(.2)], 200)
        rows = tracker.update([detection(.2, score=.15), detection(.6, score=.15)], 300)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], first)
        self.assertEqual(rows[0]['tracking_state'], 'weak')
        for ms in range(400, 1500, 100):
            tracker.update([detection(.2, score=.15)], ms)
        self.assertEqual(tracker.update([detection(.2, score=.15)], 1500), [])

    def test_other_categories_cannot_inherit_bird_identity(self):
        tracker = TemporalTracker()
        first = tracker.update([detection(.2)], 0)[0]['id']
        second = tracker.update([detection(.2, kind='squirrel')], 100)[0]['id']
        self.assertNotEqual(first, second)

    def test_crossing_distinct_appearances_keep_identities_when_detection_order_flips(self):
        tracker = TemporalTracker()
        ids = {}
        for frame in range(8):
            detections = [detection(.2+.08*frame, appearance=[1., 0.]),
                          detection(.8-.08*frame, appearance=[0., 1.])]
            if frame % 2:
                detections.reverse()
            outputs = tracker.update(detections, frame*100)
            for item in outputs:
                identity = tuple(item['appearance'])
                if frame == 0:
                    ids[identity] = item['id']
                self.assertEqual(ids[identity], item['id'])

    def test_unresolvable_overlap_restarts_instead_of_reusing_arbitrary_identity(self):
        tracker = TemporalTracker()
        old = {x['id'] for x in tracker.update([detection(.3, width=.24), detection(.7, width=.24)], 0)}
        tracker.update([detection(.4, width=.24), detection(.6, width=.24)], 100)
        outputs = tracker.update([detection(.495, width=.24), detection(.505, width=.24)], 200)
        self.assertFalse(old & {x['id'] for x in outputs})
        self.assertGreater(tracker.stats['ambiguous_restarts'], 0)

    def test_one_frame_flash_is_not_a_confirmed_sighting(self):
        tracker = TemporalTracker()
        frames = [{'ms': 0, 'animals': tracker.update([detection(.2)], 0)},
                  {'ms': 100, 'animals': tracker.update([], 100)}]
        tracker.finalize(frames)
        self.assertEqual(collect_sightings(frames), [])

    def test_weak_recovery_does_not_add_species_votes(self):
        tracker = TemporalTracker()
        frames = [{'ms': ms, 'animals': tracker.update([detection(.2, score=score)], ms)}
                  for ms, score in [(0, .9), (100, .9), (1000, .9), (1100, .15)]]
        tracker.finalize(frames)
        group = collect_sightings(frames)[0]
        self.assertEqual(len(group['views']), 2)
        self.assertEqual(group['end_ms'], 1100)

    def test_bad_timestamps_rejected(self):
        tracker = TemporalTracker()
        tracker.update([], 100)
        with self.assertRaises(ValueError):
            tracker.update([], 100)

    def test_retracking_retains_original_results_and_cancels_safely(self):
        original = {'version': 2, 'frames': [{'ms': ms, 'animals': [dict(detection(.2), id=old)]}
                                           for ms, old in [(0, 5), (100, 9), (200, 5)]]}
        snapshot = copy.deepcopy(original)
        upgraded = retrack_result(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(upgraded['tracker_version'], TRACKER_VERSION)
        self.assertEqual([f['animals'][0]['original_id'] for f in upgraded['frames']], [5, 9, 5])
        self.assertEqual(len({f['animals'][0]['id'] for f in upgraded['frames']}), 1)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            retrack_result(original, cancel)
        self.assertEqual(original, snapshot)

    def test_legacy_cache_upgrade_never_loads_ai_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root/'video.mp4'
            video.write_bytes(b'video')
            (root/'open_clip_model.safetensors').write_bytes(b'weights')
            (root/'open_clip_config.json').write_text('{}')
            engine = Engine()
            engine.prepare = Mock(side_effect=AssertionError('No model inference expected'))
            legacy = {'complete': True, 'version': 2, 'frames': [{'ms': 0, 'animals': []}]}
            with patch('engine.read_cache', side_effect=[None, legacy]), \
                 patch('tracking.retrack_result', return_value=copy.deepcopy(legacy)), \
                 patch('engine.write_cache') as write:
                result = engine.analyze(video, root, ['bird', 'squirrel'], lambda _: None, lambda *_: None, threading.Event())
                self.assertEqual(result['version'], VERSION)
                write.assert_called_once()
                engine.prepare.assert_not_called()


class EngineTrackingTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    tearDown = fixtures.CatalogTests.tearDown

    def test_cancelled_fresh_analysis_preserves_previous_ready_gallery(self):
        from bird_library import LibraryJob
        identity = self.db.ingest(self.video)
        self.db.save_analysis(identity, self.result, threading.Event())
        prior = self.db.videos()[0]['result']
        labels = self.root/'species.txt'
        labels.write_text('bird\nsquirrel')
        engine = Mock()
        engine.analyze.side_effect = Cancelled()
        job = LibraryJob('reanalyze', [identity], self.root, labels, self.db.root, engine)
        job.run()
        self.assertTrue(engine.analyze.call_args.kwargs['force'])
        self.assertEqual(self.db.videos()[0]['status'], 'ready')
        self.assertEqual(self.db.videos()[0]['result'], prior)
        self.assertIsNotNone(self.db.result(identity))

    def test_fresh_pipeline_uses_low_boxes_and_refreshes_after_weak_gap(self):
        # Real video decoder and production Engine loop, deterministic detector outputs.
        class Values:
            def __init__(self, values): self.values = values
            def cpu(self): return self
            def int(self): return self
            def tolist(self): return self.values
        class Detector:
            def __init__(self): self.index = 0
            def predict(inner, frame, **options):
                self.assertEqual(options['conf'], .10)
                present = inner.index in (0, 1, 2, 4)
                score = .15 if inner.index == 2 else .9
                inner.index += 1
                return [SimpleNamespace(boxes=SimpleNamespace(
                    xyxy=Values([[6, 5, 35, 40]] if present else []),
                    conf=Values([score] if present else []), cls=Values([0] if present else [])))]
        (self.root/'open_clip_model.safetensors').write_bytes(b'weights')
        (self.root/'open_clip_config.json').write_text('{}')
        engine = Engine(detector_batch_size=1, classification_batch_size=1, decode_queue=0)
        engine.device = 'cpu'
        engine.prepare = Mock()
        engine.detector = Detector()
        classifications = []
        def classify(crops):
            if crops:
                classifications.append(engine.detector.index-1)
            return [[{'label': 'Northern cardinal', 'similarity': .5, 'score': .8}] for _ in crops]
        engine.classify = classify
        with patch('engine.read_cache', side_effect=AssertionError('Fresh analysis must bypass caches')), \
             patch('engine.write_cache'):
            result = engine.analyze(self.video, self.root, ['bird', 'squirrel'], lambda _: None,
                                    lambda *_: None, threading.Event(), force=True)
        self.assertEqual(classifications, [0, 4])
        self.assertEqual(result['frames'][2]['animals'][0]['tracking_state'], 'weak')
        self.assertEqual(result['frames'][3]['animals'], [])
        self.assertEqual(len({a['id'] for f in result['frames'] for a in f['animals']}), 1)
        self.assertEqual(result['tracking_stats']['weak_recoveries'], 1)


if __name__ == '__main__':
    unittest.main()
