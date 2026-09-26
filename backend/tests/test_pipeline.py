import copy
import shutil
import threading
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from kiwibit_hub.engine import Engine, Cancelled
from kiwibit_hub.video_pipeline import VideoFrames
from kiwibit_hub.jobs import LibraryJob
import test_catalog as fixtures


class VideoPipelineTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    tearDown = fixtures.CatalogTests.tearDown

    def test_prefetch_preserves_all_pixels_timestamps_and_final_partial_batch(self):
        with VideoFrames(self.video, threading.Event(), 0) as source:
            reference = list(source)
        with VideoFrames(self.video, threading.Event(), 2) as source:
            batches = list(source.batches(4))
            self.assertEqual(len(batches[-1]), 1)
            with self.assertRaises(StopIteration): next(source)
        actual = [item for batch in batches for item in batch]
        self.assertEqual(len(reference), 21)
        self.assertEqual(len(reference), len(actual))
        for a, b in zip(reference, actual):
            self.assertEqual(a[:2], b[:2])
            np.testing.assert_array_equal(a[2], b[2])
        self.assertFalse(source.thread.is_alive())

    def test_cancel_stops_producer_even_when_queue_is_full(self):
        cancel = threading.Event()
        with VideoFrames(self.video, cancel, 1) as source:
            next(source)
            cancel.set()
            with self.assertRaises(Cancelled): next(source)
        self.assertFalse(source.thread.is_alive())
        self.assertTrue(source.queue.empty())

    def test_consumer_error_drains_and_stops_decoder(self):
        with self.assertRaisesRegex(RuntimeError, 'consumer failed'):
            with VideoFrames(self.video, threading.Event(), 1) as source:
                next(source)
                raise RuntimeError('consumer failed')
        self.assertFalse(source.thread.is_alive())

    def test_producer_error_propagates_and_cleans_up(self):
        with patch.object(VideoFrames, '_read', side_effect=ValueError('decode failed')):
            with self.assertRaisesRegex(ValueError, 'decode failed'):
                with VideoFrames(self.video, threading.Event(), 1) as source:
                    list(source)
        self.assertFalse(source.thread.is_alive())

    def test_photo_extraction_retrieves_each_requested_frame_once_without_seeking(self):
        real_capture = cv2.VideoCapture
        grabs, retrieved = [], []
        class Capture:
            def __init__(inner, path): inner.cap = real_capture(path); inner.index = -1
            def isOpened(inner): return inner.cap.isOpened()
            def release(inner): inner.cap.release()
            def grab(inner):
                inner.index += 1
                grabs.append(inner.index)
                return inner.cap.grab()
            def retrieve(inner):
                retrieved.append(inner.index)
                return inner.cap.retrieve()
        changed = copy.deepcopy(self.result)
        for frame in changed['frames']:
            frame['animals'].append(dict(frame['animals'][0], id=2))
        identity = self.db.ingest(self.video)
        with patch('cv2.VideoCapture', Capture):
            self.db.save_analysis(identity, changed, threading.Event())
        self.assertEqual(grabs, list(range(21)))
        self.assertEqual(retrieved, [0, 10, 20])
        self.assertEqual(len(self.db.photos('Northern cardinal')), 6)

    def test_batched_engine_preserves_causal_classifications_and_track_order(self):
        class Values:
            def __init__(self, values): self.values = values
            def cpu(self): return self
            def int(self): return self
            def tolist(self): return self.values
        class Detector:
            def predict(self, images, **kwargs):
                images = images if isinstance(images, list) else [images]
                return [SimpleNamespace(boxes=SimpleNamespace(xyxy=Values([[6, 5, 35, 40]]),
                    conf=Values([.9]), cls=Values([0]))) for _ in images]
        (self.root/'open_clip_model.safetensors').write_bytes(b'weights')
        (self.root/'open_clip_config.json').write_text('{}')
        results = []
        for detection_batch, crop_batch, queue_size in [(1, 1, 0), (4, 4, 2), (16, 16, 2), (32, 32, 2),
                                                        (4, 16, 16), (4, 32, 32)]:
            engine = Engine(detection_batch, crop_batch, queue_size)
            engine.prepare = Mock()
            engine.device = 'cpu'
            engine.detector = Detector()
            counter = 0
            def classify(crops):
                nonlocal counter
                ranked = [[{'label': f'view-{i}', 'similarity': .5, 'score': .8}]
                          for i in range(counter, counter+len(crops))]
                counter += len(crops)
                return ranked
            engine.classify = classify
            with patch('kiwibit_hub.engine.write_cache'):
                results.append(engine.analyze(self.video, self.root, ['bird', 'squirrel'],
                    lambda _: None, lambda *_: None, threading.Event(), force=True))
        for result in results[1:]:
            self.assertEqual(results[0]['frames'], result['frames'])
        self.assertEqual(results[3]['performance']['largest_detector_batch'], 21)
        self.assertEqual(results[2]['performance']['largest_detector_batch'], 16)
        self.assertEqual(results[5]['performance']['largest_detector_batch'], 4)
        self.assertEqual(results[5]['performance']['largest_classifier_batch'], 5)
        self.assertEqual(results[5]['performance']['classifier_calls'], 1)
        self.assertEqual([f['animals'][0]['ranked'][0]['label'] for f in results[1]['frames']],
                         [f'view-{i//5}' for i in range(21)])

    def test_next_video_analysis_overlaps_gallery_save(self):
        second = self.root/'second.avi'
        shutil.copy2(self.video, second)
        for path in (self.video, second): self.db.ingest(path)
        rows = self.db.videos()
        entered, release = threading.Event(), threading.Event()
        engine = Mock()
        calls = 0
        def analyze(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertTrue(entered.wait(2), 'First gallery stage did not start')
                release.set()
            return copy.deepcopy(self.result)
        engine.analyze.side_effect = analyze
        job = LibraryJob('process', [], self.root, None, self.db.root, engine)
        save = job.save_completed
        def blocked_save(row, result):
            if row['id'] == rows[0]['id']:
                entered.set()
                self.assertTrue(release.wait(2), 'GPU analysis did not overlap gallery work')
            return save(row, result)
        job.save_completed = blocked_save
        self.assertEqual(job.process_rows(self.db, rows, ['bird', 'squirrel']), 0)
        self.assertTrue(all(row['status'] == 'ready' for row in self.db.videos()))

    def test_cancelled_pipeline_restores_both_inflight_videos(self):
        second = self.root/'second.avi'
        shutil.copy2(self.video, second)
        for path in (self.video, second): self.db.ingest(path)
        rows = self.db.videos()
        entered = threading.Event()
        engine = Mock()
        job = LibraryJob('process', [], self.root, None, self.db.root, engine)
        calls = 0
        def analyze(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertTrue(entered.wait(2))
                job.cancel.set()
                raise Cancelled()
            return copy.deepcopy(self.result)
        engine.analyze.side_effect = analyze
        save = job.save_completed
        def blocked_save(row, result):
            entered.set()
            self.assertTrue(job.cancel.wait(2))
            return save(row, result)
        job.save_completed = blocked_save
        with self.assertRaises(Cancelled):
            job.process_rows(self.db, rows, ['bird', 'squirrel'])
        self.assertTrue(all(row['status'] == 'pending' for row in self.db.videos()))
        self.assertFalse(any(t.name.startswith(('bird-photo', 'bird-gallery', 'bird-video-decode')) for t in threading.enumerate()))

    def test_cancel_during_final_gallery_stage_reports_cancelled(self):
        identity = self.db.ingest(self.video)
        engine = Mock()
        engine.analyze.return_value = self.result
        job = LibraryJob('process', [], self.root, None, self.db.root, engine)
        save = job.save_completed
        def cancel_save(row, result):
            job.cancel.set()
            return save(row, result)
        job.save_completed = cancel_save
        with self.assertRaises(Cancelled):
            job.process_rows(self.db, self.db.videos(), ['bird', 'squirrel'])
        self.assertEqual(self.db.videos()[0]['status'], 'pending')

    def test_memory_pressure_reduces_batches_without_dropping_or_reordering(self):
        engine = Engine(4, 4)
        engine.device = 'cuda'
        engine._cuda_oom = lambda exc: isinstance(exc, MemoryError)
        def predict(images, **kwargs):
            batch = images if isinstance(images, list) else [images]
            if len(batch) > 1:
                raise MemoryError()
            return batch
        def classify(crops):
            if len(crops) > 2:
                raise MemoryError()
            return [[item] for item in crops]
        engine.detector = SimpleNamespace(predict=predict)
        engine.classify = classify
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None))
        with patch.dict(sys.modules, {'torch': fake_torch}):
            self.assertEqual(engine.detect_batch(list(range(7)), threading.Event()), list(range(7)))
            self.assertEqual(engine.classify_many(list(range(7)), threading.Event()), [[i] for i in range(7)])
        self.assertEqual(engine.detector_batch_size, 1)
        self.assertEqual(engine.classification_batch_size, 2)

    def test_large_decode_batches_are_not_silently_limited_to_eight(self):
        with VideoFrames(self.video, threading.Event(), 2) as source:
            batches = list(source.batches(16))
        self.assertEqual([len(batch) for batch in batches], [16, 5])
        self.assertEqual([item[0] for batch in batches for item in batch], list(range(21)))

    def test_decode_byte_budget_preserves_frame_order(self):
        frame_bytes = 64*48*3
        with VideoFrames(self.video, threading.Event(), 2) as source:
            batches = list(source.batches(32, max_bytes=frame_bytes*3))
        self.assertTrue(all(len(batch) <= 3 for batch in batches))
        self.assertEqual([item[0] for batch in batches for item in batch], list(range(21)))


if __name__ == '__main__':
    unittest.main()
