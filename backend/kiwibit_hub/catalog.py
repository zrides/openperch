"""Durable local video catalog and timestamped bird crops. No cloud credentials."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from . import config
from .cancel import Cancelled, check_cancel
from .overlap import origin_of, read_cloud_files
from .sightings import (SMART_VERSION, UNCERTAIN, animal_tag, best_photos, collect_sightings, other_animals,
                        photo_quality, sighting_matches, summarize)
from .tracking import NONBIRD_LABELS

LIBRARY = config.LIBRARY
EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def gallery_species(label):
    return 'Uncertain bird (non-bird prediction)' if label in NONBIRD_LABELS else label


def discover(folder):
    return sorted(p for p in Path(folder).rglob("*") if p.is_file()
                  and p.suffix.lower() in EXTENSIONS and ".part." not in p.name and p.stat().st_size)


def encode_photo(crop, box, target, cancel):
    """Worker owns its crop; it never accesses SQLite or mutates sighting state."""
    import cv2
    check_cancel(cancel)
    quality, fingerprint = photo_quality(crop, box)
    scale = min(1, 1200 / max(crop.shape[:2]))
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale)
    check_cancel(cancel)
    ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError('Could not encode bird photo')
    check_cancel(cancel)
    target.write_bytes(encoded.tobytes())
    return quality, fingerprint


class Catalog:
    def __init__(self, root=None, media_root=None, check_same_thread=True, migrate=True):
        self.root = Path(root) if root is not None else LIBRARY
        # Videos under media_root are stored relative to it, so one library works
        # both natively and inside a container that mounts the data elsewhere.
        self.media_root = Path(media_root if media_root is not None else config.DATA).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite3", timeout=30, check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        self._cloud = (None, set())  # (manifest mtime, cloud_files())
        if migrate:
            self._migrate()

    def _migrate(self):
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(videos)')}
        if columns and 'gallery_version' not in columns:
            backup_path = self.root / 'library-before-smart-v1.sqlite3'
            if not backup_path.exists():
                with closing(sqlite3.connect(backup_path)) as backup:
                    self.db.backup(backup)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, modified INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                result TEXT, added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS photos (
                video_id TEXT NOT NULL, species TEXT NOT NULL, ms REAL NOT NULL,
                track INTEGER NOT NULL, similarity REAL NOT NULL, path TEXT NOT NULL,
                FOREIGN KEY(video_id) REFERENCES videos(id));
            CREATE INDEX IF NOT EXISTS photos_species ON photos(species);
            CREATE INDEX IF NOT EXISTS photos_video ON photos(video_id);
            CREATE TABLE IF NOT EXISTS sightings (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL, source_signature TEXT NOT NULL,
                track INTEGER NOT NULL, start_ms REAL NOT NULL, end_ms REAL NOT NULL,
                ai_species TEXT NOT NULL, evidence TEXT NOT NULL, anchors TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS sightings_video ON sightings(video_id, active);
            CREATE TABLE IF NOT EXISTS corrections (
                sighting_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            -- Moments identified by eye to measure the AI (see review.py). Keyed by frame
            -- and box, not sighting, so answers survive re-tracking and re-analysis.
            CREATE TABLE IF NOT EXISTS answer_key (
                id TEXT PRIMARY KEY, position INTEGER NOT NULL, video_id TEXT NOT NULL,
                frame INTEGER NOT NULL, ms REAL NOT NULL, box TEXT NOT NULL, view TEXT NOT NULL,
                stratum TEXT NOT NULL, verdict TEXT, answer TEXT, answered TEXT);
        """)
        with self.db:
            video_columns = {r[1] for r in self.db.execute('PRAGMA table_info(videos)')}
            if 'gallery_version' not in video_columns:
                self.db.execute('ALTER TABLE videos ADD COLUMN gallery_version INTEGER NOT NULL DEFAULT 0')
            if 'animals' not in video_columns:
                # Other animals seen (JSON list of tags); NULL until the analysis is read.
                self.db.execute('ALTER TABLE videos ADD COLUMN animals TEXT')
                self._tag_animals()
            if 'origin' not in video_columns:
                # cloud, sdcard, live or unknown (see overlap.py); card clips also get their
                # length (0: unplayable) and, when the name has no time, the file's creation time.
                # always_show keeps a cloud clip listed even when the card holds the same moment.
                for name, declaration in [('origin', 'TEXT'), ('duration', 'REAL'), ('started', 'REAL'),
                                          ('always_show', 'INTEGER NOT NULL DEFAULT 0')]:
                    self.db.execute(f'ALTER TABLE videos ADD COLUMN {name} {declaration}')
                cloud = self.cloud_files()
                for identity, path in self.db.execute('SELECT id, path FROM videos').fetchall():
                    self.db.execute('UPDATE videos SET origin=? WHERE id=?', (origin_of(path, cloud), identity))
            if 'batch' not in {r[1] for r in self.db.execute('PRAGMA table_info(answer_key)')}:
                # Moments are added in batches; the first key is batch 1.
                self.db.execute('ALTER TABLE answer_key ADD COLUMN batch INTEGER NOT NULL DEFAULT 1')
            photo_columns = {r[1] for r in self.db.execute('PRAGMA table_info(photos)')}
            for name, declaration in [('sighting_id', 'TEXT'), ('quality', 'REAL NOT NULL DEFAULT 0'),
                                      ('featured', 'INTEGER NOT NULL DEFAULT 1')]:
                if name not in photo_columns:
                    self.db.execute(f'ALTER TABLE photos ADD COLUMN {name} {declaration}')
            # Each sighting's best photo; without it every lookup scans all photos.
            self.db.execute('CREATE INDEX IF NOT EXISTS photos_sighting ON photos(sighting_id, quality)')

    def _tag_animals(self):
        """Tag analyses saved before other animals were tagged (a one-time read of each)."""
        for row in self.db.execute("SELECT id, result FROM videos WHERE status='ready' AND result IS NOT NULL").fetchall():
            try:
                with gzip.open(self.root / row['result'], 'rt', encoding='utf-8') as stream:
                    animals = other_animals(json.load(stream)['frames'])
            except (OSError, ValueError, KeyError):
                continue
            self.db.execute('UPDATE videos SET animals=? WHERE id=?', (json.dumps(animals), row['id']))

    def close(self):
        self.db.close()

    def cloud_files(self):
        """Downloads the cloud manifest vouches for; reread only when the manifest changes (a scan ingests many)."""
        manifest = self.media_root / 'downloads' / 'download_manifest.jsonl'
        try:
            mtime = manifest.stat().st_mtime_ns
        except OSError:
            return set()
        if self._cloud[0] != mtime:
            self._cloud = (mtime, read_cloud_files(manifest))
        return self._cloud[1]

    def stored_path(self, path):
        path = Path(path).resolve()
        try:
            return path.relative_to(self.media_root).as_posix()
        except ValueError:
            return str(path)

    def video_path(self, row):
        path = Path(row['path'])
        return path if path.is_absolute() else self.media_root / path

    def recover(self):
        with self.db:
            self.db.execute("UPDATE videos SET status='pending', error='Interrupted; ready to resume' WHERE status='processing'")

    def ingest(self, source, copy=False, cancel=None, origin=None):
        source = Path(source).resolve()
        if source.suffix.lower() not in EXTENSIONS or ".part." in source.name or not source.stat().st_size:
            raise ValueError("Choose a completed, nonempty video")
        original_name = source.name
        if copy:
            # Content-addressed copies: duplicates share one retained original.
            folder = self.root / "videos"
            folder.mkdir(exist_ok=True)
            temp = folder / (uuid.uuid4().hex + ".part")
            digest = hashlib.sha256()
            try:
                with source.open("rb") as src, temp.open("xb") as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        if cancel is not None:
                            check_cancel(cancel)
                        digest.update(block)
                        dst.write(block)
                target = folder / (digest.hexdigest() + source.suffix.lower())
                if target.exists():
                    if target.stat().st_size != temp.stat().st_size:
                        raise ValueError("Stored video size mismatch; original preserved")
                else:
                    temp.rename(target)
                with target.open('rb') as retained:
                    if hashlib.file_digest(retained, 'sha256').hexdigest() != digest.hexdigest():
                        raise ValueError('Stored video hash mismatch; original preserved')
                source = target.resolve()
            finally:
                temp.unlink(missing_ok=True)
        stat = source.stat()
        stored = self.stored_path(source)
        identity = hashlib.sha256(stored.casefold().encode()).hexdigest()
        origin = origin or origin_of(stored, self.cloud_files())
        with self.db:
            existing = self.db.execute("SELECT * FROM videos WHERE id=?", (identity,)).fetchone()
            if existing and (existing['size'], existing['modified']) != (stat.st_size, stat.st_mtime_ns):
                self.db.execute("DELETE FROM photos WHERE video_id=?", (identity,))
                self.db.execute('UPDATE sightings SET active=0 WHERE video_id=?', (identity,))
                self.db.execute('''UPDATE videos SET status='pending', result=NULL, gallery_version=0, animals=NULL, error='',
                                   duration=NULL, started=NULL, size=?, modified=? WHERE id=?''',
                                (stat.st_size, stat.st_mtime_ns, identity))
            self.db.execute("INSERT OR IGNORE INTO videos(id,path,name,size,modified,origin) VALUES(?,?,?,?,?,?)",
                            (identity, stored, original_name, stat.st_size, stat.st_mtime_ns, origin))
            if origin != 'unknown':
                self.db.execute("UPDATE videos SET origin=? WHERE id=? AND COALESCE(origin, 'unknown')='unknown'",
                                (origin, identity))
        if existing and existing['result'] and (existing['size'], existing['modified']) != (stat.st_size, stat.st_mtime_ns):
            self._remove_result_folder(self.root / Path(existing['result']).parent)
        return identity

    def videos(self):
        return self.db.execute("""SELECT v.*, COALESCE(p.photos, 0) AS photos
            FROM videos v LEFT JOIN
                (SELECT video_id, count(*) AS photos FROM photos GROUP BY video_id) p
            ON p.video_id=v.id ORDER BY v.name DESC""").fetchall()

    def video_species(self):
        """Species photographed in each video, most photographed first."""
        species = {}
        for video_id, name, _ in self.db.execute(
                'SELECT video_id, species, count(*) AS n FROM photos GROUP BY video_id, species ORDER BY n DESC'):
            species.setdefault(video_id, []).append(name)
        return species

    def video_animals(self):
        """Other animals tagged in each video: from the analysis, and from sightings that are one."""
        animals = {row['id']: set(json.loads(row['animals']))
                   for row in self.db.execute('SELECT id, animals FROM videos WHERE animals IS NOT NULL')}
        # A bird sighting the species model read as an animal is shown as uncertain; a
        # correction to an animal's name counts too.
        for row in self.db.execute('''SELECT s.video_id, s.ai_species, s.evidence, c.label FROM sightings s
                LEFT JOIN corrections c ON c.sighting_id=s.id WHERE s.active=1'''):
            label = row['label'] or (json.loads(row['evidence']).get('suggestion', '')
                                     if row['ai_species'] == UNCERTAIN else '')
            tag = animal_tag(label)
            if tag:
                animals.setdefault(row['video_id'], set()).add(tag)
        return {video_id: sorted(tags) for video_id, tags in animals.items() if tags}

    def covers(self):
        """The best-quality photo of each video, for thumbnails."""
        # SQLite returns the row holding max(quality) for bare columns.
        return {video_id: path for video_id, path, _ in self.db.execute(
            'SELECT video_id, path, max(quality) FROM photos GROUP BY video_id')}

    def unmeasured_card_clips(self):
        return self.db.execute("SELECT * FROM videos WHERE origin='sdcard' AND duration IS NULL ORDER BY name").fetchall()

    def set_media(self, identity, duration, started):
        with self.db:
            self.db.execute('UPDATE videos SET duration=?, started=? WHERE id=?', (duration, started, identity))

    def set_always_show(self, identity, value):
        with self.db:
            self.db.execute('UPDATE videos SET always_show=? WHERE id=?', (int(bool(value)), identity))

    def state(self, identity, status, error=""):
        with self.db:
            self.db.execute("UPDATE videos SET status=?,error=? WHERE id=?", (status, error, identity))

    def species(self):
        return self.db.execute("""SELECT species, count(*) AS photos, count(DISTINCT video_id) AS videos,
            sum(featured) AS featured FROM photos GROUP BY species ORDER BY species""").fetchall()

    def photos(self, species, offset=0, limit=100, best_only=False):
        filter_best = 'AND p.featured=1' if best_only else ''
        return self.db.execute(f"""SELECT p.*,v.path AS video,v.name,s.start_ms,s.end_ms,
                               s.evidence,s.ai_species,c.label AS corrected
                               FROM photos p JOIN videos v ON v.id=p.video_id
                               LEFT JOIN sightings s ON s.id=p.sighting_id
                               LEFT JOIN corrections c ON c.sighting_id=s.id
                               WHERE p.species=? {filter_best}
                               ORDER BY p.quality DESC,v.name DESC,p.ms LIMIT ? OFFSET ?""",
                               (species, limit, offset)).fetchall()

    def result(self, identity, annotated=True):
        row = self.db.execute("SELECT * FROM videos WHERE id=?", (identity,)).fetchone()
        if not row or row['status'] != 'ready' or not row['result']:
            return None
        stat = self.video_path(row).stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['size'], row['modified']):
            raise ValueError("Video changed; scan/import it again before processing")
        with gzip.open(self.root / row['result'], "rt", encoding="utf-8") as stream:
            result = json.load(stream)
        if annotated:
            groups = {}
            for sighting in self.db.execute('''SELECT s.*,c.label FROM sightings s
                    LEFT JOIN corrections c ON c.sighting_id=s.id WHERE s.video_id=? AND s.active=1''', (identity,)):
                groups.setdefault(sighting['track'], []).append(sighting)
            for frame in result['frames']:
                for animal in frame['animals']:
                    if animal['category'] != 'bird':
                        continue
                    for sighting in groups.get(animal['id'], []):
                        if sighting['start_ms'] <= frame['ms'] <= sighting['end_ms']:
                            evidence = json.loads(sighting['evidence'])
                            animal['display_label'] = sighting['label'] or sighting['ai_species']
                            animal['sighting_id'] = sighting['id']
                            animal['identification_note'] = ('Your correction' if sighting['label'] else
                                f"Sighting: {evidence['support']}/{evidence['samples']} sampled views agree. {evidence['reason']}.")
                            break
        return result

    def correct_sighting(self, sighting_id, label):
        """A correction applies to this appearance only; it never changes model weights."""
        label = label.strip() if label is not None else None
        if label is not None and (not label or len(label) > 120 or any(ord(c) < 32 for c in label)):
            raise ValueError('Enter a species name (1–120 characters)')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            sighting = self.db.execute('SELECT * FROM sightings WHERE id=? AND active=1', (sighting_id,)).fetchone()
            if not sighting:
                raise ValueError('This sighting changed; refresh the gallery and select it again')
            if label is None:
                self.db.execute('DELETE FROM corrections WHERE sighting_id=?', (sighting_id,))
            else:
                self.db.execute('''INSERT INTO corrections(sighting_id,label) VALUES(?,?)
                    ON CONFLICT(sighting_id) DO UPDATE SET label=excluded.label,updated=CURRENT_TIMESTAMP''',
                    (sighting_id, label))
            self.db.execute('UPDATE photos SET species=? WHERE sighting_id=?',
                            (label or sighting['ai_species'], sighting_id))

    def sightings(self, video_id):
        """Active sightings of one recording, with any correction and its best photo."""
        return self.db.execute('''SELECT s.id, s.track, s.start_ms, s.end_ms, s.ai_species, s.evidence,
                   c.label AS corrected,
                   (SELECT p.path FROM photos p WHERE p.sighting_id=s.id ORDER BY p.quality DESC LIMIT 1) AS photo,
                   (SELECT count(*) FROM photos p WHERE p.sighting_id=s.id) AS photos
            FROM sightings s LEFT JOIN corrections c ON c.sighting_id=s.id
            WHERE s.video_id=? AND s.active=1 ORDER BY s.start_ms''', (video_id,)).fetchall()

    def unmatched_corrections(self):
        return self.db.execute('''SELECT s.*,c.label,v.name FROM corrections c
            JOIN sightings s ON s.id=c.sighting_id JOIN videos v ON v.id=s.video_id
            WHERE s.active=0 ORDER BY c.updated DESC''').fetchall()

    def save_analysis(self, identity, result, cancel, photo_workers=4):
        """Publish results and gallery together; interrupted extraction stays retryable."""
        import cv2
        check_cancel(cancel)
        row = self.db.execute("SELECT * FROM videos WHERE id=?", (identity,)).fetchone()
        video = self.video_path(row)
        stat = video.stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['size'], row['modified']):
            raise ValueError("Video changed during processing; scan it again")
        photos = []
        groups = collect_sightings(result['frames'])
        source_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        old = [dict(item) for item in self.db.execute('SELECT * FROM sightings WHERE video_id=? AND source_signature=?',
                                                     (identity, source_signature))]
        for item in old:
            item['anchors'] = json.loads(item['anchors'])
        matched = sighting_matches(groups, old)
        previous = row['result']
        relative = Path("results") / (identity + "-" + uuid.uuid4().hex)
        folder = self.root / relative
        # Nothing may raise between creating these and the try that cleans them up.
        folder.mkdir(parents=True)
        capture = cv2.VideoCapture(str(video))
        committed = False
        try:
            if not capture.isOpened():
                raise ValueError("Cannot decode video for gallery")
            requests = defaultdict(list)
            for group_index, group in enumerate(groups):
                group['id'] = matched.get(group_index, uuid.uuid4().hex)
                for view in group['views']:
                    requests[view['frame']].append(view)
            # Decode each requested frame exactly once, in order. H.264 random seeks
            # repeatedly decoded the same GOP and duplicate bird frames previously.
            pending = deque()
            workers = max(1, min(4, photo_workers))
            def finish_one():
                view, future = pending.popleft()
                view['quality'], view['fingerprint'] = future.result()
            # The pool drains before failure cleanup, so no writer can outlive its folder.
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bird-photo') as pool:
                for frame_index in range(max(requests, default=-1)+1):
                    check_cancel(cancel)
                    if not capture.grab():
                        raise ValueError('Could not extract a gallery frame')
                    if frame_index not in requests:
                        continue
                    ok, image = capture.retrieve()
                    if not ok:
                        raise ValueError('Could not decode a gallery frame')
                    height, width = image.shape[:2]
                    for view in requests[frame_index]:
                        check_cancel(cancel)
                        animal = view['animal']
                        x1, y1, x2, y2 = animal['box']
                        crop = image[max(0, int(y1*height)):min(height, int(y2*height)),
                                     max(0, int(x1*width)):min(width, int(x2*width))]
                        if not crop.size:
                            continue
                        # Bound queued image memory independently of clip length.
                        if len(pending) >= workers*2:
                            finish_one()
                        if view['photo']:
                            filename = f"{view['frame']}-{animal['id']}.jpg"
                            view['path'] = str(relative / filename)
                            pending.append((view, pool.submit(encode_photo, crop.copy(), animal['box'], folder/filename, cancel)))
                        else:
                            # A species view between photos: its sharpness still weights its vote.
                            pending.append((view, pool.submit(photo_quality, crop.copy(), animal['box'])))
                while pending:
                    check_cancel(cancel)
                    finish_one()
            for group in groups:
                group['views'] = [v for v in group['views'] if 'quality' in v]
                group['evidence'] = summarize(group['views'], NONBIRD_LABELS)
                photographed = [v for v in group['views'] if 'path' in v]
                featured = best_photos(photographed)
                for view in photographed:
                    photos.append((identity, group['evidence']['species'], view['ms'], group['track'],
                                   group['evidence']['similarity'], view['path'], group['id'],
                                   view['quality'], int(view['path'] in featured)))
            with gzip.open(folder / "analysis.json.gz", "wt", encoding="utf-8") as sink:
                json.dump(result, sink, separators=(',', ':'))
            check_cancel(cancel)
            final_stat = video.stat()
            if (final_stat.st_size, final_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('Video changed during gallery extraction; scan it again')
            with self.db:
                self.db.execute("DELETE FROM photos WHERE video_id=?", (identity,))
                self.db.execute('UPDATE sightings SET active=0 WHERE video_id=?', (identity,))
                for group in groups:
                    anchors = [{'ms': v['ms'], 'box': v['animal']['box']} for v in group['views']]
                    self.db.execute('''INSERT INTO sightings VALUES(?,?,?,?,?,?,?,?,?,1)
                        ON CONFLICT(id) DO UPDATE SET track=excluded.track,start_ms=excluded.start_ms,
                        end_ms=excluded.end_ms,ai_species=excluded.ai_species,evidence=excluded.evidence,
                        anchors=excluded.anchors,active=1''',
                        (group['id'], identity, source_signature, group['track'], group['start_ms'], group['end_ms'],
                         group['evidence']['species'], json.dumps(group['evidence']), json.dumps(anchors)))
                self.db.executemany('''INSERT INTO photos(video_id,species,ms,track,similarity,path,sighting_id,quality,featured)
                                      VALUES(?,?,?,?,?,?,?,?,?)''', photos)
                # Read corrections inside the publish transaction: another connection may
                # have received a user edit while crop extraction was in progress.
                self.db.execute('''UPDATE photos SET species=(SELECT label FROM corrections WHERE sighting_id=photos.sighting_id)
                    WHERE video_id=? AND sighting_id IN (SELECT sighting_id FROM corrections)''', (identity,))
                self.db.execute("UPDATE videos SET status='ready',error='',result=?,gallery_version=?,animals=? WHERE id=?",
                                (str(relative / "analysis.json.gz"), SMART_VERSION,
                                 json.dumps(other_animals(result['frames'])), identity))
            committed = True
        finally:
            capture.release()
            if not committed:
                self._remove_result_folder(folder)
        if previous:
            # The catalog no longer references the superseded analysis or its photos.
            self._remove_result_folder(self.root / Path(previous).parent)

    def _remove_result_folder(self, folder):
        resolved = folder.resolve()
        results = (self.root / 'results').resolve()
        if resolved != results and resolved.is_relative_to(results) and resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
            # Cached gallery thumbnails mirror the results tree.
            thumbs = (self.root / 'thumbs' / resolved.relative_to(self.root.resolve())).resolve()
            if thumbs.is_dir():
                shutil.rmtree(thumbs, ignore_errors=True)

    def thumbnail(self, photo, size=360):
        """A cached JPEG no larger than `size` pixels for a gallery photo (relative results path)."""
        import cv2
        source = (self.root / photo).resolve()
        target = (self.root / 'thumbs' / photo).resolve()
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
        image = cv2.imread(str(source))
        if image is None:
            raise ValueError('Unreadable photo')
        scale = min(1.0, size / max(image.shape[:2]))
        if scale < 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError('Could not encode thumbnail')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f'{target.stem}.{uuid.uuid4().hex}.tmp')
        temporary.write_bytes(encoded.tobytes())
        temporary.replace(target)
        return target
