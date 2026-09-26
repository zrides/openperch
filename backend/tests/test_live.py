import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from kiwibit_hub.camera import CameraService
from kiwibit_hub.jobs import JobRunner
from kiwibit_hub.live import LiveError, LiveManager
from kiwibit_hub.server import Credentials, create_app

FAKE = [sys.executable, str(Path(__file__).with_name('fake_relay.py'))]
OFFER = 'v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n'


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class LiveManagerTests(unittest.TestCase):
    def setUp(self):
        self.statuses = []
        self.live = LiveManager(FAKE, listener=self.statuses.append)

    def tearDown(self):
        self.live.shutdown()

    def test_start_offer_and_stop(self):
        status = self.live.start('secret-token', 'serial-1', '1280x720')
        self.assertTrue(status['running'])
        self.assertTrue(wait_for(lambda: self.live.status()['state'] == 'streaming'))
        answer = self.live.offer(OFFER)
        self.assertTrue(answer.startswith('v=0'))
        self.assertIn('1280x720', answer)
        self.assertTrue(wait_for(lambda: self.live.status()['viewers'] == 1))
        status = self.live.stop()
        self.assertFalse(status['running'])
        self.assertTrue(wait_for(lambda: self.live.status()['state'] == 'ended'))
        self.assertIn('streaming', [s['state'] for s in self.statuses])

    def test_same_resolution_reuses_session_and_new_one_restarts(self):
        self.live.start('t', 's', '1280x720')
        first = self.live.process
        self.live.start('t', 's', '1280x720')
        self.assertIs(self.live.process, first)
        self.live.start('t', 's', '3840x2160')
        self.assertIsNot(self.live.process, first)
        self.assertIsNotNone(first.poll(), 'the previous relay must be stopped')
        self.assertEqual(self.live.status()['resolution'], '3840x2160')

    def test_token_reaches_relay_only_through_environment(self):
        with patch('kiwibit_hub.live.subprocess.Popen', wraps=__import__('subprocess').Popen) as popen:
            self.live.start('secret-token', 'serial-1', '1280x720')
        command = popen.call_args.args[0]
        self.assertNotIn('secret-token', ' '.join(command))
        self.assertEqual(popen.call_args.kwargs['env']['KIWIBIT_TOKEN'], 'secret-token')

    def test_relay_failure_reports_its_reason_quickly(self):
        started = time.monotonic()
        with patch.dict(os.environ, {'FAKE_RELAY_MODE': 'fail'}):
            with self.assertRaisesRegex(LiveError, 'token expired'):
                self.live.start('t', 's', '1280x720')
        self.assertLess(time.monotonic() - started, 5, 'must not wait for the full ready timeout')
        self.assertEqual(self.live.status()['state'], 'ended')

    def test_validation(self):
        with self.assertRaisesRegex(LiveError, 'Start live view first'):
            self.live.offer(OFFER)
        with self.assertRaisesRegex(LiveError, 'Resolution'):
            self.live.start('t', 's', '640x480')
        self.live.start('t', 's', '1280x720')
        with self.assertRaisesRegex(LiveError, 'SDP offer'):
            self.live.offer('<script>')

    def test_missing_relay(self):
        with patch('kiwibit_hub.live.find_relay', return_value=None):
            with self.assertRaisesRegex(LiveError, 'not installed'):
                LiveManager().start('t', 's', '1280x720')


class FakeClient:
    def __init__(self):
        self.woken = []

    def devices(self):
        return [{'serialNumber': 'other', 'deviceName': 'Other'},
                {'serialNumber': 'cam-1', 'deviceName': 'Birdy McFeedface', 'displayModelNo': 'Beako Smart Bird Feeder',
                 'modelNo': 'KF126A-P7G2', 'online': 1, 'awake': 0, 'batteryLevel': 100, 'firmwareId': '1.15.30',
                 'recResolution': '3840x2160', 'canWatchLive': True, 'userSn': 'not-exposed'}]

    def wake(self, serial):
        self.woken.append(serial)


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client_api = FakeClient()
        camera = CameraService(account={'token': 'Bearer secret-token', 'defaultSerialNumber': 'cam-1'},
                               client=self.client_api)
        self.live = LiveManager(FAKE)
        web = root / 'web'
        web.mkdir()
        (web / 'index.html').write_text('<!doctype html><title>Kiwibit Hub</title>')
        app = create_app(JobRunner(root / 'library', root / 'downloads', root), Credentials('kiwibit', 'pw'),
                         root / 'library', root, diagnostics=False, live=self.live, camera=camera, web=web)
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'kiwibit:pw').decode()}
        self.client = TestClient(app, headers=auth)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_camera_summary_is_curated(self):
        camera = self.client.get('/api/camera').json()
        self.assertEqual(camera['name'], 'Birdy McFeedface')
        self.assertEqual(camera['serial'], 'cam-1', 'the configured default camera is preferred')
        self.assertEqual((camera['battery'], camera['firmware'], camera['awake']), (100, '1.15.30', False))
        self.assertNotIn('userSn', camera)
        self.assertEqual(self.client.post('/api/camera/wake').status_code, 202)
        self.assertEqual(self.client_api.woken, ['cam-1'])

    def test_live_round_trip_through_api(self):
        self.assertEqual(self.client.get('/api/live').json()['state'], 'idle')
        self.assertEqual(self.client.post('/api/live/offer', json={'sdp': OFFER}).status_code, 409)
        started = self.client.post('/api/live/start', json={'resolution': '3840x2160'})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()['resolution'], '3840x2160')
        answer = self.client.post('/api/live/offer', json={'sdp': OFFER})
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.json()['sdp'].startswith('v=0'))
        with self.client.websocket_connect('/api/events') as socket:
            socket.receive_json()  # initial job state
            self.client.post('/api/live/stop')
            while True:
                event = socket.receive_json()
                if event['kind'] == 'live' and event['state'] == 'ended':
                    break
        self.assertFalse(self.client.get('/api/live').json()['running'])
        self.assertEqual(self.client.post('/api/live/start', json={'resolution': '800x600'}).status_code, 409)

    def test_live_requires_authentication(self):
        anonymous = TestClient(self.client.app)
        for method, path in [('get', '/api/live'), ('post', '/api/live/start'), ('post', '/api/live/offer'),
                             ('get', '/api/camera'), ('get', '/')]:
            self.assertEqual(getattr(anonymous, method)(path).status_code, 401, path)

    def test_web_app_is_served_after_api_routes(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Kiwibit Hub', page.text)
        self.assertEqual(self.client.get('/api/health').json(), {'status': 'ok'})


if __name__ == '__main__':
    unittest.main()
