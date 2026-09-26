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
from .sightings import SMART_VERSION, collect_sightings, photo_quality, summarize, best_photos, sighting_matches

LIBRARY = config.LIBRARY
EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
NONBIRD_LABELS = set((Path(__file__).parent / 'nonbird-labels.txt').read_text(encoding='utf-8-sig').splitlines())


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
    def __init__(self, root=None, media_root=None, check_same_thread=True):
        self.root = Path(root) if root is not None else LIBRARY
        # Videos under media_root are stored relative to it, so one library works
        # both natively and inside a container that mounts the data elsewhere.
        self.media_root = Path(media_root if media_root is not None else config.DATA).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "library.sqlite3", timeout=30, check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
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
            CREATE TABLE IF NOT EXISTS sightings (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL, source_signature TEXT NOT NULL,
                track INTEGER NOT NULL, start_ms REAL NOT NULL, end_ms REAL NOT NULL,
                ai_species TEXT NOT NULL, evidence TEXT NOT NULL, anchors TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS sightings_video ON sightings(video_id, active);
            CREATE TABLE IF NOT EXISTS corrections (
                sighting_id TEXT PRIMARY KEY, label TEXT NOT NULL,
                updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        with self.db:
            if 'gallery_version' not in {r[1] for r in self.db.execute('PRAGMA table_info(videos)')}:
                self.db.execute('ALTER TABLE videos ADD COLUMN gallery_version INTEGER NOT NULL DEFAULT 0')
            photo_columns = {r[1] for r in self.db.execute('PRAGMA table_info(photos)')}
            for name, declaration in [('sighting_id', 'TEXT'), ('quality', 'REAL NOT NULL DEFAULT 0'),
                                      ('featured', 'INTEGER NOT NULL DEFAULT 1')]:
                if name not in photo_columns:
                    self.db.execute(f'ALTER TABLE photos ADD COLUMN {name} {declaration}')

    def close(self):
        self.db.close()

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

    def ingest(self, source, copy=False, cancel=None):
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
        with self.db:
            existing = self.db.execute("SELECT * FROM videos WHERE id=?", (identity,)).fetchone()
            if existing and (existing['size'], existing['modified']) != (stat.st_size, stat.st_mtime_ns):
                self.db.execute("DELETE FROM photos WHERE video_id=?", (identity,))
                self.db.execute('UPDATE sightings SET active=0 WHERE video_id=?', (identity,))
                self.db.execute("UPDATE videos SET status='pending', result=NULL, gallery_version=0, error='', size=?, modified=? WHERE id=?",
                                (stat.st_size, stat.st_mtime_ns, identity))
            self.db.execute("INSERT OR IGNORE INTO videos(id,path,name,size,modified) VALUES(?,?,?,?,?)",
                            (identity, stored, original_name, stat.st_size, stat.st_mtime_ns))
        return identity

    def videos(self):
        return self.db.execute("""SELECT v.*, COALESCE(p.photos, 0) AS photos
            FROM videos v LEFT JOIN
                (SELECT video_id, count(*) AS photos FROM photos GROUP BY video_id) p
            ON p.video_id=v.id ORDER BY v.name DESC""").fetchall()

    def state(self, identity, status, error=""):
        with self.db:
            self.db.execute("UPDATE videos SET status=?,error=? WHERE id=?", (status, error, identity))

    def species(self):
        return self.db.execute("SELECT species,count(*) AS photos,count(DISTINCT video_id) AS videos FROM photos GROUP BY species ORDER BY species").fetchall()

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
        relative = Path("results") / (identity + "-" + uuid.uuid4().hex)
        folder = self.root / relative
        folder.mkdir(parents=True)
        capture = cv2.VideoCapture(str(video))
        photos = []
        groups = collect_sightings(result['frames'])
        source_signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        old = [dict(item) for item in self.db.execute('SELECT * FROM sightings WHERE video_id=? AND source_signature=?',
                                                     (identity, source_signature))]
        for item in old:
            item['anchors'] = json.loads(item['anchors'])
        matched = sighting_matches(groups, old)
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
                        filename = f"{view['frame']}-{animal['id']}.jpg"
                        view['path'] = str(relative / filename)
                        # Bound queued image memory independently of clip length.
                        if len(pending) >= workers*2:
                            finish_one()
                        pending.append((view, pool.submit(encode_photo, crop.copy(), animal['box'], folder/filename, cancel)))
                while pending:
                    check_cancel(cancel)
                    finish_one()
            for group in groups:
                group['views'] = [v for v in group['views'] if 'path' in v]
                group['evidence'] = summarize(group['views'], NONBIRD_LABELS)
                featured = best_photos(group['views'])
                for view in group['views']:
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
                self.db.execute("UPDATE videos SET status='ready',error='',result=?,gallery_version=? WHERE id=?",
                                (str(relative / "analysis.json.gz"), SMART_VERSION, identity))
            committed = True
        finally:
            capture.release()
            if not committed:
                resolved = folder.resolve()
                if resolved.is_relative_to((self.root / 'results').resolve()):
                    shutil.rmtree(resolved)
