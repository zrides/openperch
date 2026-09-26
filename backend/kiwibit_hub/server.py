"""HTTP API for the hub: library browsing, corrections, jobs and live job events.

Run: uvicorn --factory kiwibit_hub.server:build_app --host 0.0.0.0 --port 8000
Interactive API documentation is served at /docs.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config
from .catalog import Catalog
from .jobs import JobRunner

log = logging.getLogger('kiwibit_hub.server')

# Jobs the API may start. 'import' is excluded: it takes arbitrary server-side paths.
API_ACTIONS = {'scan', 'process', 'selected', 'reanalyze', 'fetch', 'verify', 'upgrade'}


class Credentials:
    """HTTP Basic credentials from KIWIBIT_HUB_USER / KIWIBIT_HUB_PASSWORD."""

    def __init__(self, user, password):
        self.user, self.password = user, password

    @classmethod
    def from_env(cls):
        password = os.environ.get('KIWIBIT_HUB_PASSWORD', '')
        if not password and os.environ.get('KIWIBIT_HUB_ALLOW_NO_AUTH') != '1':
            raise RuntimeError('Set KIWIBIT_HUB_PASSWORD (or KIWIBIT_HUB_ALLOW_NO_AUTH=1 for local development). '
                               'The hub can control the camera and holds the account token.')
        return cls(os.environ.get('KIWIBIT_HUB_USER', 'kiwibit'), password) if password else None

    def check(self, header):
        scheme, _, value = (header or '').partition(' ')
        if scheme.lower() != 'basic':
            return False
        try:
            user, _, password = base64.b64decode(value, validate=True).decode('utf-8').partition(':')
        except (binascii.Error, UnicodeDecodeError):
            return False
        # Evaluate both comparisons so timing does not reveal which one failed.
        user_ok = secrets.compare_digest(user.encode(), self.user.encode())
        password_ok = secrets.compare_digest(password.encode(), self.password.encode())
        return user_ok and password_ok


class JobRequest(BaseModel):
    action: str
    ids: list[str] = Field(default_factory=list)


class CorrectionRequest(BaseModel):
    # null restores the AI identification.
    label: str | None


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


def create_app(runner=None, credentials=None, library=None, media_root=None, diagnostics=True):
    library = Path(library) if library is not None else config.LIBRARY
    media_root = Path(media_root) if media_root is not None else config.DATA
    hub = EventHub()

    @asynccontextmanager
    async def lifespan(app):
        if diagnostics:
            from .diagnostics import install
            install()
        startup = Catalog(library, media_root)
        startup.recover()
        startup.close()
        app.state.runner = runner or JobRunner(library, media_root=media_root)
        hub.loop = asyncio.get_running_loop()
        unsubscribe = app.state.runner.subscribe(hub.publish)
        log.info('Hub started: library=%s', library)
        try:
            yield
        finally:
            unsubscribe()
            await asyncio.to_thread(app.state.runner.shutdown, 60)
            log.info('Hub stopped')

    app = FastAPI(title='Kiwibit Hub', version='0.1.0', lifespan=lifespan)

    if credentials is not None:
        @app.middleware('http')
        async def require_auth(request: Request, call_next):
            if request.url.path != '/api/health' and not credentials.check(request.headers.get('authorization')):
                return JSONResponse({'detail': 'Authentication required'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Basic realm="Kiwibit Hub"'})
            return await call_next(request)

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
    def recordings(q: str = '', db: Catalog = Depends(catalog)):
        text = q.casefold()
        return [{key: row[key] for key in ('id', 'name', 'status', 'error', 'photos', 'added', 'size')}
                for row in db.videos() if text in (row['name'] + ' ' + row['status']).casefold()]

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

    @app.websocket('/api/events')
    async def events(websocket: WebSocket):
        if credentials is not None and not credentials.check(websocket.headers.get('authorization')):
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

    return app


def build_app():
    """Entry point for `uvicorn --factory kiwibit_hub.server:build_app`; tests use create_app()."""
    return create_app(credentials=Credentials.from_env())
