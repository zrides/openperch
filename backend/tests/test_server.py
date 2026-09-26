import base64
import tempfile
import threading
import unittest
from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kiwibit_hub.catalog import Catalog
from kiwibit_hub.jobs import JobRunner
from kiwibit_hub.server import Credentials, create_app


def basic(user, password):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}


AUTH = basic('kiwibit', 'correct horse')


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        downloads = self.root / 'downloads'
        downloads.mkdir()
        self.video = downloads / 'bird.avi'
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        for _ in range(21):
            writer.write(np.full((48, 64, 3), 120, dtype=np.uint8))
        writer.release()
        db = Catalog(self.library, self.root)
        self.identity = db.ingest(self.video)
        animal = {'id': 1, 'category': 'bird', 'box': [.1, .1, .9, .9], 'detection': .9,
                  'ranked': [{'label': 'Northern cardinal', 'similarity': .4, 'score': .9},
                             {'label': 'Blue jay', 'similarity': .2, 'score': .1}]}
        self.result = {'complete': True, 'fps': 10, 'frames': [{'ms': n*100, 'animals': [dict(animal)]} for n in range(21)]}
        db.close()
        self.runner = JobRunner(self.library, downloads, self.root)
        app = create_app(self.runner, Credentials('kiwibit', 'correct horse'), self.library, self.root, diagnostics=False)
        self.client = TestClient(app, headers=AUTH)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def process(self):
        db = Catalog(self.library, self.root)
        db.save_analysis(self.identity, self.result, threading.Event())
        db.close()

    def test_everything_but_health_requires_credentials(self):
        anonymous = TestClient(self.client.app)
        self.assertEqual(anonymous.get('/api/health').status_code, 200)
        for path in ('/api/recordings', '/api/status', '/docs', '/openapi.json'):
            response = anonymous.get(path)
            self.assertEqual(response.status_code, 401, path)
            # No Basic challenge: browsers must show the sign-in page, not a popup.
            self.assertNotIn('www-authenticate', response.headers)
        self.assertEqual(anonymous.get('/api/recordings', headers=basic('kiwibit', 'wrong')).status_code, 401)
        self.assertEqual(anonymous.get('/api/recordings', headers={'Authorization': 'Basic !!!'}).status_code, 401)
        with self.assertRaises(WebSocketDisconnect):
            with anonymous.websocket_connect('/api/events') as socket:
                socket.receive_json()

    def test_server_refuses_to_start_without_a_password(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'KIWIBIT_HUB_PASSWORD': ''}, clear=False):
            os.environ.pop('KIWIBIT_HUB_ALLOW_NO_AUTH', None)
            with self.assertRaisesRegex(RuntimeError, 'KIWIBIT_HUB_PASSWORD'):
                Credentials.from_env()

    def test_recordings_and_video_range_requests(self):
        recordings = self.client.get('/api/recordings').json()
        self.assertEqual([(r['id'], r['name'], r['status']) for r in recordings], [(self.identity, 'bird.avi', 'pending')])
        self.assertEqual(self.client.get('/api/recordings', params={'q': 'nothing'}).json(), [])
        full = self.client.get(f'/api/recordings/{self.identity}/video')
        self.assertEqual(full.content, self.video.read_bytes())
        # Browsers seek with Range requests.
        part = self.client.get(f'/api/recordings/{self.identity}/video', headers={'Range': 'bytes=0-9'})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, self.video.read_bytes()[:10])
        self.assertEqual(self.client.get('/api/recordings/unknown/video').status_code, 404)

    def test_analysis_species_photos_and_corrections(self):
        self.assertEqual(self.client.get(f'/api/recordings/{self.identity}/analysis').status_code, 404)
        self.process()
        analysis = self.client.get(f'/api/recordings/{self.identity}/analysis').json()
        self.assertEqual(len(analysis['frames']), 21)
        self.assertIn('sighting_id', analysis['frames'][0]['animals'][0])
        species = self.client.get('/api/species').json()
        self.assertEqual(len(species), 1)
        name = species[0]['species']
        photos = self.client.get(f'/api/species/{name}/photos', params={'include_duplicates': True}).json()
        self.assertEqual(len(photos), 3)
        image = self.client.get(f"/api/photos/{photos[0]['path']}")
        self.assertEqual(image.headers['content-type'], 'image/jpeg')
        self.assertTrue(image.content.startswith(b'\xff\xd8'))

        sighting = photos[0]['sighting_id']
        response = self.client.post(f'/api/sightings/{sighting}/correction', json={'label': 'Purple finch'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['species'] for s in self.client.get('/api/species').json()], ['Purple finch'])
        overlay = self.client.get(f'/api/recordings/{self.identity}/analysis').json()['frames'][0]['animals'][0]
        self.assertEqual(overlay['display_label'], 'Purple finch')
        self.client.post(f'/api/sightings/{sighting}/correction', json={'label': None})
        self.assertEqual([s['species'] for s in self.client.get('/api/species').json()], [name])
        self.assertEqual(self.client.post('/api/sightings/missing/correction', json={'label': 'X'}).status_code, 409)
        self.assertEqual(self.client.post(f'/api/sightings/{sighting}/correction', json={'label': ''}).status_code, 409)

    def test_photo_endpoint_only_serves_gallery_jpegs(self):
        self.process()
        for path in ('library.sqlite3', '../downloads/bird.avi', 'results/../library.sqlite3',
                     '..%2F..%2Fsecret.jpg', 'results/nope.jpg'):
            self.assertEqual(self.client.get(f'/api/photos/{path}').status_code, 404, path)

    def test_jobs_validation_start_and_events(self):
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'import', 'ids': ['C:/Windows']}).status_code, 422)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'reanalyze'}).status_code, 422)
        with self.client.websocket_connect('/api/events') as socket:
            self.assertEqual(socket.receive_json(), {'kind': 'state', **self.runner.state})
            self.assertEqual(self.client.post('/api/jobs', json={'action': 'scan'}).status_code, 202)
            kinds = []
            while True:
                event = socket.receive_json()
                kinds.append(event['kind'])
                if event['kind'] == 'state' and not event['running']:
                    break
            self.assertEqual(kinds[0], 'state')
            self.assertIn('message', kinds)
        self.assertFalse(self.client.get('/api/jobs').json()['running'])

    def test_processing_settings(self):
        self.assertEqual(self.client.put('/api/settings/processing',
                                         json={'detector_batch': 8, 'classifier_batch': 16}).status_code, 200)
        settings = self.client.get('/api/settings/processing').json()
        self.assertEqual((settings['detector_batch'], settings['classifier_batch']), (8, 16))
        self.assertEqual(self.client.put('/api/settings/processing',
                                         json={'detector_batch': 5, 'classifier_batch': 16}).status_code, 422)

    def test_recording_details_for_the_player(self):
        import json as _json
        manifest = self.root / 'downloads' / 'download_manifest.jsonl'
        manifest.write_text(_json.dumps({'file': 'bird.avi', 'durationSeconds': 2.1}) + '\n')
        self.process()
        row = self.client.get('/api/recordings').json()[0]
        self.assertEqual(row['duration'], 2.1)
        self.assertEqual(row['species'], ['Northern cardinal'])
        self.assertTrue(row['cover'].endswith('.jpg'))
        self.assertEqual(self.client.get(f"/api/photos/{row['cover']}").status_code, 200)
        sightings = self.client.get(f'/api/recordings/{self.identity}/sightings').json()
        self.assertEqual(len(sightings), 1)
        sighting = sightings[0]
        self.assertEqual(sighting['ai_species'], 'Northern cardinal')
        self.assertIsNone(sighting['corrected'])
        self.assertEqual(sighting['photos'], 3)
        self.assertTrue(sighting['photo'].endswith('.jpg'))
        self.assertIsInstance(sighting['evidence'], dict)
        self.client.post(f"/api/sightings/{sighting['id']}/correction", json={'label': 'Summer tanager'})
        self.assertEqual(self.client.get(f'/api/recordings/{self.identity}/sightings').json()[0]['corrected'],
                         'Summer tanager')
        self.assertEqual(self.client.get('/api/recordings/unknown/sightings').status_code, 404)

    def test_gallery_rows_and_thumbnails(self):
        self.process()
        species = self.client.get('/api/species').json()[0]
        self.assertEqual((species['photos'], species['featured']), (3, 1))
        rows = self.client.get(f"/api/species/{species['species']}/photos").json()
        self.assertEqual(len(rows), 1, 'best photos only by default')
        row = rows[0]
        self.assertIsInstance(row['evidence'], dict)
        self.assertIn('recorded_at', row)
        full = self.client.get(f"/api/photos/{row['path']}")
        thumb = self.client.get(f"/api/photos/{row['path']}", params={'size': 'thumb'})
        self.assertEqual(thumb.headers['content-type'], 'image/jpeg')
        self.assertIn('immutable', thumb.headers['cache-control'])
        import cv2
        import numpy as np
        decoded = cv2.imdecode(np.frombuffer(thumb.content, np.uint8), cv2.IMREAD_COLOR)
        self.assertLessEqual(max(decoded.shape[:2]), 360)
        self.assertTrue(full.content.startswith(b'\xff\xd8'))
        cached = self.library / 'thumbs' / row['path']
        self.assertTrue(cached.is_file(), 'thumbnail is cached on disk')
        # Reprocessing replaces the results folder; its thumbnails go with it.
        self.process()
        self.assertFalse(cached.exists())

    def test_download_and_labels(self):
        download = self.client.get(f'/api/recordings/{self.identity}/video', params={'download': True})
        self.assertIn('attachment', download.headers['content-disposition'])
        self.assertIn('bird.avi', download.headers['content-disposition'])
        inline = self.client.get(f'/api/recordings/{self.identity}/video')
        self.assertNotIn('attachment', inline.headers.get('content-disposition', ''))
        labels = self.client.get('/api/labels').json()
        self.assertIn('Northern cardinal', labels)
        self.assertGreater(len(labels), 100)

    def test_fetch_settings_and_manual_fetch_go_through_the_schedule(self):
        settings = self.client.get('/api/settings/fetch').json()
        self.assertEqual((settings['auto'], settings['interval_minutes']), (True, 30))
        changed = self.client.put('/api/settings/fetch', json={'auto': False, 'interval_minutes': 60}).json()
        self.assertEqual((changed['auto'], changed['interval_minutes'], changed['next_run']), (False, 60, None))
        self.assertEqual(self.client.put('/api/settings/fetch', json={'interval_minutes': 5}).status_code, 422)
        from unittest.mock import patch as _patch
        with _patch.object(self.runner, 'start') as start:
            self.assertEqual(self.client.post('/api/jobs', json={'action': 'fetch'}).status_code, 202)
            start.assert_called_once_with('fetch')
        self.assertEqual(self.client.get('/api/settings/fetch').json()['last_trigger'], 'manual')

    def test_status_summary(self):
        self.process()
        status = self.client.get('/api/status').json()
        self.assertEqual(status['recordings'], 1)
        self.assertEqual(status['by_status'], {'ready': 1})
        self.assertEqual(status['photos'], 3)
        self.assertFalse(status['job']['running'])


if __name__ == '__main__':
    unittest.main()
