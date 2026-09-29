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

from kiwibit_hub.tracking import TemporalTracker, global_assignment, retrack_result, TRACKER_VERSION
from kiwibit_hub.engine import Engine, Cancelled, VERSION
from kiwibit_hub.sightings import collect_sightings


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

    def test_detector_relabelling_does_not_break_the_track(self):
        # YOLO-World calls the same House finch 'bird', 'sheep' and 'dog' in consecutive frames.
        tracker = TemporalTracker()
        ids = {tracker.update([detection(.2, kind=kind)], ms)[0]['id']
               for ms, kind in [(0, 'bird'), (100, 'sheep'), (200, 'dog'), (300, 'bird')]}
        self.assertEqual(len(ids), 1)

    def test_track_category_follows_the_species_model_over_detector_labels(self):
        def track(kinds, label):
            tracker = TemporalTracker()
            frames = []
            for index, kind in enumerate(kinds):
                animal = dict(detection(.2, kind=kind), ranked=[{'label': label, 'similarity': .5}])
                frames.append({'ms': index*100, 'animals': tracker.update([animal], index*100)})
            tracker.finalize(frames, ['House finch', 'Eastern gray squirrel'])
            return [a for f in frames for a in f['animals']]
        finch = track(['sheep', 'dog', 'sheep', 'bird'], 'House finch')
        self.assertEqual({a['category'] for a in finch}, {'bird'})
        self.assertEqual([a['detector_category'] for a in finch], ['sheep', 'dog', 'sheep', 'bird'])
        squirrel = track(['bird', 'bird', 'bear'], 'Eastern gray squirrel')
        self.assertEqual({a['category'] for a in squirrel}, {'bear'})
        self.assertEqual({a['category'] for a in track(['bird', 'bird'], 'Eastern gray squirrel')}, {'animal'})

    def test_birds_only_species_list_leaves_bird_or_not_to_the_detector(self):
        tracker = TemporalTracker()
        frames = [{'ms': ms, 'animals': tracker.update([detection(.2, kind='bear')], ms)} for ms in (0, 100)]
        tracker.finalize(frames, ['House finch', 'Northern cardinal'])
        self.assertEqual({a['category'] for f in frames for a in f['animals']}, {'bear'})

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

    def split_by_a_miss(self, first, later, gap_ms=500):
        """Detections at 0 ms, then nothing until gap_ms, then three frames: the tracker makes two tracks."""
        tracker = TemporalTracker()
        frames = [{'ms': 0, 'animals': tracker.update(first, 0)}]
        for ms in range(100, gap_ms, 100):
            frames.append({'ms': ms, 'animals': tracker.update([], ms)})
        for ms in range(gap_ms, gap_ms+300, 100):
            frames.append({'ms': ms, 'animals': tracker.update(copy.deepcopy(later), ms)})
        return tracker, frames

    def test_a_brief_miss_is_stitched_into_one_track(self):
        tracker, frames = self.split_by_a_miss([detection(.2)], [detection(.21)])
        self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), 2)
        stats = tracker.finalize(frames)
        animals = [a for f in frames for a in f['animals']]
        self.assertEqual(stats['stitched'], 1)
        self.assertEqual({a['id'] for a in animals}, {animals[0]['id']})
        self.assertNotIn('tracklet', animals[0])
        self.assertTrue(all('tracklet' in a for a in animals[1:]))
        # The one-frame start was never confirmed on its own; joined, it belongs to a confirmed track.
        self.assertTrue(all(a['track_confirmed'] for a in animals))
        self.assertEqual(len(collect_sightings(frames)), 1)

    def test_a_different_species_arriving_is_not_stitched(self):
        finch = dict(detection(.2), ranked=[{'label': 'House finch', 'similarity': .5}])
        titmouse = dict(detection(.21), ranked=[{'label': 'Tufted titmouse', 'similarity': .5}])
        tracker, frames = self.split_by_a_miss([finch], [titmouse])
        tracker.finalize(frames)
        self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), 2)

    def test_look_alikes_that_merely_share_a_guess_are_not_stitched(self):
        # Both pieces list Black-capped chickadee, but each one's best guess is missing from the other.
        nuthatch = dict(detection(.2), ranked=[{'label': 'White-breasted nuthatch', 'similarity': .5},
                                              {'label': 'Black-capped chickadee', 'similarity': .3}])
        chickadee = dict(detection(.21), ranked=[{'label': 'Black-capped chickadee', 'similarity': .5},
                                                {'label': 'Tufted titmouse', 'similarity': .3}])
        tracker, frames = self.split_by_a_miss([nuthatch], [chickadee])
        tracker.finalize(frames)
        self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), 2)

    def test_two_birds_side_by_side_each_keep_their_own_continuation(self):
        tracker, frames = self.split_by_a_miss([detection(.30), detection(.42)], [detection(.31), detection(.41)])
        tracker.finalize(frames)
        def side(animal):
            return 'left' if animal['box'][0] < .3 else 'right'
        tracks = {}
        for frame in frames:
            for animal in frame['animals']:
                tracks.setdefault(animal['id'], set()).add(side(animal))
        self.assertEqual(sorted(map(sorted, tracks.values())), [['left'], ['right']])

    def test_late_or_distant_starts_are_not_stitched(self):
        # Late: past the brief-miss limit and moved off the spot, or past the still-bird limit.
        for first, later, gap in [([detection(.2)], [detection(.28)], 1700), ([detection(.2)], [detection(.21)], 9000),
                                  ([detection(.2)], [detection(.5)], 500)]:
            tracker, frames = self.split_by_a_miss(first, later, gap)
            tracker.finalize(frames)
            self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), 2, (later[0]['box'], gap))

    def test_the_start_level_follows_the_detector_that_made_the_boxes(self):
        from kiwibit_hub.tracking import start_confidence
        self.assertEqual(start_confidence(None), .22)  # saved before detectors were recorded: the small model
        self.assertEqual(start_confidence('yolov8s-worldv2.pt'), .22)
        self.assertEqual(start_confidence('bakeoff/yolov8x-worldv2.pt'), .30)
        self.assertEqual(len(TemporalTracker().update([detection(.2, score=.25)], 0)), 1)
        self.assertEqual(TemporalTracker(.30).update([detection(.2, score=.25)], 0), [])
        # Re-tracking a saved analysis uses its own detector's level.
        frames = [{'ms': ms, 'animals': [dict(detection(.2, score=.25), id=1)]} for ms in range(0, 500, 100)]
        for detector, tracks in [(None, 1), ('yolov8x-worldv2.pt', 0)]:
            result = retrack_result({'frames': copy.deepcopy(frames), 'fps': 10, 'detector': detector, 'tracker_version': 0})
            self.assertEqual(len({a['id'] for f in result['frames'] for a in f['animals']}), tracks, detector)

    def test_a_still_bird_missed_for_seconds_stays_one_sighting(self):
        # A finch sits on the rim while the detector misses it for 4 s: one track and one sighting,
        # but only where it was, and only within the allowed gap.
        for later, gap, tracks in [(detection(.21), 4000, 1), (detection(.28), 4000, 2), (detection(.21), 9000, 2)]:
            tracker, frames = self.split_by_a_miss([detection(.2)], [later], gap)
            tracker.finalize(frames)
            self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), tracks, (later['box'], gap))
            if tracks == 1:  # a one-frame first piece is no sighting of its own
                self.assertEqual(len(collect_sightings(frames)), 1, gap)
        with patch('kiwibit_hub.tracking.STILL_MAX_GAP_MS', 0):
            tracker, frames = self.split_by_a_miss([detection(.2)], [detection(.21)], 4000)
            tracker.finalize(frames)
            self.assertEqual(len({a['id'] for f in frames for a in f['animals']}), 2, 'turned off')

    def test_retracking_a_stitched_result_stitches_it_the_same_way(self):
        tracker, frames = self.split_by_a_miss([detection(.2, appearance=[1., 0.])], [detection(.21, appearance=[1., 0.])])
        result = {'frames': frames, 'tracking_stats': tracker.finalize(frames), 'tracker_version': 1}
        again = retrack_result(result)
        self.assertEqual([[a['id'] for a in f['animals']] for f in again['frames']],
                         [[a['id'] for a in f['animals']] for f in frames])
        self.assertEqual(again['tracking_stats']['stitched'], 1)

    def dropout_result(self, after_x):
        # Five frames at 100 ms, then the camera sends nothing for 5 s, then five more frames.
        finch = [{'label': 'House finch', 'similarity': .5}]
        times = [0, 100, 200, 300, 400, 5400, 5500, 5600, 5700, 5800]
        return {'fps': 10, 'tracker_version': 0, 'frames': [
            {'ms': ms, 'animals': [dict(detection(.2 if ms < 5000 else after_x, appearance=[1., 0.]), id=1, ranked=finch)]}
            for ms in times]}

    def test_a_bird_that_sits_through_a_camera_dropout_is_one_sighting(self):
        result = retrack_result(self.dropout_result(.21))
        self.assertEqual(len({a['id'] for f in result['frames'] for a in f['animals']}), 1)
        # Real times stay for the player; tracking time counts the 5 s hole as one frame.
        self.assertEqual([f['ms'] for f in result['frames']][5], 5400)
        self.assertEqual([f['clock'] for f in result['frames']], [0, 100, 200, 300, 400, 500, 600, 700, 800, 900])
        groups = collect_sightings(result['frames'])
        self.assertEqual([(g['start_ms'], g['end_ms']) for g in groups], [(0, 5800)])

    def test_a_different_spot_after_a_dropout_is_a_new_track(self):
        result = retrack_result(self.dropout_result(.7))
        self.assertEqual(len({a['id'] for f in result['frames'] for a in f['animals']}), 2)
        self.assertEqual(len(collect_sightings(result['frames'])), 2)

    def test_pause_clock_counts_a_dropout_as_one_frame(self):
        from kiwibit_hub.tracking import PauseClock
        clock = PauseClock(10)
        self.assertEqual([clock(ms) for ms in (1000, 1100, 1350, 1700, 6700, 6800)], [1000, 1100, 1350, 1700, 1800, 1900])

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

    def test_retracking_uses_detector_labels_and_skips_decoding_when_colors_are_saved(self):
        # A tracker-v1 result: the finch broke into two tracks when the detector said 'sheep'.
        original = {'tracker_version': 1, 'labels': ['House finch', 'Eastern gray squirrel'], 'frames': [
            {'ms': ms, 'animals': [dict(detection(.2, kind=kind, appearance=[1., 0.]), id=old,
                                        ranked=[{'label': 'House finch', 'similarity': .5}])]}
            for ms, kind, old in [(0, 'bird', 1), (100, 'bird', 1), (200, 'sheep', 2), (300, 'sheep', 2)]]}
        upgraded = retrack_result(original, video=Path('missing-video.mp4'))
        animals = [f['animals'][0] for f in upgraded['frames']]
        self.assertEqual(len({a['id'] for a in animals}), 1)
        self.assertEqual({a['category'] for a in animals}, {'bird'})
        self.assertEqual([a['detector_category'] for a in animals], ['bird', 'bird', 'sheep', 'sheep'])
        # Re-tracking again starts from the detector's labels, not the resolved ones.
        upgraded['tracker_version'] = 1
        again = retrack_result(upgraded)
        self.assertEqual([f['animals'][0]['detector_category'] for f in again['frames']], ['bird', 'bird', 'sheep', 'sheep'])

    def test_cached_analysis_from_an_older_tracker_is_retracked_without_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root/'video.mp4'
            video.write_bytes(b'video')
            (root/'open_clip_model.safetensors').write_bytes(b'weights')
            (root/'open_clip_config.json').write_text('{}')
            engine = Engine()
            engine.prepare = Mock(side_effect=AssertionError('No model inference expected'))
            cached = {'complete': True, 'version': VERSION, 'tracker_version': 1, 'frames': [{'ms': 0, 'animals': []}]}
            with patch('kiwibit_hub.engine.read_cache', return_value=copy.deepcopy(cached)), \
                 patch('kiwibit_hub.engine.write_cache') as write:
                result = engine.analyze(video, root, ['bird', 'squirrel'], lambda _: None, lambda *_: None, threading.Event())
            self.assertEqual(result['tracker_version'], TRACKER_VERSION)
            write.assert_called_once()
            engine.prepare.assert_not_called()

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
            with patch('kiwibit_hub.engine.read_cache', side_effect=[None, legacy]), \
                 patch('kiwibit_hub.tracking.retrack_result', return_value=copy.deepcopy(legacy)), \
                 patch('kiwibit_hub.engine.write_cache') as write:
                result = engine.analyze(video, root, ['bird', 'squirrel'], lambda _: None, lambda *_: None, threading.Event())
                self.assertEqual(result['version'], VERSION)
                write.assert_called_once()
                engine.prepare.assert_not_called()


class EngineTrackingTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    tearDown = fixtures.CatalogTests.tearDown

    def test_cancelled_fresh_analysis_preserves_previous_ready_gallery(self):
        from kiwibit_hub.jobs import LibraryJob
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
        classifications, shapes = [], []
        def classify(crops):
            if crops:
                classifications.append(engine.detector.index-1)
                shapes.extend(crop.shape[:2] for crop in crops)
            return [[{'label': 'Northern cardinal', 'similarity': .5, 'score': .8}] for _ in crops]
        engine.classify = classify
        with patch('kiwibit_hub.engine.read_cache', side_effect=AssertionError('Fresh analysis must bypass caches')), \
             patch('kiwibit_hub.engine.write_cache'):
            result = engine.analyze(self.video, self.root, ['bird', 'squirrel'], lambda _: None,
                                    lambda *_: None, threading.Event(), force=True)
        self.assertEqual(classifications, [0, 4])
        # BioCLIP gets a square with context; 1.5x the 35 px box is capped at the 48 px frame.
        self.assertEqual(shapes, [(48, 48), (48, 48)])
        self.assertEqual(result['frames'][2]['animals'][0]['tracking_state'], 'weak')
        self.assertEqual(result['frames'][3]['animals'], [])
        self.assertEqual(len({a['id'] for f in result['frames'] for a in f['animals']}), 1)
        self.assertEqual(result['tracking_stats']['weak_recoveries'], 1)
        # No dropouts in this video: tracking time is real time.
        self.assertEqual([f['clock'] for f in result['frames']], [f['ms'] for f in result['frames']])


if __name__ == '__main__':
    unittest.main()
