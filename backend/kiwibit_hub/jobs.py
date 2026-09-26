"""Library jobs (scan, fetch, process, verify, upgrade) independent of any UI toolkit.

A job reports through a listener callback: listener(kind, **data) where kind is
'message' (text), 'progress' (value, total) or 'changed' (catalog updated).
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .cancel import Cancelled, check_cancel
from .catalog import Catalog, discover
from .engine import Engine, read_labels, SUPPORTED_BATCH_SIZES, DEFAULT_DETECTOR_BATCH, DEFAULT_CLASSIFICATION_BATCH
from .sightings import SMART_VERSION

log = logging.getLogger('kiwibit_hub.jobs')

ACTIONS = ('scan', 'import', 'process', 'selected', 'reanalyze', 'fetch', 'verify', 'upgrade')


class LibraryJob:
    def __init__(self, action, paths, model, labels, root, engine, listener=None, downloads=None, media_root=None):
        if action not in ACTIONS:
            raise ValueError(f'Unknown job action: {action}')
        self.action, self.paths = action, paths
        self.model, self.labels, self.root = model, labels, root
        self.engine = engine
        self.listener = listener or (lambda kind, **data: None)
        self.downloads = Path(downloads) if downloads is not None else config.DOWNLOADS
        self.media_root = media_root
        self.cancel = threading.Event()

    def message(self, text):
        self.listener('message', text=text)

    def progress(self, value, total):
        self.listener('progress', value=value, total=total)

    def changed(self):
        self.listener('changed')

    def catalog(self):
        return Catalog(self.root, self.media_root)

    def run(self):
        log.info('JOB START action=%s selection_count=%s', self.action, len(self.paths))
        db = None
        failures = 0
        try:
            db = self.catalog()
            if self.action == 'upgrade':
                rows = [row for row in db.videos() if row['status'] == 'ready' and row['gallery_version'] < SMART_VERSION]
                for index, row in enumerate(rows):
                    check_cancel(self.cancel)
                    self.message(f"Improving saved results {index+1}/{len(rows)} · {row['name']}")
                    try:
                        result = db.result(row['id'], annotated=False)
                        from .tracking import retrack_result
                        result = retrack_result(result, self.cancel, db.video_path(row), self.message, self.progress)
                        db.save_analysis(row['id'], result, self.cancel)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        failures += 1
                        db.state(row['id'], 'ready', f'Gallery upgrade failed: {exc}'[:600])
                    self.progress(index+1, len(rows))
                    self.changed()
                self.message(f'Saved results improved · {failures} failure(s). No model inference was needed.')
                return
            if self.action == 'verify':
                self.verify_downloads()
                return
            if self.action == 'fetch':
                failures += self.fetch(db)
            elif self.action in ('import', 'scan'):
                paths = self.paths if self.action == 'import' else discover(self.downloads) if self.downloads.is_dir() else []
                for index, path in enumerate(paths):
                    check_cancel(self.cancel)
                    self.message(f"Importing {index+1}/{len(paths)} · {Path(path).name}")
                    try:
                        db.ingest(path, copy=self.action == 'import', cancel=self.cancel)
                    except Cancelled:
                        raise
                    except Exception:
                        failures += 1
                    self.progress(index+1, len(paths))
                self.changed()
            if self.action != 'scan':
                rows = [row for row in db.videos() if (row['id'] in self.paths if self.action in ('selected', 'reanalyze')
                                                       else row['status'] != 'ready')]
                labels = read_labels(self.labels)
                failures += self.process_rows(db, rows, labels)
            self.message(f"Finished · {failures} failure(s)." if failures else "Finished · library saved.")
        except Cancelled:
            self.message("Stopped. Completed videos and photos are saved; pending work can resume.")
        except Exception as exc:
            log.exception('Library job failed: action=%s', self.action)
            self.message(f"{type(exc).__name__}: operation failed. Check camera credentials/connection or local model and video files, then retry.")
        finally:
            if db is not None:
                db.close()
            log.info('JOB END action=%s failures=%s cancelled=%s', self.action, failures, self.cancel.is_set())
            self.changed()

    @staticmethod
    def restore_state(db, row, exc):
        previous = row['status'] == 'ready' and bool(row['result'])
        if isinstance(exc, Cancelled):
            db.state(row['id'], 'ready' if previous else 'pending',
                     'Reanalysis cancelled; previous results kept' if previous else 'Cancelled; ready to resume')
        else:
            log.error('Video operation failed: id=%s', row['id'], exc_info=(type(exc), exc, exc.__traceback__))
            db.state(row['id'], 'ready' if previous else 'failed', f'{type(exc).__name__}: {exc}'[:600])

    def save_completed(self, row, result):
        # This thread owns its connection. Only one completed video is being saved
        # while the single GPU owner analyzes the next video.
        db = self.catalog()
        try:
            db.save_analysis(row['id'], result, self.cancel)
            return 0
        except Exception as exc:
            self.restore_state(db, row, exc)
            return 0 if isinstance(exc, Cancelled) else 1
        finally:
            db.close()
            self.changed()

    def process_rows(self, db, rows, labels):
        failures = 0
        pending = None
        def finish_pending():
            nonlocal pending, failures
            if pending is not None:
                row, future = pending
                pending = None
                try:
                    failures += future.result()
                except Exception as exc:
                    self.restore_state(db, row, exc)
                    failures += 1
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bird-gallery') as saver:
            try:
                for index, row in enumerate(rows):
                    check_cancel(self.cancel)
                    db.state(row['id'], 'processing')
                    self.changed()
                    prefix = f"Video {index+1}/{len(rows)} · {row['name']}"
                    log.info('ANALYZE %s id=%s', prefix, row['id'])
                    try:
                        result = self.engine.analyze(db.video_path(row), self.model, labels,
                            lambda text: self.message(f'{prefix} · {text}'),
                            self.progress, self.cancel, force=self.action == 'reanalyze')
                        finish_pending()
                        check_cancel(self.cancel)
                        self.message(f'{prefix} · Saving photos while the next video is analyzed…')
                        pending = (row, saver.submit(self.save_completed, dict(row), result))
                    except Cancelled as exc:
                        self.restore_state(db, row, exc)
                        raise
                    except Exception as exc:
                        self.restore_state(db, row, exc)
                        failures += 1
                    self.changed()
            finally:
                # Drain on success, failure and cancellation before closing the job.
                finish_pending()
        check_cancel(self.cancel)
        return failures

    def fetch(self, db):
        from .downloader import KiwibitClient, load_config, load_recordings, read_manifest, download_one
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            raise ValueError('FFmpeg and ffprobe are required')
        output = self.downloads
        output.mkdir(parents=True, exist_ok=True)
        manifest_path = output / 'download_manifest.jsonl'
        manifest = read_manifest(manifest_path)
        client = KiwibitClient(load_config())
        failures = 0
        events = []
        self.message('Checking cameras for recordings…')
        for device in client.devices():
            check_cancel(self.cancel)
            def page_progress(done, total):
                check_cancel(self.cancel)
                self.message(f'Checking cloud recordings · {done} events')
            recordings, _, _ = load_recordings(client, device['serialNumber'], page_progress)
            events.extend(recordings)
        events.sort(key=lambda item: item.get('startTime') or 0, reverse=True)
        for index, event in enumerate(events):
            check_cancel(self.cancel)
            if index % 20 == 0:
                self.message(f'Fetching recordings · {index+1}/{len(events)} (unchanged verified files are skipped)…')
            action = None
            try:
                action, target = download_one(ffmpeg, ffprobe, output, manifest_path, manifest, event, 120)
                db.ingest(target)
            except Exception:
                failures += 1
            if action == 'saved' or (index+1) % 20 == 0 or index+1 == len(events):
                self.progress(index+1, len(events))
                self.changed()
        return failures

    def verify_downloads(self):
        """Explicit local integrity check; no cloud access or AI processing."""
        from .downloader import read_manifest, verify_existing
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            raise ValueError('ffprobe is required')
        output = self.downloads.resolve()
        manifest_path = output / 'download_manifest.jsonl'
        manifest = read_manifest(manifest_path)
        names = set(manifest) | {p.relative_to(output).as_posix() for p in discover(output)}
        report = []
        complete = False
        try:
            for index, name in enumerate(sorted(names)):
                check_cancel(self.cancel)
                self.message(f'Full integrity check · {index+1}/{len(names)}…')
                try:
                    target = (output / name).resolve()
                    if not target.is_relative_to(output):
                        raise ValueError('Manifest path is outside downloads')
                    verify_existing(ffprobe, output, manifest_path, manifest, target, full=True)
                    report.append({'file': name, 'status': 'verified'})
                except Exception as exc:
                    report.append({'file': name, 'status': 'failed', 'error': str(exc)})
                self.progress(index+1, len(names))
            complete = True
        finally:
            report_path = Path(self.root) / 'verification-report.json'
            temporary = report_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'complete': complete, 'files': report}, indent=2), encoding='utf-8')
            temporary.replace(report_path)
        failures = sum(item['status'] == 'failed' for item in report)
        self.message(f'Integrity check finished · {len(report)-failures} verified · {failures} failed.')


class JobRunner:
    """Owns the one GPU engine and runs at most one library job at a time."""

    def __init__(self, root=None, downloads=None, media_root=None, model=None, labels=None):
        self.root = Path(root) if root is not None else config.LIBRARY
        self.downloads = downloads
        self.media_root = media_root
        self.model = Path(model) if model is not None else config.BIOCLIP
        self.labels = Path(labels) if labels is not None else config.SPECIES
        self.settings_path = self.root / 'processing-settings.json'
        settings = self._read_settings()
        self.engine = Engine(settings['detector_batch'], settings['classifier_batch'])
        self.listeners = []
        self.lock = threading.Lock()
        self.job = None
        self.thread = None
        self.state = {'action': None, 'running': False, 'message': '', 'value': 0, 'total': 0}

    def _read_settings(self):
        try:
            settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        def batch(key, default):
            value = settings.get(key, default)
            return value if type(value) is int and value in SUPPORTED_BATCH_SIZES else default
        return {'detector_batch': batch('detector_batch', DEFAULT_DETECTOR_BATCH),
                'classifier_batch': batch('classifier_batch', DEFAULT_CLASSIFICATION_BATCH)}

    def batch_settings(self):
        return {'detector_batch': self.engine.detector_batch_size,
                'classifier_batch': self.engine.classification_batch_size,
                'choices': list(SUPPORTED_BATCH_SIZES)}

    def configure_batches(self, detector_batch, classifier_batch):
        if detector_batch not in SUPPORTED_BATCH_SIZES or classifier_batch not in SUPPORTED_BATCH_SIZES:
            raise ValueError(f'Batch sizes must be one of {SUPPORTED_BATCH_SIZES}')
        with self.lock:
            if self.running:
                raise RuntimeError('Batch sizes cannot change while a job is running')
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'detector_batch': detector_batch, 'classifier_batch': classifier_batch}, indent=2),
                                 encoding='utf-8')
            temporary.replace(self.settings_path)
            self.engine.detector_batch_size = detector_batch
            self.engine.classification_batch_size = classifier_batch
            if self.engine.auto_decode_queue:
                self.engine.decode_queue = max(detector_batch, classifier_batch)

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def _publish(self, kind, **data):
        if kind == 'message':
            self.state['message'] = data['text']
        elif kind == 'progress':
            self.state.update(value=data['value'], total=data['total'])
        for listener in list(self.listeners):
            try:
                listener(kind, **data)
            except Exception:
                log.exception('Job listener failed')

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, action, ids=()):
        with self.lock:
            if self.running:
                raise RuntimeError('Another library job is running')
            self.job = LibraryJob(action, list(ids), self.model, self.labels, self.root, self.engine,
                                  self._publish, self.downloads, self.media_root)
            self.state = {'action': action, 'running': True, 'message': 'Starting…', 'value': 0, 'total': 0}
            self._publish('state', **self.state)
            self.thread = threading.Thread(target=self._run, args=(self.job,), name=f'job-{action}', daemon=True)
            self.thread.start()

    def _run(self, job):
        try:
            job.run()
        finally:
            self.state['running'] = False
            self._publish('state', **self.state)

    def cancel(self):
        if self.job is not None:
            log.info('Stop requested')
            self.job.cancel.set()

    def shutdown(self, timeout=None):
        self.cancel()
        if self.thread is not None:
            self.thread.join(timeout)
