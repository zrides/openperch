import tempfile
import unittest
from pathlib import Path

from engine import animals_at, cache_key, read_labels


class PlayerLogicTests(unittest.TestCase):
    def test_seeking_uses_matching_frame_and_clears_empty_frames(self):
        frames = [{"animals": ["first"]}, {"animals": []}, {"animals": ["last"]}]
        times = [0, 100, 200]
        self.assertEqual(animals_at(frames, times, 200, 10), ["last"])
        self.assertEqual(animals_at(frames, times, 100, 10), [])
        self.assertEqual(animals_at(frames, times, 0, 10), ["first"])
        self.assertEqual(animals_at(frames, times, 900, 10), [])

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
            self.assertNotEqual(first, cache_key(video, root, ["bird", "squirrel"]))

    def test_candidate_file_ignores_comments_and_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "labels.txt"
            path.write_text("# comment\nbird\n\nbird\nsquirrel\n", encoding="utf-8")
            self.assertEqual(read_labels(path), ["bird", "squirrel"])
            path.write_text("bird\n")
            with self.assertRaises(ValueError):
                read_labels(path)


if __name__ == "__main__":
    unittest.main()
