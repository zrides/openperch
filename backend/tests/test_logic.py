import tempfile
import unittest
from pathlib import Path

from kiwibit_hub.engine import animals_at, cache_key, decoded_enough, read_labels


class PlayerLogicTests(unittest.TestCase):
    def test_seeking_uses_matching_frame_and_clears_empty_frames(self):
        frames = [{"animals": ["first"]}, {"animals": []}, {"animals": ["last"]}]
        times = [0, 100, 200]
        self.assertEqual(animals_at(frames, times, 200, 10), ["last"])
        self.assertEqual(animals_at(frames, times, 100, 10), [])
        self.assertEqual(animals_at(frames, times, 0, 10), ["first"])
        self.assertEqual(animals_at(frames, times, 900, 10), [])

    def test_boxes_stay_on_the_frozen_picture_through_a_camera_dropout(self):
        frames = [{"animals": ["before"]}, {"animals": ["frozen"]}, {"animals": ["after"]}]
        times = [0, 100, 4100]
        self.assertEqual(animals_at(frames, times, 3000, 10), ["frozen"])
        self.assertEqual(animals_at(frames, times, 4100, 10), ["after"])
        self.assertEqual(animals_at(frames, times, 4400, 10), [])

    def test_cache_invalidates_when_video_or_species_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            video.write_bytes(b"video one")
            (root / "open_clip_config.json").write_text("{}")
            (root / "open_clip_model.safetensors").write_bytes(b"weights")
            first = cache_key(video, root, ["bird", "squirrel"])
            self.assertNotEqual(first, cache_key(video, root, ["bird", "rabbit"]))
            video.write_bytes(b"a different video")
            second = cache_key(video, root, ["bird", "squirrel"])
            self.assertNotEqual(first, second)
            # Another detector gets its own saved analyses; the default detector's keys never changed.
            from unittest.mock import patch
            with patch("kiwibit_hub.engine.DETECTOR", "bakeoff/yoloe-26l-seg.pt"):
                self.assertNotEqual(second, cache_key(video, root, ["bird", "squirrel"]))
            with patch("kiwibit_hub.engine.DETECTOR", "yolov8s-worldv2.pt"):
                self.assertEqual(second, cache_key(video, root, ["bird", "squirrel"]))

    def test_candidate_file_ignores_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "labels.txt"
            path.write_text("# comment\nbird\n\nbird\nsquirrel\n", encoding="utf-8")
            self.assertEqual(read_labels(path), ["bird", "squirrel"])
            path.write_text("bird\n")
            with self.assertRaises(ValueError):
                read_labels(path)


class CompletenessTests(unittest.TestCase):
    def test_estimated_frame_counts_do_not_fail_complete_videos(self):
        # A 4.3 s live clip at ~14 fps: container estimate 60, decoded 55.
        self.assertTrue(decoded_enough(55, 60, 13.9))
        self.assertTrue(decoded_enough(296, 296, 12.75))
        self.assertTrue(decoded_enough(10, 0, 30), 'unknown frame count')

    def test_truncated_videos_still_fail(self):
        self.assertFalse(decoded_enough(150, 300, 12.75), 'half a recording')
        self.assertFalse(decoded_enough(250, 300, 12.75), 'several seconds missing')
        self.assertFalse(decoded_enough(0, 20, 30))


if __name__ == "__main__":
    unittest.main()
