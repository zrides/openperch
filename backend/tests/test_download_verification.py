import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_library_jobs import cloud
from kiwibit_hub.jobs import LibraryJob


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
        downloads = self.root / 'downloads'
        downloads.mkdir()
        (downloads / 'local.mp4').write_bytes(b'video')
        engine = Mock()
        job = LibraryJob('verify', [], None, None, self.root / 'library', engine, downloads=downloads)
        with patch.object(cloud, 'KiwibitClient') as client, \
             patch.object(cloud, 'probe_video', return_value=12.5):
            job.run()
            client.assert_not_called()
            engine.analyze.assert_not_called()
        report = json.loads((self.root / 'library/verification-report.json').read_text())
        self.assertTrue(report['complete'])
        self.assertEqual(report['files'][0]['status'], 'verified')


class RemuxTests(unittest.TestCase):
    """The camera sometimes advertises an AAC track with sample rate 0."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.event = {'serialNumber': 'test-camera', 'startTime': 1, 'videoUrl': 'https://cloud.example/v.m3u8'}
        self.manifest_path = self.root / 'download_manifest.jsonl'
        self.manifest = {}
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    def fake_ffmpeg(self, outcomes):
        """Each call writes the output file (or not) and returns the next scripted result."""
        outcomes = iter(outcomes)
        def run(command, **_):
            self.commands.append(command)
            code, stderr, output = next(outcomes)
            if output is not None:
                Path(command[-1]).write_bytes(output)
            return Mock(returncode=code, stderr=stderr)
        return run

    def download(self, outcomes):
        with patch.object(cloud.subprocess, 'run', side_effect=self.fake_ffmpeg(outcomes)), \
             patch.object(cloud, 'MediaProxy') as proxy, \
             patch.object(cloud, 'probe_video', return_value=2.1):
            proxy.return_value.url_for.return_value = 'http://127.0.0.1/media/x'
            return cloud.download_one('ffmpeg', 'ffprobe', self.root, self.manifest_path,
                                      self.manifest, self.event, 30)

    def partials(self):
        return sorted(p.name for p in self.root.rglob('*.part.mp4'))

    def test_empty_audio_track_retries_video_only(self):
        empty_audio = b'[mp4 @ 0x1] sample rate not set\nCould not write header (incorrect codec parameters ?)'
        action, target = self.download([(1, empty_audio, b''), (0, b'', b'video bytes')])
        self.assertEqual(action, 'saved')
        self.assertEqual(target.read_bytes(), b'video bytes')
        self.assertIn('0:a:0?', self.commands[0])
        self.assertIn('-an', self.commands[1])
        self.assertNotIn('0:a:0?', self.commands[1])
        self.assertTrue(self.manifest[target.relative_to(self.root).as_posix()]['audioDropped'])
        self.assertEqual(self.partials(), [], 'the empty first attempt must not be left behind')

    def test_normal_download_keeps_audio_and_does_not_flag(self):
        action, target = self.download([(0, b'', b'video+audio')])
        self.assertEqual(len(self.commands), 1)
        self.assertNotIn('audioDropped', self.manifest[target.relative_to(self.root).as_posix()])

    def test_unrelated_failure_is_not_retried_and_empty_partial_removed(self):
        with self.assertRaisesRegex(RuntimeError, 'empty partial removed'):
            self.download([(1, b'Server returned 403 Forbidden', b'')])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.partials(), [])

    def test_failed_partial_with_bytes_is_kept_for_inspection(self):
        with self.assertRaisesRegex(RuntimeError, 'partial kept at'):
            self.download([(1, b'Connection reset', b'half a video')])
        self.assertEqual(len(self.partials()), 1)


if __name__ == '__main__':
    unittest.main()
