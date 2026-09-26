import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_library_jobs import cloud
from bird_library import LibraryJob


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.event = {'serialNumber': 'test-camera', 'startTime': 1}
        self.target = cloud.recording_path(self.root, self.event)
        self.target.parent.mkdir()
        self.target.write_bytes(b'original video')
        self.manifest_path = self.root / 'download_manifest.jsonl'
        record = cloud.manifest_record(self.root, self.target, self.event, 12.5)
        self.name = record['file']
        self.manifest = {self.name: record}
        cloud.append_manifest(self.manifest_path, record)

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, full=False):
        return cloud.verify_existing('ffprobe', self.root, self.manifest_path,
                                     self.manifest, self.target, full=full)

    def test_unchanged_fetch_does_no_hash_probe_or_manifest_write(self):
        with patch.object(cloud, 'file_hash') as digest, patch.object(cloud, 'probe_video') as probe, \
             patch.object(cloud, 'append_manifest') as append, patch.object(cloud, 'MediaProxy') as proxy:
            self.assertEqual(cloud.download_one('ffmpeg', 'ffprobe', self.root, self.manifest_path,
                self.manifest, self.event, 30), ('skipped', self.target))
            digest.assert_not_called()
            probe.assert_not_called()
            append.assert_not_called()
            proxy.assert_not_called()

    def test_legacy_manifest_gets_one_hash_check_then_persistent_fast_skip(self):
        del self.manifest[self.name]['mtime_ns']
        with patch.object(cloud, 'file_hash', wraps=cloud.file_hash) as digest, \
             patch.object(cloud, 'probe_video') as probe:
            self.verify()
            self.manifest = cloud.read_manifest(self.manifest_path)
            self.verify()
            self.assertEqual(digest.call_count, 1)
            probe.assert_not_called()

    def test_changed_timestamp_requires_hash_and_updates_baseline(self):
        stat = self.target.stat()
        os.utime(self.target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2000000000))
        with patch.object(cloud, 'file_hash', wraps=cloud.file_hash) as digest:
            self.verify()
            self.verify()
            self.assertEqual(digest.call_count, 1)

    def test_corrupted_file_is_preserved_and_not_trusted(self):
        prior = dict(self.manifest[self.name])
        self.target.write_bytes(b'corrupt! video')
        os.utime(self.target, ns=(self.target.stat().st_atime_ns, prior['mtime_ns']+2000000000))
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.verify()
        self.assertEqual(self.manifest[self.name], prior)
        self.assertEqual(self.target.read_bytes(), b'corrupt! video')

    def test_full_check_hashes_and_probes_even_unchanged_file(self):
        with patch.object(cloud, 'file_hash', wraps=cloud.file_hash) as digest, \
             patch.object(cloud, 'probe_video', return_value=12.5) as probe:
            self.verify(full=True)
            digest.assert_called_once_with(self.target)
            probe.assert_called_once_with('ffprobe', self.target)

    def test_untracked_file_is_hashed_probed_and_registered(self):
        self.manifest.clear()
        with patch.object(cloud, 'probe_video', return_value=12.5) as probe:
            self.verify()
            probe.assert_called_once()
        self.assertEqual(self.manifest[self.name]['sha256'], cloud.file_hash(self.target))

    def test_file_changed_during_verification_does_not_update_manifest(self):
        prior = dict(self.manifest[self.name])
        def modifying_hash(path):
            digest = prior['sha256']
            path.write_bytes(b'new bytes during verification')
            return digest
        with patch.object(cloud, 'file_hash', side_effect=modifying_hash), \
             patch.object(cloud, 'probe_video', return_value=12.5):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                self.verify(full=True)
        self.assertEqual(self.manifest[self.name], prior)

    def test_full_check_job_does_not_fetch_or_process(self):
        import json
        downloads = self.root / 'kiwibit-downloads'
        downloads.mkdir()
        (downloads / 'local.mp4').write_bytes(b'video')
        engine = Mock()
        job = LibraryJob('verify', [], None, None, self.root / 'library', engine)
        with patch('bird_library.ROOT', self.root), \
             patch.object(cloud, 'KiwibitClient') as client, \
             patch.object(cloud, 'probe_video', return_value=12.5):
            job.run()
            client.assert_not_called()
            engine.analyze.assert_not_called()
        report = json.loads((self.root / 'library/verification-report.json').read_text())
        self.assertTrue(report['complete'])
        self.assertEqual(report['files'][0]['status'], 'verified')


if __name__ == '__main__':
    unittest.main()
