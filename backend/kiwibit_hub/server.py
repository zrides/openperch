"""HTTP API for the hub: library browsing, corrections, jobs and live job events.

Run: uvicorn --factory kiwibit_hub.server:build_app --host 0.0.0.0 --port 8000
Interactive API documentation is served at /docs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.error
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .auth import SESSION_COOKIE, SESSION_SECONDS, Credentials, LoginLimiter
from .camera import CameraService
from .catalog import Catalog
from .jobs import JobRunner
from .kiwibit_api import KiwibitError
from .live import LiveError, LiveManager

# Downloaded recordings are named <UTC start>_<digest>.mp4, e.g. 20260925T185753Z_3a88a9623053ee38.mp4.
RECORDED_AT = re.compile(r'^(\d{8}T\d{6}Z)_')
CLOUD_ERRORS = (OSError, KiwibitError, ValueError, KeyError)

log = logging.getLogger('kiwibit_hub.server')

# Jobs the API may start. 'import' is excluded: it takes arbitrary server-side paths.
API_ACTIONS = {'scan', 'process', 'selected', 'reanalyze', 'fetch', 'verify', 'upgrade'}


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


class JobRequest(BaseModel):
    action: str
    ids: list[str] = Field(default_factory=list)


class CorrectionRequest(BaseModel):
    # null restores the AI identification.
    label: str | None


class LiveStart(BaseModel):
    resolution: str = '1280x720'


class LiveOffer(BaseModel):
    sdp: str


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


def recorded_at(name):
    match = RECORDED_AT.match(name)
    if not match:
        return None
    return datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).isoformat()


def create_app(runner=None, credentials=None, library=None, media_root=None, diagnostics=True,
               live=None, camera=None, web=None, limiter=None):
    library = Path(library) if library is not None else config.LIBRARY
    media_root = Path(media_root) if media_root is not None else config.DATA
    web = Path(web) if web is not None else config.WEB
    hub = EventHub()
    limiter = limiter or LoginLimiter()

    def signed_in(headers, cookies):
        if credentials is None:
            return 'local'
        return credentials.verify(cookies.get(SESSION_COOKIE)) or (
            credentials.user if credentials.check(headers.get('authorization')) else None)

    @asynccontextmanager
    async def lifespan(app):
        if diagnostics:
            from .diagnostics import install
            install()
        startup = Catalog(library, media_root)
        startup.recover()
        startup.close()
        app.state.runner = runner or JobRunner(library, media_root=media_root)
        app.state.live = live or LiveManager()
        app.state.live.listener = lambda status: hub.publish('live', **status)
        app.state.camera = camera or CameraService()
        hub.loop = asyncio.get_running_loop()
        unsubscribe = app.state.runner.subscribe(hub.publish)
        log.info('Hub started: library=%s', library)
        try:
            yield
        finally:
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
        if protected and path not in PUBLIC_API and not signed_in(request.headers, request.cookies):
            # No WWW-Authenticate header: browsers show the app's sign-in page, not a popup.
            return JSONResponse({'detail': 'Sign in required'}, status_code=401)
        return await call_next(request)

    @app.get('/api/session')
    def session(request: Request):
        user = signed_in(request.headers, request.cookies)
        return {'authenticated': user is not None, 'user': user, 'auth_required': credentials is not None}

    @app.post('/api/login')
    def login(body: LoginRequest, request: Request):
        if credentials is None:
            return {'user': 'local'}
        client = request.client.host if request.client else 'unknown'
        wait = limiter.blocked(client)
        if wait:
            raise HTTPException(429, f'Too many failed attempts. Try again in {wait} seconds.',
                                headers={'Retry-After': str(wait)})
        if not credentials.verify_password(body.username.strip(), body.password):
            limiter.failure(client)
            log.warning('Failed sign-in from %s', client)
            raise HTTPException(401, 'Wrong username or password')
        limiter.success(client)
        log.info('Signed in from %s', client)
        response = JSONResponse({'user': credentials.user})
        response.set_cookie(SESSION_COOKIE, credentials.issue(), max_age=SESSION_SECONDS if body.remember else None,
                            httponly=True, samesite='lax', secure=request.url.scheme == 'https', path='/')
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

    def live_of(request: Request) -> LiveManager:
        return request.app.state.live

    def camera_of(request: Request) -> CameraService:
        return request.app.state.camera

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
        return {'recordings': len(rows), 'by_status': counts, 'species': len(species),
                'photos': sum(row['photos'] for row in species),
                'unmatched_corrections': len(db.unmatched_corrections()), 'job': runner.state}

    @app.get('/api/recordings')
    def recordings(q: str = '', limit: int = 0, db: Catalog = Depends(catalog)):
        text = q.casefold()
        species = db.video_species()
        rows = [{**{key: row[key] for key in ('id', 'name', 'status', 'error', 'photos', 'added', 'size')},
                 'recorded_at': recorded_at(row['name']), 'species': species.get(row['id'], [])}
                for row in db.videos() if text in (row['name'] + ' ' + row['status']).casefold()]
        return rows[:limit] if limit > 0 else rows

    @app.get('/api/recordings/{identity}/video')
    def video(identity: str, db: Catalog = Depends(catalog)):
        path = db.video_path(video_row(db, identity))
        if not path.is_file():
            raise HTTPException(404, 'Video file is missing')
        return FileResponse(path, media_type='video/mp4')

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
        return [{key: row[key] for key in ('video_id', 'name', 'species', 'ms', 'track', 'similarity', 'path',
                                           'sighting_id', 'quality', 'featured', 'start_ms', 'end_ms',
                                           'evidence', 'ai_species', 'corrected')} for row in rows]

    @app.get('/api/photos/{path:path}')
    def photo(path: str):
        results = (library / 'results').resolve()
        target = (library / path).resolve()
        if not target.is_relative_to(results) or target.suffix.lower() != '.jpg' or not target.is_file():
            raise HTTPException(404, 'Photo not found')
        return FileResponse(target, media_type='image/jpeg')

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
    def start_job(body: JobRequest, runner: JobRunner = Depends(runner_of)):
        if body.action not in API_ACTIONS:
            raise HTTPException(422, f'action must be one of {sorted(API_ACTIONS)}')
        if body.action in ('selected', 'reanalyze') and not body.ids:
            raise HTTPException(422, 'Select at least one recording')
        try:
            runner.start(body.action, body.ids)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runner.state

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

    @app.post('/api/live/stop')
    def live_stop(live: LiveManager = Depends(live_of)):
        return live.stop()

    @app.websocket('/api/events')
    async def events(websocket: WebSocket):
        if not same_origin(websocket.headers) or not signed_in(websocket.headers, websocket.cookies):
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
    return create_app(credentials=Credentials.from_env())
