import base64
import os
import shutil
import subprocess
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


FFMPEG = shutil.which('ffmpeg')


def make_h264(folder):
    """A 3-second Annex-B H264 test stream, or None when ffmpeg/libx264 are unavailable."""
    if not FFMPEG:
        return None
    target = Path(folder) / 'test.h264'
    result = subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10', '-t', '3',
                             '-c:v', 'libx264', '-g', '5', '-f', 'h264', str(target)], capture_output=True)
    return target if result.returncode == 0 and target.stat().st_size else None


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stream = make_h264(self.temp.name)
        if not self.stream:
            self.skipTest('ffmpeg with libx264 is not available')
        self.recorded = []
        self.live = LiveManager(FAKE, on_recorded=lambda path: self.recorded.append(path) or 'rec-1',
                                recordings=Path(self.temp.name) / 'live')
        env = patch.dict(os.environ, {'FAKE_RELAY_STREAM': str(self.stream)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.live.shutdown()
        self.temp.cleanup()

    def probe(self, path):
        result = subprocess.run([shutil.which('ffprobe'), '-v', 'error', '-show_entries', 'format=duration',
                                 '-of', 'csv=p=0', str(path)], capture_output=True, text=True)
        return float(result.stdout.strip() or 0)

    def test_record_and_stop_produces_a_library_clip(self):
        self.live.start('t', 's', '1280x720')
        status = self.live.start_recording()
        self.assertTrue(status['recording']['active'])
        with self.assertRaisesRegex(LiveError, 'Already recording'):
            self.live.start_recording()
        time.sleep(1.5)
        status = self.live.stop_recording()
        last = status['recording']['last']
        self.assertFalse(status['recording']['active'])
        self.assertEqual(last['error'], '')
        self.assertEqual(last['recording_id'], 'rec-1')
        clip = Path(self.temp.name) / 'live' / last['file']
        self.assertEqual(self.recorded, [clip])
        self.assertRegex(clip.name, r'^\d{8}T\d{6}Z_live\.mp4$', 'named like downloads so it sorts by time')
        self.assertGreater(self.probe(clip), 0.5)
        self.assertEqual(list(clip.parent.glob('*.part.mp4')), [])

    def test_ending_live_view_finishes_the_clip(self):
        self.live.start('t', 's', '1280x720')
        self.live.start_recording()
        time.sleep(1)
        self.live.stop()
        last = self.live.status()['recording']['last']
        self.assertTrue(last['file'])
        self.assertEqual(len(self.recorded), 1)

    def test_recording_requires_a_live_session(self):
        with self.assertRaisesRegex(LiveError, 'Start live view first'):
            self.live.start_recording()


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

    # Shapes as the live camera reports them.
    def device_attributes(self, serial):
        return {'modifiableAttributes': [
            {'name': 'pirSwitch', 'value': True, 'disabled': False, 'type': 'SWITCH'},
            {'name': 'pirSensitivity', 'value': 'high', 'options': ['high', 'mid', 'low'], 'disabledOptions': [], 'type': 'ENUM'},
            {'name': 'pirRecordTime', 'value': '60s', 'options': ['10s', '15s', '20s', '60s'],
             'disabledOptions': ['120s', '180s'], 'type': 'ENUM'},
            {'name': 'location', 'type': 'OBJECT_ENUM', 'value': {'id': '1', 'text': 'Backyard'},
             'options': [{'id': '1', 'text': 'Backyard'}, {'id': '2', 'text': 'Front door'}]},
            {'name': 'voiceVolume', 'type': 'INT_RANGE', 'value': 100},
            {'name': 'deviceName', 'type': 'TEXT', 'value': 'Birdy McFeedface'},
            {'name': 'criticalAlerts', 'type': 'SWITCH', 'value': False},
            {'name': 'criticalAlerts', 'type': 'SWITCH', 'value': True},
            {'name': 'chargeAutoPowerOnCapacity', 'type': 'ENUM', 'value': '10pct', 'options': ['10pct']},
            {'name': 'newFeature', 'value': 3, 'type': 'NUMBER'},
        ]}

    def bird_ai_setting(self, serial):
        return {'aiNotifySwitch': True, 'aiAnalyzeSwitch': True}

    def activity_zones(self, serial):
        raise OSError('temporarily unavailable')

    def device(self, serial):
        return {'wifiPowerLevel': 5, 'wifiPowerDbm': None, 'signalStrength': -61, 'signalLevel': 3}


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

    def test_camera_settings_are_read_grouped_and_described(self):
        settings = self.client.get('/api/camera/settings').json()
        self.assertFalse(settings['writable'])
        self.assertEqual((settings['status']['wifi_dbm'], settings['status']['wifi_level']), (-61, 3))
        groups = {g['title']: {x['name']: x for x in g['settings']} for g in settings['groups']}
        self.assertEqual(list(groups), ['Motion and recording', 'Sound and alarm', 'AI and notifications',
                                        'Power and network', 'Device', 'Other'])
        motion = groups['Motion and recording']
        self.assertEqual(motion['pirSwitch']['label'], 'Motion detection')
        self.assertEqual([o['label'] for o in motion['pirSensitivity']['options']], ['High', 'Medium', 'Low'])
        self.assertEqual([(o['value'], o['disabled']) for o in motion['pirRecordTime']['options']],
                         [('10s', False), ('15s', False), ('20s', False), ('60s', False), ('120s', True), ('180s', True)])
        location = groups['Device']['location']
        self.assertEqual((location['type'], location['value']), ('ENUM', '1'))
        self.assertEqual([o['label'] for o in location['options']], ['Backyard', 'Front door'])
        self.assertEqual(groups['Device']['deviceName']['value'], 'Birdy McFeedface')
        self.assertEqual(groups['Sound and alarm']['voiceVolume']['type'], 'INT_RANGE')
        self.assertIs(groups['AI and notifications']['criticalAlerts']['value'], False, 'duplicates: first wins')
        self.assertEqual(groups['Power and network']['chargeAutoPowerOnCapacity']['options'][0]['label'], '10%')
        self.assertEqual(groups['Other']['newFeature']['label'], 'newFeature', 'unknown settings still appear')
        self.assertEqual(settings['bird_ai'], {'analyze': True, 'notify': True})
        self.assertIsNone(settings['activity_zones'], 'a failing section degrades instead of failing the page')

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
                             ('get', '/api/camera')]:
            self.assertEqual(getattr(anonymous, method)(path).status_code, 401, path)

    def test_web_app_is_served_after_api_routes(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Kiwibit Hub', page.text)
        self.assertEqual(self.client.get('/api/health').json(), {'status': 'ok'})


if __name__ == '__main__':
    unittest.main()
