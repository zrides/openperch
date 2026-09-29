"""HTTP API for the hub: library browsing, corrections, jobs and live job events.

Run: uvicorn --factory kiwibit_hub.server:build_app --host 0.0.0.0 --port 8000
Interactive API documentation is served at /docs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, review
from .auth import SESSION_COOKIE, SESSION_SECONDS, Credentials, LoginLimiter
from .camera import CameraService
from .catalog import Catalog
from .downloader import read_manifest
from .engine import read_labels
from .jobs import JobRunner, from_sdcard
from .kiwibit_api import KiwibitError
from .live import LiveError, LiveManager
from .overlap import coverage, estimate_offset, recorded_at, start_seconds
from .scheduler import FetchScheduler, run_forever
from .sightings import SMART_VERSION

CLOUD_ERRORS = (OSError, KiwibitError, ValueError, KeyError)

log = logging.getLogger('kiwibit_hub.server')

# Jobs the API may start. 'import' is excluded: it takes arbitrary server-side paths.
API_ACTIONS = {'scan', 'process', 'selected', 'reanalyze', 'reanalyze_all', 'fetch', 'verify', 'upgrade'}


# Reachable without signing in: the login itself and the web app's static files.
PUBLIC_API = {'/api/health', '/api/login', '/api/logout', '/api/session'}
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


def same_origin(headers):
    """Reject cross-site requests: a present Origin must match the host the browser used."""
    origin = headers.get('origin')
    if not origin:
        return True  # not a browser cross-origin request (curl, same-origin GET)
    netloc = urllib.parse.urlsplit(origin).netloc
    return netloc in {headers.get('host'), headers.get('x-forwarded-host')}


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = True


class NewAccount(BaseModel):
    name: str
    password: str
    role: str = 'member'


class AccountChange(BaseModel):
    # Either or both; omitted fields stay as they are.
    role: str | None = None
    password: str | None = None


class PasswordChange(BaseModel):
    current: str
    new: str


class JobRequest(BaseModel):
    action: str
    ids: list[str] = Field(default_factory=list)


class CorrectionRequest(BaseModel):
    # null restores the AI identification.
    label: str | None


class ReviewAnswer(BaseModel):
    # species, not_bird, several or unsure; null clears the answer.
    verdict: str | None
    species: str | None = None


class LiveStart(BaseModel):
    resolution: str = '1280x720'


class LiveOffer(BaseModel):
    sdp: str


class FetchSettings(BaseModel):
    auto: bool | None = None
    interval_minutes: int | None = None


class AlwaysShow(BaseModel):
    value: bool


class ProcessingSettings(BaseModel):
    detector_batch: int
    classifier_batch: int


class EventHub:
    """Fans job events from the worker thread out to WebSocket clients on the event loop."""

    def __init__(self):
        self.loop = None
        self.queues = set()

    def publish(self, kind, **data):
        if self.loop is None:
            return
        event = {'kind': kind, **data}
        for queue in list(self.queues):
            self.loop.call_soon_threadsafe(self._offer, queue, event)

    @staticmethod
    def _offer(queue, event):
        if queue.full():
            # A slow client loses the oldest progress update, never blocks the job.
            queue.get_nowait()
        queue.put_nowait(event)


def create_app(runner=None, credentials=None, library=None, media_root=None, diagnostics=True,
               live=None, camera=None, web=None, limiter=None, schedule_period=None):
    library = Path(library) if library is not None else config.LIBRARY
    media_root = Path(media_root) if media_root is not None else config.DATA
    web = Path(web) if web is not None else config.WEB
    hub = EventHub()
    limiter = limiter or LoginLimiter()

    def add_clip(app, path):
        """A clip recorded from live view joins the library and is identified when the hub is free."""
        db = Catalog(library, media_root)
        try:
            identity = db.ingest(path, origin='live')
        finally:
            db.close()
        hub.publish('changed')
        try:
            app.state.runner.start('selected', [identity])
        except RuntimeError:
            pass  # busy: it stays pending for the next process or fetch
        return identity

    def client_of(connection):
        return connection.client.host if connection.client else 'unknown'

    def signed_in(connection):
        """The account a request or WebSocket belongs to (session cookie or HTTP Basic), or None."""
        if credentials is None:
            return 'local'
        user = credentials.verify(connection.cookies.get(SESSION_COOKIE))
        header = connection.headers.get('authorization')
        if user or not header:
            return user
        # Scripts' Basic sign-ins count toward the same lockout as the sign-in page.
        client = client_of(connection)
        if limiter.blocked(client):
            return None
        user = credentials.check(header)
        if user is None:
            limiter.failure(client)
            log.warning('Failed HTTP Basic sign-in from %s', client)
        return user

    def role_of(user):
        return 'admin' if credentials is None else credentials.role(user)

    @asynccontextmanager
    async def lifespan(app):
        if diagnostics:
            from .diagnostics import install
            install()
        startup = Catalog(library, media_root)
        startup.recover()
        startup.close()
        app.state.runner = runner or JobRunner(library, media_root=media_root)
        app.state.live = live or LiveManager(recordings=media_root / 'downloads' / 'live')
        app.state.live.listener = lambda status: hub.publish('live', **status)
        app.state.live.on_recorded = lambda path: add_clip(app, path)
        app.state.camera = camera or CameraService()
        hub.loop = asyncio.get_running_loop()
        unsubscribe = app.state.runner.subscribe(hub.publish)
        app.state.scheduler = FetchScheduler(app.state.runner, library / 'fetch-schedule.json')
        stop_schedule = threading.Event()
        if schedule_period:
            threading.Thread(target=run_forever, args=(app.state.scheduler, stop_schedule, schedule_period),
                             name='fetch-schedule', daemon=True).start()
        log.info('Hub started: library=%s', library)
        try:
            yield
        finally:
            stop_schedule.set()
            unsubscribe()
            await asyncio.to_thread(app.state.live.shutdown)
            await asyncio.to_thread(app.state.runner.shutdown, 60)
            log.info('Hub stopped')

    app = FastAPI(title='Kiwibit Hub', version='0.1.0', lifespan=lifespan)

    @app.middleware('http')
    async def guard(request: Request, call_next):
        if request.method not in SAFE_METHODS and not same_origin(request.headers):
            return JSONResponse({'detail': 'Cross-site request refused'}, status_code=403)
        path = request.url.path
        protected = path.startswith('/api/') or path in ('/docs', '/redoc', '/openapi.json')
        if protected and path not in PUBLIC_API:
            request.state.user = signed_in(request)
            if not request.state.user:
                # No WWW-Authenticate header: browsers show the app's sign-in page, not a popup.
                return JSONResponse({'detail': 'Sign in required'}, status_code=401)
        return await call_next(request)

    @app.get('/api/session')
    def session(request: Request):
        user = signed_in(request)
        return {'authenticated': user is not None, 'user': user, 'role': role_of(user) if user else None,
                'owner': credentials is None or (user is not None and credentials.is_owner(user)),
                'auth_required': credentials is not None}

    @app.post('/api/login')
    def login(body: LoginRequest, request: Request):
        if credentials is None:
            return {'user': 'local'}
        client = request.client.host if request.client else 'unknown'
        wait = limiter.blocked(client)
        if wait:
            raise HTTPException(429, f'Too many failed attempts. Try again in {wait} seconds.',
                                headers={'Retry-After': str(wait)})
        user = credentials.authenticate(body.username.strip(), body.password)
        if user is None:
            limiter.failure(client)
            log.warning('Failed sign-in from %s', client)
            raise HTTPException(401, 'Wrong username or password')
        limiter.success(client)
        log.info('%s signed in from %s', user, client)
        response = JSONResponse({'user': user})
        set_session(response, request, user, body.remember)
        return response

    def set_session(response, request, user, remember=True):
        response.set_cookie(SESSION_COOKIE, credentials.issue(user), max_age=SESSION_SECONDS if remember else None,
                            httponly=True, samesite='lax', secure=request.url.scheme == 'https', path='/')

    def admin(request: Request):
        """The signed-in admin, for managing accounts."""
        if credentials is None:
            raise HTTPException(409, 'Sign-in is turned off on this hub')
        user = request.state.user
        if role_of(user) != 'admin':
            raise HTTPException(403, 'Only admins can manage accounts')
        return user

    def accounts():
        added = credentials.users.all() if credentials.users else []
        return ([{'name': credentials.user, 'role': 'admin', 'owner': True, 'created': None}] +
                [{'name': a['name'], 'role': a['role'], 'owner': False, 'created': a['created']}
                 for a in sorted(added, key=lambda a: a['name'].casefold())])

    @app.get('/api/users')
    def list_accounts(user: str = Depends(admin)):
        return accounts()

    @app.post('/api/users', status_code=201)
    def add_account(body: NewAccount, user: str = Depends(admin)):
        name = body.name.strip()
        if credentials.users is None:
            raise HTTPException(409, 'This hub keeps no added accounts')
        if name.casefold() == credentials.user.casefold():
            raise HTTPException(409, 'That is the owner account')
        try:
            credentials.users.add(name, body.password, body.role)
        except KeyError as exc:
            raise HTTPException(409, f'{name} already has an account') from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info('Account %s (%s) added by %s', name, body.role, user)
        return accounts()

    @app.put('/api/users/{name}')
    def change_account(name: str, body: AccountChange, user: str = Depends(admin)):
        if credentials.is_owner(name):
            raise HTTPException(409, "The owner account is set in the hub's .env file")
        if body.role not in (None, 'admin') and name.casefold() == user.casefold():
            raise HTTPException(409, "You can't take away your own admin role")
        try:
            credentials.users.update(name, password=body.password, role=body.role)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info('Account %s changed by %s (%s)', name, user,
                 ', '.join(part for part, given in (('role', body.role), ('password', body.password)) if given))
        return accounts()

    @app.delete('/api/users/{name}')
    def remove_account(name: str, user: str = Depends(admin)):
        if credentials.is_owner(name):
            raise HTTPException(409, "The owner account is set in the hub's .env file")
        if name.casefold() == user.casefold():
            raise HTTPException(409, "You can't remove your own account")
        try:
            credentials.users.remove(name)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        log.info('Account %s removed by %s', name, user)
        return accounts()

    @app.put('/api/account/password')
    def change_own_password(body: PasswordChange, request: Request):
        """Any added account may change its own password; other devices are signed out."""
        user = request.state.user
        if credentials is None or credentials.is_owner(user):
            raise HTTPException(409, "The owner's password is set in the hub's .env file")
        client = client_of(request)
        if limiter.blocked(client) or credentials.authenticate(user, body.current) is None:
            limiter.failure(client)
            raise HTTPException(403, 'Your current password is wrong')
        try:
            credentials.users.update(user, password=body.new)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        log.info('%s changed their password', user)
        response = JSONResponse({'user': user})
        set_session(response, request, user)  # this device stays signed in
        return response

    @app.post('/api/logout')
    def logout():
        response = JSONResponse({'authenticated': False})
        response.delete_cookie(SESSION_COOKIE, path='/')
        return response

    def catalog():
        # One connection per request; FastAPI may run a sync dependency and its
        # endpoint on different pool threads, hence check_same_thread=False.
        db = Catalog(library, media_root, check_same_thread=False, migrate=False)
        try:
            yield db
        finally:
            db.close()

    def runner_of(request: Request) -> JobRunner:
        return request.app.state.runner

    def scheduler_of(request: Request) -> FetchScheduler:
        return request.app.state.scheduler

    def live_of(request: Request) -> LiveManager:
        return request.app.state.live

    def camera_of(request: Request) -> CameraService:
        return request.app.state.camera

    manifest_cache = {'mtime': None, 'durations': {}, 'cameras': 0}

    def manifest():
        """Clip lengths and the number of cameras from the download manifest, reloaded when it changes."""
        path = media_root / 'downloads' / 'download_manifest.jsonl'
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            return {'durations': {}, 'cameras': 0}
        if manifest_cache['mtime'] != mtime:
            records = read_manifest(path)
            manifest_cache['durations'] = {'downloads/' + name: record.get('durationSeconds')
                                           for name, record in records.items()}
            manifest_cache['cameras'] = len({record.get('serialNumber') for record in records.values()} - {None})
            manifest_cache['mtime'] = mtime
        return manifest_cache

    def durations():
        return manifest()['durations']

    def card_copies(db, rows):
        """({cloud id: covering card clip}, summary): cloud clips whose moment the SD card also holds."""
        info = manifest()
        lengths = info['durations']
        cloud = [(row['id'], start_seconds(row['name']), lengths.get(row['path']))
                 for row in rows if row['origin'] == 'cloud']
        cards = {row['id']: row for row in rows if row['origin'] == 'sdcard' and (row['duration'] or 0) > 0}
        spans = [(identity, start_seconds(row['name']) or row['started'], row['duration'])
                 for identity, row in cards.items()]
        offset, pairs = estimate_offset([start for _, start, _ in cloud if start is not None],
                                        [start for _, start, _ in spans if start is not None])
        summary = {'offset_seconds': round(offset, 2), 'pairs': pairs, 'several_cameras': info['cameras'] > 1}
        if summary['several_cameras']:
            # Card clips carry no camera; with several cameras a match could be another camera's.
            return {}, summary
        covered = coverage(cloud, spans, offset)
        # Only a card file that is still there, unchanged since it was measured, may stand in.
        gone = set()
        for identity in {cover['id'] for cover in covered.values()}:
            row = cards[identity]
            try:
                stat = db.video_path(row).stat()
                if (stat.st_size, stat.st_mtime_ns) != (row['size'], row['modified']):
                    gone.add(identity)
            except OSError:
                gone.add(identity)
        if gone:
            covered = coverage(cloud, [span for span in spans if span[0] not in gone], offset)
        return covered, summary

    def video_row(db, identity):
        row = db.db.execute('SELECT * FROM videos WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Recording not found')
        return row

    @app.get('/api/health')
    def health():
        return {'status': 'ok'}

    @app.get('/api/status')
    def status(db: Catalog = Depends(catalog), runner: JobRunner = Depends(runner_of)):
        rows = db.videos()
        species = db.species()
        counts = {}
        for row in rows:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        card = [row for row in rows if from_sdcard(row)]
        return {'recordings': len(rows), 'by_status': counts, 'species': len(species),
                'sdcard': {'recordings': len(card), 'waiting': sum(row['status'] != 'ready' for row in card)},
                # Analyzed with older tracking or sighting rules; the 'upgrade' job brings them up to date.
                'outdated': sum(row['status'] == 'ready' and row['gallery_version'] < SMART_VERSION for row in rows),
                'photos': sum(row['photos'] for row in species),
                'unmatched_corrections': len(db.unmatched_corrections()), 'job': runner.state}

    @app.get('/api/recordings')
    def recordings(q: str = '', limit: int = 0, db: Catalog = Depends(catalog)):
        text = q.casefold()
        species = db.video_species()
        animals = db.video_animals()
        covers = db.covers()
        lengths = durations()
        videos = db.videos()
        # Over the whole library, before any search: a search must not change what covers what.
        copies, _ = card_copies(db, videos)
        rows = [{**{key: row[key] for key in ('id', 'name', 'status', 'error', 'photos', 'added', 'size', 'origin')},
                 'recorded_at': recorded_at(row['name']), 'species': species.get(row['id'], []),
                 'animals': animals.get(row['id'], []),
                 'duration': lengths.get(row['path']) or row['duration'] or None, 'cover': covers.get(row['id']),
                 'covered_by': copies.get(row['id']), 'always_show': bool(row['always_show']),
                 'hidden': row['id'] in copies and not row['always_show']}
                for row in videos if text in (row['name'] + ' ' + row['status']).casefold()]
        # Newest recording first; imported files without a timestamp go last.
        rows.sort(key=lambda row: row['recorded_at'] or '', reverse=True)
        return rows[:limit] if limit > 0 else rows

    @app.get('/api/recordings/card-copies')
    def card_copy_summary(db: Catalog = Depends(catalog)):
        """How cloud clips were matched with SD-card clips: the measured clock offset and the count."""
        videos = db.videos()
        copies, summary = card_copies(db, videos)
        return {**summary, 'hidden': sum(row['id'] in copies and not row['always_show'] for row in videos)}

    @app.put('/api/recordings/{identity}/always-show')
    def always_show(identity: str, body: AlwaysShow, db: Catalog = Depends(catalog)):
        """Keep a cloud clip listed even when the SD card holds the same moment."""
        video_row(db, identity)
        db.set_always_show(identity, body.value)
        hub.publish('changed')
        return {'id': identity, 'always_show': body.value}

    @app.get('/api/recordings/{identity}/sightings')
    def recording_sightings(identity: str, db: Catalog = Depends(catalog)):
        video_row(db, identity)
        return [{**dict(row), 'evidence': json.loads(row['evidence'])} for row in db.sightings(identity)]

    @app.get('/api/labels')
    def labels():
        """Candidate species names, for corrections."""
        try:
            return read_labels(config.SPECIES)
        except (OSError, ValueError) as exc:
            raise HTTPException(500, 'The species list could not be read') from exc

    @app.get('/api/recordings/{identity}/video')
    def video(identity: str, download: bool = False, db: Catalog = Depends(catalog)):
        row = video_row(db, identity)
        path = db.video_path(row)
        if not path.is_file():
            raise HTTPException(404, 'Video file is missing')
        return FileResponse(path, media_type='video/mp4', filename=row['name'] if download else None,
                            content_disposition_type='attachment' if download else 'inline')

    @app.get('/api/recordings/{identity}/analysis')
    def analysis(identity: str, db: Catalog = Depends(catalog)):
        video_row(db, identity)
        try:
            result = db.result(identity)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if result is None:
            raise HTTPException(404, 'This recording has not been processed yet')
        return result

    @app.get('/api/species')
    def species(db: Catalog = Depends(catalog)):
        return [dict(row) for row in db.species()]

    @app.get('/api/species/{name}/photos')
    def photos(name: str, offset: int = 0, limit: int = 100, include_duplicates: bool = False,
               db: Catalog = Depends(catalog)):
        limit = max(1, min(200, limit))
        rows = db.photos(name, max(0, offset), limit, best_only=not include_duplicates)
        return [{**{key: row[key] for key in ('video_id', 'name', 'species', 'ms', 'track', 'similarity', 'path',
                                              'sighting_id', 'quality', 'featured', 'start_ms', 'end_ms',
                                              'ai_species', 'corrected')},
                 'evidence': json.loads(row['evidence']) if row['evidence'] else None,
                 'recorded_at': recorded_at(row['name'])} for row in rows]

    @app.get('/api/photos/{path:path}')
    def photo(path: str, size: str = 'full', db: Catalog = Depends(catalog)):
        results = (library / 'results').resolve()
        target = (library / path).resolve()
        if not target.is_relative_to(results) or target.suffix.lower() != '.jpg' or not target.is_file():
            raise HTTPException(404, 'Photo not found')
        if size == 'thumb':
            try:
                target = db.thumbnail(target.relative_to(library.resolve()).as_posix())
            except (OSError, ValueError):
                pass  # fall back to the full photo
        # Result folders are immutable (a new analysis gets a new folder), so cache hard.
        return FileResponse(target, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @app.post('/api/sightings/{sighting_id}/correction')
    def correct(sighting_id: str, body: CorrectionRequest, db: Catalog = Depends(catalog)):
        try:
            db.correct_sighting(sighting_id, body.label)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'sighting_id': sighting_id, 'label': body.label}

    @app.get('/api/corrections/unmatched')
    def unmatched(db: Catalog = Depends(catalog)):
        return [dict(row) for row in db.unmatched_corrections()]

    @app.get('/api/review')
    def review_items(db: Catalog = Depends(catalog)):
        return [{**item, 'recorded_at': recorded_at(item['name'])} for item in review.items(db)]

    @app.post('/api/review', status_code=201)
    def review_build(db: Catalog = Depends(catalog)):
        try:
            review.build(db)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return review_items(db)

    @app.get('/api/review/score')
    def review_score(db: Catalog = Depends(catalog)):
        return review.score(db)

    @app.put('/api/review/{item_id}')
    def review_answer(item_id: str, body: ReviewAnswer, db: Catalog = Depends(catalog)):
        try:
            review.answer(db, item_id, body.verdict, body.species)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {'id': item_id, 'verdict': body.verdict, 'species': body.species}

    @app.get('/api/review/{item_id}/image')
    def review_image(item_id: str, db: Catalog = Depends(catalog)):
        try:
            path = review.image(db, item_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        # A moment's id never changes what it shows.
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=31536000, immutable'})

    @app.get('/api/settings/processing')
    def get_processing(runner: JobRunner = Depends(runner_of)):
        return runner.batch_settings()

    @app.put('/api/settings/processing')
    def put_processing(body: ProcessingSettings, runner: JobRunner = Depends(runner_of)):
        try:
            runner.configure_batches(body.detector_batch, body.classifier_batch)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.batch_settings()

    @app.get('/api/jobs')
    def job_state(runner: JobRunner = Depends(runner_of)):
        return runner.state

    @app.post('/api/jobs', status_code=202)
    def start_job(body: JobRequest, runner: JobRunner = Depends(runner_of),
                  scheduler: FetchScheduler = Depends(scheduler_of)):
        if body.action not in API_ACTIONS:
            raise HTTPException(422, f'action must be one of {sorted(API_ACTIONS)}')
        if body.action in ('selected', 'reanalyze') and not body.ids:
            raise HTTPException(422, 'Select at least one recording')
        if body.action == 'fetch':
            # Through the scheduler, so a manual fetch also resets the schedule.
            if not scheduler.start('manual'):
                raise HTTPException(409, 'Another library job is running')
            return runner.state
        try:
            runner.start(body.action, body.ids)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.state

    @app.get('/api/settings/fetch')
    def get_fetch_settings(scheduler: FetchScheduler = Depends(scheduler_of)):
        return scheduler.status()

    @app.put('/api/settings/fetch')
    def put_fetch_settings(body: FetchSettings, scheduler: FetchScheduler = Depends(scheduler_of)):
        try:
            return scheduler.configure(body.auto, body.interval_minutes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post('/api/jobs/cancel', status_code=202)
    def cancel_job(runner: JobRunner = Depends(runner_of)):
        runner.cancel()
        return runner.state

    @app.get('/api/camera')
    def camera_summary(refresh: bool = False, camera: CameraService = Depends(camera_of)):
        try:
            return camera.summary(refresh)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CLOUD_ERRORS as exc:
            log.warning('Camera lookup failed: %s', exc)
            raise HTTPException(502, 'Could not reach the Kiwibit service. Check the account token.') from exc

    @app.get('/api/camera/settings')
    def camera_settings(refresh: bool = False, camera: CameraService = Depends(camera_of)):
        try:
            return camera.settings(refresh)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CLOUD_ERRORS as exc:
            log.warning('Camera settings failed: %s', exc)
            raise HTTPException(502, 'Could not reach the Kiwibit service. Check the account token.') from exc

    @app.post('/api/camera/wake', status_code=202)
    def wake_camera(camera: CameraService = Depends(camera_of)):
        try:
            camera.wake()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CLOUD_ERRORS as exc:
            log.warning('Wake failed: %s', exc)
            raise HTTPException(502, 'The wake request failed') from exc
        return {'status': 'waking'}

    @app.get('/api/live')
    def live_status(live: LiveManager = Depends(live_of)):
        return live.status()

    @app.post('/api/live/start')
    def live_start(body: LiveStart, live: LiveManager = Depends(live_of), camera: CameraService = Depends(camera_of)):
        try:
            device = camera.summary()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CLOUD_ERRORS as exc:
            log.warning('Live start failed: %s', exc)
            raise HTTPException(502, 'Could not reach the Kiwibit service') from exc
        if not device['can_watch_live']:
            raise HTTPException(409, 'This camera does not allow live viewing')
        try:
            return live.start(camera.token(), device['serial'], body.resolution)
        except LiveError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/live/offer')
    def live_offer(body: LiveOffer, live: LiveManager = Depends(live_of)):
        try:
            return {'sdp': live.offer(body.sdp)}
        except LiveError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/live/record')
    def live_record(live: LiveManager = Depends(live_of)):
        try:
            return live.start_recording()
        except LiveError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/live/record/stop')
    def live_record_stop(live: LiveManager = Depends(live_of)):
        return live.stop_recording()

    @app.post('/api/live/stop')
    def live_stop(live: LiveManager = Depends(live_of)):
        return live.stop()

    @app.websocket('/api/events')
    async def events(websocket: WebSocket):
        if not same_origin(websocket.headers) or not signed_in(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = asyncio.Queue(maxsize=256)
        hub.queues.add(queue)
        try:
            await websocket.send_json({'kind': 'state', **websocket.app.state.runner.state})
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            hub.queues.discard(queue)

    if (web / 'index.html').is_file():
        # Mounted last so every /api route takes precedence.
        app.mount('/', StaticFiles(directory=web, html=True), name='web')

    return app


def build_app():
    """Entry point for `uvicorn --factory kiwibit_hub.server:build_app`; tests use create_app()."""
    return create_app(credentials=Credentials.from_env(), schedule_period=30)
