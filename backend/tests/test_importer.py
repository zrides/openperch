import json
import os
import tempfile
import unittest
from pathlib import Path

from kiwibit_hub.downloader import read_manifest, verify_existing
from kiwibit_hub.importer import import_downloads


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / 'old'
        self.dest = root / 'hub' / 'downloads'
        camera = self.source / 'cam'
        camera.mkdir(parents=True)
        (camera / 'a.mp4').write_bytes(b'video a')
        (camera / 'b.mp4').write_bytes(b'video b')
        (camera / 'b.1234abcd.part.mp4').write_bytes(b'partial')
        (camera / 'empty.mp4').touch()
        (camera / 'notes.txt').write_text('ignore me')
        stat = (camera / 'a.mp4').stat()
        record = {'file': 'cam/a.mp4', 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                  'sha256': 'f' * 64, 'durationSeconds': 2.5}
        (self.source / 'download_manifest.jsonl').write_text(json.dumps(record) + '\n')
        self.before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def run_import(self):
        return import_downloads(self.source, self.dest, self.messages.append)

    def test_imports_finished_videos_and_leaves_source_unchanged(self):
        counts = self.run_import()
        self.assertEqual(counts['linked'] + counts['copied'], 2)
        self.assertEqual(sorted(p.relative_to(self.dest).as_posix() for p in self.dest.rglob('*.mp4')),
                         ['cam/a.mp4', 'cam/b.mp4'])
        self.assertEqual({p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}, self.before)

    def test_hard_links_share_storage_and_timestamps(self):
        self.run_import()
        original, imported = self.source / 'cam' / 'a.mp4', self.dest / 'cam' / 'a.mp4'
        self.assertTrue(os.path.samefile(original, imported))
        self.assertEqual(original.stat().st_mtime_ns, imported.stat().st_mtime_ns)

    def test_manifest_records_come_along_so_fetch_skips_verification(self):
        self.run_import()
        manifest = read_manifest(self.dest / 'download_manifest.jsonl')
        self.assertEqual(list(manifest), ['cam/a.mp4'])
        action, _ = verify_existing('ffprobe-not-needed', self.dest, self.dest / 'download_manifest.jsonl',
                                    manifest, self.dest / 'cam' / 'a.mp4')
        self.assertEqual(action, 'skipped')

    def test_rerun_is_idempotent_and_never_overwrites(self):
        self.run_import()
        (self.dest / 'cam' / 'b.mp4').unlink()
        (self.dest / 'cam' / 'b.mp4').write_bytes(b'different size!')
        counts = self.run_import()
        self.assertEqual(counts, {'linked': 0, 'copied': 0, 'present': 1, 'skipped': 1})
        self.assertEqual((self.dest / 'cam' / 'b.mp4').read_bytes(), b'different size!')
        self.assertEqual(len((self.dest / 'download_manifest.jsonl').read_text().splitlines()), 1)

    def test_refuses_overlapping_folders(self):
        with self.assertRaises(ValueError):
            import_downloads(self.source, self.source / 'nested')


if __name__ == '__main__':
    unittest.main()
