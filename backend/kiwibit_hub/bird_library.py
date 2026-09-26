"""Kiwibit desktop library: ingest, identify, browse species, and fetch on demand."""
from __future__ import annotations

import shutil
import json
import sys
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

diagnostics = None
if __name__ == '__main__':
    from diagnostics import install
    diagnostics = install()
log = logging.getLogger('bird-library')

from PySide6.QtCore import Qt, QThread, Signal, QSize, QUrl, QTimer
from PySide6.QtGui import QIcon, QDesktopServices
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QListWidget, QListWidgetItem, QSplitter, QFileDialog, QLineEdit,
    QProgressBar, QMessageBox, QCheckBox, QInputDialog, QComboBox)

from catalog import Catalog, ROOT, discover
from engine import (Engine, Cancelled, check_cancel, read_labels, SUPPORTED_BATCH_SIZES,
                    DEFAULT_DETECTOR_BATCH, DEFAULT_CLASSIFICATION_BATCH)
from player import Window, clock
from sightings import SMART_VERSION, UNCERTAIN


class LibraryJob(QThread):
    message = Signal(str)
    changed = Signal()
    progress = Signal(int, int)

    def __init__(self, action, paths, model, labels, root, engine):
        super().__init__()
        self.action, self.paths = action, paths
        self.model, self.labels, self.root = model, labels, root
        self.engine = engine
        self.cancel = threading.Event()

    def run(self):
        log.info('JOB START action=%s selection_count=%s', self.action, len(self.paths))
        db = None
        failures = 0
        try:
            db = Catalog(self.root)
            if self.action == 'upgrade':
                rows = [row for row in db.videos() if row['status'] == 'ready' and row['gallery_version'] < SMART_VERSION]
                for index, row in enumerate(rows):
                    check_cancel(self.cancel)
                    self.message.emit(f"Improving saved results {index+1}/{len(rows)} · {row['name']}")
                    try:
                        result = db.result(row['id'], annotated=False)
                        from tracking import retrack_result
                        result = retrack_result(result, self.cancel, row['path'], self.message.emit, self.progress.emit)
                        db.save_analysis(row['id'], result, self.cancel)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        failures += 1
                        db.state(row['id'], 'ready', f'Gallery upgrade failed: {exc}'[:600])
                    self.progress.emit(index+1, len(rows))
                    self.changed.emit()
                self.message.emit(f'Saved results improved · {failures} failure(s). No model inference was needed.')
                return
            if self.action == 'verify':
                self.verify_downloads()
                return
            if self.action == 'fetch':
                failures += self.fetch(db)
            elif self.action in ('import', 'scan'):
                paths = self.paths if self.action == 'import' else discover(ROOT / 'kiwibit-downloads')
                for index, path in enumerate(paths):
                    check_cancel(self.cancel)
                    self.message.emit(f"Importing {index+1}/{len(paths)} · {Path(path).name}")
                    try:
                        db.ingest(path, copy=self.action == 'import', cancel=self.cancel)
                    except Cancelled:
                        raise
                    except Exception:
                        failures += 1
                    self.progress.emit(index+1, len(paths))
                self.changed.emit()
            if self.action != 'scan':
                rows = [row for row in db.videos() if (row['id'] in self.paths if self.action in ('selected', 'reanalyze')
                                                       else row['status'] != 'ready')]
                labels = read_labels(self.labels)
                failures += self.process_rows(db, rows, labels)
            self.message.emit(f"Finished · {failures} failure(s)." if failures else "Finished · library saved.")
        except Cancelled:
            self.message.emit("Stopped. Completed videos and photos are saved; pending work can resume.")
        except Exception as exc:
            log.exception('Library job failed: action=%s', self.action)
            self.message.emit(f"{type(exc).__name__}: operation failed. Check camera credentials/connection or local model and video files, then retry.")
        finally:
            if db is not None:
                db.close()
            log.info('JOB END action=%s failures=%s cancelled=%s', self.action, failures, self.cancel.is_set())
            self.changed.emit()

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
        db = Catalog(self.root)
        try:
            db.save_analysis(row['id'], result, self.cancel)
            return 0
        except Exception as exc:
            self.restore_state(db, row, exc)
            return 0 if isinstance(exc, Cancelled) else 1
        finally:
            db.close()
            self.changed.emit()

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
                    self.changed.emit()
                    prefix = f"Video {index+1}/{len(rows)} · {row['name']}"
                    log.info('ANALYZE %s id=%s', prefix, row['id'])
                    try:
                        result = self.engine.analyze(Path(row['path']), self.model, labels,
                            lambda text: self.message.emit(f'{prefix} · {text}'),
                            self.progress.emit, self.cancel, force=self.action == 'reanalyze')
                        finish_pending()
                        check_cancel(self.cancel)
                        self.message.emit(f'{prefix} · Saving photos while the next video is analyzed…')
                        pending = (row, saver.submit(self.save_completed, dict(row), result))
                    except Cancelled as exc:
                        self.restore_state(db, row, exc)
                        raise
                    except Exception as exc:
                        self.restore_state(db, row, exc)
                        failures += 1
                    self.changed.emit()
            finally:
                # Drain on success, failure and cancellation before closing the job.
                finish_pending()
        check_cancel(self.cancel)
        return failures

    def fetch(self, db):
        sys.path.insert(0, str(ROOT / 'kiwibit-desktop'))
        from download_cloud_videos import (KiwibitClient, load_config, load_recordings,
            read_manifest, download_one)
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            raise ValueError('FFmpeg and ffprobe are required')
        output = ROOT / 'kiwibit-downloads'
        output.mkdir(exist_ok=True)
        manifest_path = output / 'download_manifest.jsonl'
        manifest = read_manifest(manifest_path)
        client = KiwibitClient(load_config())
        failures = 0
        events = []
        self.message.emit('Checking cameras for recordings…')
        for device in client.devices():
            check_cancel(self.cancel)
            def page_progress(done, total):
                check_cancel(self.cancel)
                self.message.emit(f'Checking cloud recordings · {done} events')
            recordings, _, _ = load_recordings(client, device['serialNumber'], page_progress)
            events.extend(recordings)
        events.sort(key=lambda item: item.get('startTime') or 0, reverse=True)
        for index, event in enumerate(events):
            check_cancel(self.cancel)
            if index % 20 == 0:
                self.message.emit(f'Fetching recordings · {index+1}/{len(events)} (unchanged verified files are skipped)…')
            action = None
            try:
                action, target = download_one(ffmpeg, ffprobe, output, manifest_path, manifest, event, 120)
                db.ingest(target)
            except Exception:
                failures += 1
            if action == 'saved' or (index+1) % 20 == 0 or index+1 == len(events):
                self.progress.emit(index+1, len(events))
                self.changed.emit()
        return failures

    def verify_downloads(self):
        """Explicit local integrity check; no cloud access or AI processing."""
        sys.path.insert(0, str(ROOT / 'kiwibit-desktop'))
        from download_cloud_videos import read_manifest, verify_existing
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            raise ValueError('ffprobe is required')
        output = (ROOT / 'kiwibit-downloads').resolve()
        manifest_path = output / 'download_manifest.jsonl'
        manifest = read_manifest(manifest_path)
        names = set(manifest) | {p.relative_to(output).as_posix() for p in discover(output)}
        report = []
        complete = False
        try:
            for index, name in enumerate(sorted(names)):
                check_cancel(self.cancel)
                self.message.emit(f'Full integrity check · {index+1}/{len(names)}…')
                try:
                    target = (output / name).resolve()
                    if not target.is_relative_to(output):
                        raise ValueError('Manifest path is outside downloads')
                    verify_existing(ffprobe, output, manifest_path, manifest, target, full=True)
                    report.append({'file': name, 'status': 'verified'})
                except Exception as exc:
                    report.append({'file': name, 'status': 'failed', 'error': str(exc)})
                self.progress.emit(index+1, len(names))
            complete = True
        finally:
            report_path = self.root / 'verification-report.json'
            temporary = report_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'complete': complete, 'files': report}, indent=2), encoding='utf-8')
            temporary.replace(report_path)
        failures = sum(item['status'] == 'failed' for item in report)
        self.message.emit(f'Integrity check finished · {len(report)-failures} verified · {failures} failed. Report: {report_path}')


class SavedPlayer(Window):
    """Keep the existing frame overlays, audio, seeking and prediction inspector."""
    def __init__(self, catalog):
        self.catalog = catalog
        self.current_id = None
        self.start_ms = None
        super().__init__()
        self.player.mediaStatusChanged.connect(self.apply_seek)
        self.open_button.hide()
        self.analyze_button.hide()
        self.video_combo.hide()
        self.cancel_button.hide()

    def open_dialog(self):
        pass

    def analyze(self):
        if not self.current_id:
            return
        try:
            result = self.catalog.result(self.current_id)
            if result:
                self.on_analysis(result)
            else:
                self.status.setText('Process this video from the Recordings tab to see AI overlays.')
        except Exception as exc:
            self.status.setText(str(exc))

    def show_video(self, identity, path, ms=0):
        self.current_id, self.start_ms = identity, None
        self.video_path = Path(path)
        self.player.stop()
        self.clear_predictions()
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.play_button.setEnabled(True)
        self.analyze()
        self.start_ms = ms
        self.apply_seek()

    def apply_seek(self, *_):
        if self.start_ms is not None and self.player.mediaStatus() in (
                QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.player.setPosition(int(self.start_ms))
            self.start_ms = None

    def refresh_identifications(self):
        if self.current_id:
            result = self.catalog.result(self.current_id)
            if result:
                from engine import animals_at
                self.analysis = result
                self.timestamps = [frame['ms'] for frame in result['frames']]
                self.canvas.animals = animals_at(result['frames'], self.timestamps,
                                                 self.player.position(), result['fps'])
                self.last_panel_key = object()
                self.refresh_panel()
                self.canvas.update()


class LibraryWindow(QMainWindow):
    def __init__(self, root=None):
        super().__init__()
        self.db = Catalog(root) if root else Catalog()
        self.db.recover()
        self.processing_settings = self.db.root/'processing-settings.json'
        try:
            settings = json.loads(self.processing_settings.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        def batch_setting(key, default):
            value = settings.get(key, default)
            return value if type(value) is int and value in SUPPORTED_BATCH_SIZES else default
        self.engine = Engine(batch_setting('detector_batch', DEFAULT_DETECTOR_BATCH),
                             batch_setting('classifier_batch', DEFAULT_CLASSIFICATION_BATCH))
        self.job = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(250)
        self.refresh_timer.timeout.connect(self.refresh)
        self.closing = False
        self.photo_offset = 0
        self.setWindowTitle('Kiwibit • Bird Library')
        self.resize(1480, 940)
        self.viewer = SavedPlayer(self.db)
        self.setStyleSheet(self.viewer.styleSheet() + '''
            QTabBar::tab { padding: 13px 26px; background: #253640; }
            QTabBar::tab:selected { color: #b3ee78; background: #344d58; }
            QListWidget, QLineEdit { background: #1a272f; border: 1px solid #3a4c57; padding: 8px; }
            QListWidget::item:selected { background: #36535f; }
        ''')
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(22, 20, 22, 20)
        heading = QLabel('KIWIBIT  /  BIRD LIBRARY', objectName='title')
        layout.addWidget(heading)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        actions = QHBoxLayout()
        self.buttons = []
        for label, handler in [('Import videos…', self.import_files), ('Scan downloaded videos', lambda: self.start('scan')),
                               ('Process selected', self.process_selected),
                               ('Process pending / retry failed', lambda: self.start('process')),
                               ('Fetch latest recordings', lambda: self.start('fetch'))]:
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
            self.buttons.append(button)
        self.stop = QPushButton('Stop after current operation')
        self.stop.clicked.connect(self.cancel)
        self.stop.setEnabled(False)
        actions.addWidget(self.stop)
        layout.addLayout(actions)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        recordings = QWidget()
        record_layout = QVBoxLayout(recordings)
        batch_controls = QHBoxLayout()
        batch_controls.addWidget(QLabel('Frames per GPU batch'))
        self.detector_batch = QComboBox()
        self.classifier_batch = QComboBox()
        for combo, selected in [(self.detector_batch, self.engine.detector_batch_size),
                                (self.classifier_batch, self.engine.classification_batch_size)]:
            for value in SUPPORTED_BATCH_SIZES:
                combo.addItem(str(value), value)
            combo.setCurrentIndex(combo.findData(selected))
            combo.currentIndexChanged.connect(self.configure_batch_sizes)
        batch_controls.addWidget(self.detector_batch)
        batch_controls.addWidget(QLabel('Bird crops per GPU batch'))
        batch_controls.addWidget(self.classifier_batch)
        self.batch_note = QLabel('Larger batches use more memory; memory fallback stays enabled.')
        self.batch_note.setWordWrap(True)
        batch_controls.addWidget(self.batch_note, 1)
        record_layout.addLayout(batch_controls)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search recordings by filename or processing status…')
        self.search.textChanged.connect(self.refresh_videos)
        record_layout.addWidget(self.search)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Recording', 'Status', 'Bird photos', 'Details'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.open_recording)
        record_layout.addWidget(self.table)
        record_layout.addWidget(QLabel('Double-click a recording to watch it. Imports are copied into the library; downloaded originals stay in kiwibit-downloads.'))
        self.tabs.addTab(recordings, 'Recordings')
        verify = QPushButton('Full integrity check of downloads')
        verify.clicked.connect(lambda: self.start('verify'))
        record_layout.addWidget(verify)
        self.buttons.append(verify)
        improve = QPushButton('Improve existing saved results')
        improve.clicked.connect(lambda: self.start('upgrade'))
        record_layout.addWidget(improve)
        self.buttons.append(improve)
        fresh = QPushButton('Reanalyze selected with fresh AI')
        fresh.clicked.connect(lambda: self.process_selected(fresh=True))
        record_layout.addWidget(fresh)
        self.buttons.append(fresh)
        birds = QWidget()
        bird_layout = QVBoxLayout(birds)
        bird_layout.addWidget(QLabel('Species supported across a sighting · best distinct photos first · double-click a photo to watch that moment'))
        self.all_photos = QCheckBox('All photos (include near-duplicates)')
        self.all_photos.toggled.connect(self.choose_species)
        bird_layout.addWidget(self.all_photos)
        split = QSplitter()
        self.species_list = QListWidget()
        self.species_list.currentItemChanged.connect(self.choose_species)
        split.addWidget(self.species_list)
        self.gallery = QListWidget()
        self.gallery.setViewMode(QListWidget.IconMode)
        self.gallery.setResizeMode(QListWidget.Adjust)
        self.gallery.setIconSize(QSize(210, 155))
        self.gallery.setGridSize(QSize(260, 225))
        self.gallery.setWordWrap(True)
        self.gallery.setSpacing(8)
        self.gallery.itemDoubleClicked.connect(self.open_photo_video)
        self.gallery.currentItemChanged.connect(self.photo_selected)
        split.addWidget(self.gallery)
        split.setSizes([290, 1100])
        bird_layout.addWidget(split, 1)
        self.photo_details = QLabel('Select a photo to inspect the sighting or correct its species.')
        self.photo_details.setWordWrap(True)
        bird_layout.addWidget(self.photo_details)
        gallery_actions = QHBoxLayout()
        self.more = QPushButton('Load more photos')
        self.more.clicked.connect(self.load_photos)
        gallery_actions.addWidget(self.more)
        full = QPushButton('Open selected photo')
        full.clicked.connect(self.open_photo)
        gallery_actions.addWidget(full)
        self.correct_button = QPushButton('Correct sighting…')
        self.correct_button.clicked.connect(self.correct_photo)
        gallery_actions.addWidget(self.correct_button)
        self.restore_button = QPushButton('Use AI identification')
        self.restore_button.clicked.connect(lambda: self.apply_correction(None))
        gallery_actions.addWidget(self.restore_button)
        self.review_button = QPushButton('Review unmatched corrections')
        self.review_button.clicked.connect(self.review_corrections)
        gallery_actions.addWidget(self.review_button)
        gallery_actions.addStretch()
        bird_layout.addLayout(gallery_actions)
        self.tabs.addTab(birds, 'Bird species + photos')
        self.tabs.addTab(self.viewer, 'Video + AI overlays')
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel('Scan downloaded videos or import files to begin. Cloud access happens only when requested.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh()

    def import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Import camera videos', str(ROOT / 'kiwibit-downloads'),
                                               'Videos (*.mp4 *.mov *.mkv *.avi *.webm *.m4v)')
        if paths:
            self.start('import', paths)

    def configure_batch_sizes(self, *_):
        if self.job and self.job.isRunning():
            return
        settings = {'detector_batch': self.detector_batch.currentData(),
                    'classifier_batch': self.classifier_batch.currentData()}
        temporary = self.processing_settings.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps(settings, indent=2), encoding='utf-8')
            temporary.replace(self.processing_settings)
        except OSError as exc:
            self.status.setText(f'Could not save GPU batch settings: {exc}')
            return
        self.engine.detector_batch_size = settings['detector_batch']
        self.engine.classification_batch_size = settings['classifier_batch']
        if self.engine.auto_decode_queue:
            self.engine.decode_queue = max(settings.values())
        self.batch_note.setText('Batch sizes saved. They apply to the next analysis; cached results are reused.')

    def process_selected(self, fresh=False):
        identities = [self.table.item(index.row(), 0).data(Qt.UserRole)['id']
                      for index in self.table.selectionModel().selectedRows()]
        if identities:
            self.start('reanalyze' if fresh else 'selected', identities)
        else:
            self.status.setText('Select one or more rows in Recordings first.')

    def start(self, action, paths=None):
        if self.job and self.job.isRunning():
            return
        self.job = LibraryJob(action, paths or [], self.viewer.model_dir, self.viewer.label_path, self.db.root, self.engine)
        self.job.message.connect(self.status.setText)
        if diagnostics:
            self.job.message.connect(diagnostics.status)
        self.job.changed.connect(self.schedule_refresh)
        self.job.progress.connect(self.on_progress)
        self.job.finished.connect(self.finished)
        for button in self.buttons:
            button.setEnabled(False)
        self.viewer.model_button.setEnabled(False)
        self.viewer.labels_button.setEnabled(False)
        self.detector_batch.setEnabled(False)
        self.classifier_batch.setEnabled(False)
        self.stop.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.job.start()

    def on_progress(self, value, total):
        self.progress.setRange(0, max(total, value, 1))
        self.progress.setValue(value)

    def cancel(self):
        log.info('Stop requested')
        if self.job:
            self.job.cancel.set()
            self.status.setText('Stopping after the current model/download operation; completed work is kept…')
            self.stop.setEnabled(False)

    def finished(self):
        self.progress.hide()
        self.stop.setEnabled(False)
        for button in self.buttons:
            button.setEnabled(True)
        self.viewer.model_button.setEnabled(True)
        self.viewer.labels_button.setEnabled(True)
        self.detector_batch.setEnabled(True)
        self.classifier_batch.setEnabled(True)
        if (self.engine.detector_batch_size != self.detector_batch.currentData()
                or self.engine.classification_batch_size != self.classifier_batch.currentData()):
            self.batch_note.setText(f'Memory fallback: now using {self.engine.detector_batch_size} frames / '
                                   f'{self.engine.classification_batch_size} crops per GPU batch.')
        self.refresh()
        if self.closing:
            self.close()

    def schedule_refresh(self):
        if not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def refresh_videos(self, _text=None, *, rows=None):
        text = self.search.text().casefold()
        rows = [row for row in (self.db.videos() if rows is None else rows)
                if text in (row['name'] + ' ' + row['status']).casefold()]
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate([row['name'], row['status'], row['photos'], row['error']]):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, dict(row))
                item.setToolTip(row['path'] if column == 0 else str(value))
                self.table.setItem(index, column, item)

    def refresh(self):
        self.refresh_timer.stop()
        rows = self.db.videos()
        self.refresh_videos(rows=rows)
        species = self.db.species()
        self.summary.setText(f"{len(rows)} recordings  ·  {sum(row['status']=='ready' for row in rows)} processed  ·  {len(species)} species groups  ·  {sum(row['photos'] for row in species)} bird photos saved")
        unmatched = len(self.db.unmatched_corrections())
        self.review_button.setText(f'Review unmatched corrections ({unmatched})')
        self.review_button.setEnabled(unmatched > 0)
        selected = self.species_list.currentItem()
        name = selected.data(Qt.UserRole) if selected else None
        self.species_list.blockSignals(True)
        self.species_list.clear()
        chosen = None
        for row in species:
            item = QListWidgetItem(f"{row['species']}\n{row['photos']} photos · {row['videos']} videos")
            item.setData(Qt.UserRole, row['species'])
            self.species_list.addItem(item)
            if row['species'] == name:
                chosen = item
        self.species_list.blockSignals(False)
        if chosen:
            self.species_list.setCurrentItem(chosen)
        elif species:
            self.species_list.setCurrentRow(0)
        else:
            self.choose_species()

    def choose_species(self, *_):
        self.gallery.clear()
        self.photo_offset = 0
        self.load_photos()

    def load_photos(self):
        item = self.species_list.currentItem()
        rows = self.db.photos(item.data(Qt.UserRole), self.photo_offset, best_only=not self.all_photos.isChecked()) if item else []
        for row in rows:
            source = 'Your correction' if row['corrected'] else 'AI sighting'
            photo = QListWidgetItem(QIcon(str(self.db.root / row['path'])),
                f"{clock(row['ms'])} · {source}\n{row['name']}")
            photo.setData(Qt.UserRole, dict(row))
            photo.setToolTip(f"{row['species']} · {source}\n{row['name']} at {row['ms']/1000:.2f}s\nPhoto quality: {row['quality']:.0f}/100 (heuristic)")
            self.gallery.addItem(photo)
        self.photo_offset += len(rows)
        self.more.setEnabled(len(rows) == 100)
        self.photo_selected(self.gallery.currentItem())

    def photo_selected(self, item, *_):
        if not hasattr(self, 'correct_button'):
            return
        row = item.data(Qt.UserRole) if item else None
        usable = bool(row and row.get('sighting_id'))
        self.correct_button.setEnabled(usable)
        self.restore_button.setEnabled(usable and bool(row['corrected']))
        if usable:
            evidence = json.loads(row['evidence'])
            note = 'Your correction' if row['corrected'] else evidence['reason']
            self.photo_details.setText(f"{row['species']} · sighting {clock(row['start_ms'])}–{clock(row['end_ms'])} · {note}. "
                f"AI suggestion: {evidence['suggestion'] or 'none'}; {evidence['support']}/{evidence['samples']} sampled views agree. "
                'Agreement is not a probability.')
        else:
            self.photo_details.setText('Select a photo to correct its entire sighting. Older results can be upgraded from Recordings.')

    def correct_photo(self):
        item = self.gallery.currentItem()
        if not item:
            return
        row = item.data(Qt.UserRole)
        try:
            choices = read_labels(self.viewer.label_path)
        except (OSError, ValueError):
            choices = []
        choices = list(dict.fromkeys([row['species'], UNCERTAIN] + choices))
        label, ok = QInputDialog.getItem(self, 'Correct this bird sighting',
            'Species for every photo in this sighting (you can type a name):', choices, 0, True)
        if ok:
            self.apply_correction(label)

    def apply_correction(self, label):
        item = self.gallery.currentItem()
        if not item:
            return
        row = item.data(Qt.UserRole)
        try:
            self.db.correct_sighting(row['sighting_id'], label)
            target_species = label.strip() if label is not None else row['ai_species']
            self.refresh()
            for index in range(self.species_list.count()):
                if self.species_list.item(index).data(Qt.UserRole) == target_species:
                    self.species_list.setCurrentRow(index)
                    break
            self.viewer.refresh_identifications()
            self.status.setText('Sighting updated. All its photos and overlays use this identification; original model predictions are retained.')
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Could not update sighting', str(exc))

    def review_corrections(self):
        rows = self.db.unmatched_corrections()
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Preserved corrections needing review')
        dialog.setText('These corrections could not be safely matched after reprocessing or a source-file change. '
                       'They are preserved here. Inspect the corresponding recording and correct its new sighting if appropriate.')
        dialog.setDetailedText('\n'.join(f"{r['name']} · {clock(r['start_ms'])}–{clock(r['end_ms'])} · {r['label']}" for r in rows))
        dialog.exec()

    def open_recording(self, row, column):
        data = self.table.item(row, 0).data(Qt.UserRole)
        log.info('PLAY recording id=%s', data['id'])
        self.viewer.show_video(data['id'], data['path'])
        self.tabs.setCurrentIndex(2)

    def open_photo_video(self, item):
        row = item.data(Qt.UserRole)
        log.info('PLAY photo video_id=%s ms=%s', row['video_id'], row['ms'])
        self.viewer.show_video(row['video_id'], row['video'], row['ms'])
        self.tabs.setCurrentIndex(2)

    def open_photo(self):
        item = self.gallery.currentItem()
        if item:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.db.root / item.data(Qt.UserRole)['path'])))

    def closeEvent(self, event):
        log.info('Window close requested')
        self.closing = True
        if self.job and self.job.isRunning():
            self.cancel()
            event.ignore()
            return
        self.viewer.player.stop()
        self.viewer.worker.shutdown()
        self.viewer.worker.wait()
        self.refresh_timer.stop()
        self.db.close()
        log.info('Window shutdown complete')
        event.accept()


def main():
    app = QApplication(sys.argv)
    if diagnostics:
        diagnostics.attach_qt(app)
    app.setApplicationName('Kiwibit Bird Library')
    # A single catalog owner keeps recovery and processing queues unambiguous.
    from PySide6.QtCore import QLockFile
    from catalog import LIBRARY
    LIBRARY.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(LIBRARY / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, 'Bird Library', 'The bird library is already open.')
        return 0
    window = LibraryWindow()
    window.show()
    log.info('Library window shown')
    if not window.db.videos():
        QTimer.singleShot(100, lambda: window.start('scan'))
    elif any(row['status'] == 'ready' and row['gallery_version'] < SMART_VERSION for row in window.db.videos()):
        QTimer.singleShot(100, lambda: window.start('upgrade'))
    code = app.exec()
    log.info('Qt event loop exited: code=%s', code)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
