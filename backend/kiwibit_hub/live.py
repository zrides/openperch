"""Live view: supervise the Go relay that re-serves the camera's WebRTC stream.

The relay is started on demand (waking the camera) and ends by itself when no
browser has been watching for a while, so the battery camera can sleep again.
Browsers never talk to the relay's control port; their SDP offers pass through
the authenticated hub API.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger('kiwibit_hub.live')

RESOLUTIONS = ('1280x720', '1920x1080', '3840x2160')
MAX_RECORDING_SECONDS = 600


class LiveError(Exception):
    """A live-view request that cannot be fulfilled; the message is shown to the user."""


def find_relay():
    configured = os.environ.get('KIWIBIT_LIVE_RELAY')
    if configured:
        return configured
    for candidate in (shutil.which('kiwibit-relay'), config.REPO / 'live' / 'kiwibit-relay.exe',
                      config.REPO / 'live' / 'kiwibit-relay'):
        if candidate and os.path.isfile(candidate):
            return str(candidate)
    return None


class LiveManager:
    def __init__(self, relay=None, ready_timeout=15, listener=None, on_recorded=None, recordings=None):
        self.relay = relay
        self.ready_timeout = ready_timeout
        self.listener = listener or (lambda status: None)
        # Called with each finished clip's path; returns its library id (or None).
        self.on_recorded = on_recorded or (lambda path: None)
        self.recordings = Path(recordings) if recordings is not None else config.DOWNLOADS / 'live'
        self.lock = threading.RLock()
        self.process = None
        self.port = None
        self.ready = threading.Event()
        self.recorder = None
        self.status_data = {'state': 'idle', 'detail': '', 'resolution': None, 'viewers': 0, 'started': None,
                            'recording': {'active': False, 'started': None, 'last': None}}

    # ---- status -----------------------------------------------------------

    def status(self):
        with self.lock:
            return dict(self.status_data, running=self._running())

    def _running(self):
        return self.process is not None and self.process.poll() is None

    def _update(self, **changes):
        with self.lock:
            self.status_data.update(changes)
            snapshot = dict(self.status_data, running=self._running())
        self.listener(snapshot)

    # ---- lifecycle ----------------------------------------------------------

    def start(self, token, serial, resolution):
        if resolution not in RESOLUTIONS:
            raise LiveError(f'Resolution must be one of {", ".join(RESOLUTIONS)}')
        relay = self.relay or find_relay()
        if not relay:
            raise LiveError('The live relay (kiwibit-relay) is not installed')
        with self.lock:
            if self._running() and self.status_data['resolution'] == resolution:
                return self.status()
            if self._running():
                self._stop_locked()
            env = {**os.environ, 'KIWIBIT_TOKEN': token, 'KIWIBIT_SN': serial,
                   'KIWIBIT_RESOLUTION': resolution, 'KIWIBIT_RELAY_ADDR': '127.0.0.1:0'}
            self.ready = threading.Event()
            self.port = None
            self.status_data = {'state': 'starting', 'detail': '', 'resolution': resolution,
                                'viewers': 0, 'started': time.time(),
                                'recording': {'active': False, 'started': None,
                                              'last': self.status_data['recording']['last']}}
            log.info('Starting live relay at %s', resolution)
            command = list(relay) if isinstance(relay, (list, tuple)) else [relay]
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.process = process
            threading.Thread(target=self._read_events, args=(process,), name='live-events', daemon=True).start()
            threading.Thread(target=self._read_logs, args=(process,), name='live-logs', daemon=True).start()
        self.listener(self.status())
        if not self.ready.wait(self.ready_timeout):
            with self.lock:
                if self.process is process:
                    self._stop_locked()
            raise LiveError('The live relay did not start')
        with self.lock:
            if self.process is process and self.port is None:
                raise LiveError(self.status_data['detail'] or 'The live relay stopped while starting')
        return self.status()

    def _read_events(self, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get('event')
            if kind == 'listening':
                with self.lock:
                    if self.process is process:
                        self.port = int(event['port'])
                        self.ready.set()
            elif kind == 'state':
                self._update_if_current(process, state=event.get('state', ''), detail=event.get('detail', ''))
            elif kind == 'viewers':
                self._update_if_current(process, viewers=int(event.get('count', 0)))
        process.stdout.close()
        code = process.wait()
        with self.lock:
            current = self.process is process
            detail = self.status_data['detail'] if self.status_data['state'] == 'ended' else ''
        if current:
            self._update_if_current(process, state='ended', viewers=0,
                                    detail=detail or f'The live relay exited ({code})')
            log.info('Live relay ended: %s', detail or code)
            self.ready.set()  # release a start() still waiting for 'listening'


    def _update_if_current(self, process, **changes):
        with self.lock:
            if self.process is not process:
                return
        self._update(**changes)

    @staticmethod
    def _read_logs(process):
        relay_log = logging.getLogger('kiwibit_hub.live.relay')
        for line in process.stderr:
            if line.strip():
                relay_log.info('%s', line.rstrip())
        process.stderr.close()

    def offer(self, sdp):
        """Pass a browser's SDP offer to the relay and return its answer."""
        if not isinstance(sdp, str) or not sdp.lstrip().startswith('v=0') or len(sdp) > 64_000:
            raise LiveError('Expected an SDP offer')
        with self.lock:
            if not self._running() or self.port is None:
                raise LiveError('Start live view first')
            url = f'http://127.0.0.1:{self.port}/whep'
        request = urllib.request.Request(url, data=sdp.encode('utf-8'), method='POST',
                                         headers={'Content-Type': 'application/sdp'})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            raise LiveError(exc.read().decode('utf-8', errors='replace').strip() or 'The live relay refused the viewer') from exc
        except OSError as exc:
            raise LiveError('The live relay is not responding') from exc

    def stop(self):
        self.stop_recording()  # finish the clip before its source goes away
        with self.lock:
            self._stop_locked()
        return self.status()

    # ---- recording ----------------------------------------------------------

    def start_recording(self, max_seconds=MAX_RECORDING_SECONDS):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise LiveError('FFmpeg is needed to record clips')
        with self.lock:
            if not self._running() or self.port is None:
                raise LiveError('Start live view first')
            if self.recorder is not None:
                raise LiveError('Already recording')
            started = time.time()
            name = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(started)) + '_live'
            self.recordings.mkdir(parents=True, exist_ok=True)
            final = self.recordings / f'{name}.mp4'
            partial = self.recordings / f'{name}.part.mp4'
            stop = threading.Event()
            thread = threading.Thread(target=self._record, name='live-record',
                                      args=(self.port, ffmpeg, partial, final, stop, started + max_seconds))
            self.recorder = {'thread': thread, 'stop': stop}
            recording = {'active': True, 'started': started, 'last': self.status_data['recording']['last']}
        self._update(recording=recording)
        thread.start()
        log.info('Recording started')
        return self.status()

    def stop_recording(self, timeout=40):
        with self.lock:
            recorder = self.recorder
        if recorder is None:
            return self.status()
        recorder['stop'].set()
        recorder['thread'].join(timeout)
        return self.status()

    def _record(self, port, ffmpeg, partial, final, stop, deadline):
        error = ''
        # Raw H264 has no timestamps: stamp frames as they arrive (they arrive live).
        encoder = subprocess.Popen(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-use_wallclock_as_timestamps', '1',
             '-f', 'h264', '-i', 'pipe:0', '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', '-y', str(partial)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/stream.h264', timeout=5) as stream:
                while not stop.is_set() and time.time() < deadline:
                    chunk = stream.read1(65536)
                    if not chunk:
                        break  # the live session ended
                    encoder.stdin.write(chunk)
        except (OSError, ValueError) as exc:
            if not stop.is_set():
                error = 'The live stream ended while recording'
                log.info('Recording stream ended: %s', exc)
        finally:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        try:
            _, stderr = encoder.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            encoder.kill()
            _, stderr = encoder.communicate()
        recording_id = None
        if encoder.returncode == 0 and partial.is_file() and partial.stat().st_size > 0:
            partial.replace(final)
            error = ''
            try:
                recording_id = self.on_recorded(final)
            except Exception:
                log.exception('Could not add the recorded clip to the library')
        else:
            partial.unlink(missing_ok=True)
            detail = (stderr or b'').decode('utf-8', 'replace').strip().splitlines()
            error = error or ('No video was received' if not detail else f'Recording failed: {detail[-1][:160]}')
        last = {'file': final.name if final.exists() else None,
                'recording_id': recording_id, 'error': error, 'finished': time.time()}
        log.info('Recording finished: %s', error or final.name)
        with self.lock:
            self.recorder = None
        self._update(recording={'active': False, 'started': None, 'last': last})

    def _stop_locked(self):
        process, port = self.process, self.port
        if process is None or process.poll() is not None:
            return
        log.info('Stopping live relay')
        if port is not None:
            try:
                urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}/stop', method='POST'), timeout=3)
            except OSError:
                pass
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(3)
            except subprocess.TimeoutExpired:
                process.kill()

    def shutdown(self):
        self.stop()
