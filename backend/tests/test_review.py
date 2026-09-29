import base64
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from kiwibit_hub import review
from kiwibit_hub.catalog import Catalog
from kiwibit_hub.jobs import JobRunner
from kiwibit_hub.server import Credentials, create_app

FINCH = [{'label': 'House finch', 'similarity': .4, 'score': .9}]
SQUIRREL = [{'label': 'Eastern gray squirrel', 'similarity': .4, 'score': .9}]


def animal(track, box, category='bird', ranked=FINCH, **extra):
    return {'id': track, 'category': category, 'box': box, 'detection': .9, 'ranked': ranked, **extra}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'downloads' / '20260920T140105Z_feeder.avi'
        self.video.parent.mkdir()
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        for n in range(21):
            # Each frame has its own brightness, so a picture shows which frame was decoded.
            writer.write(np.full((48, 64, 3), n*10, dtype=np.uint8))
        writer.release()
        self.db = Catalog(self.root / 'library', self.root)
        self.identity = self.db.ingest(self.video)
        frames = []
        for n in range(21):
            animals = [animal(1, [.1, .1, .5, .6])]
            if n < 10:
                animals.append(animal(2, [.6, .5, .9, .9], category='bear', ranked=SQUIRREL))
            if n == 15:
                animals.append(animal(3, [.55, .05, .75, .3], track_confirmed=False))
            frames.append({'ms': n*100, 'animals': animals})
        self.result = {'complete': True, 'width': 64, 'height': 48, 'fps': 10, 'frames': frames}
        self.db.save_analysis(self.identity, self.result, threading.Event())

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def strata(self):
        return {row['stratum']: row for row in self.db.db.execute('SELECT * FROM answer_key')}

    def test_allocation_favours_common_groups_and_stays_within_budget(self):
        quotas = review.allocate({'House finch': 800, 'Blue jay': 50, 'Fish crow': 6}, 20)
        self.assertEqual(sum(quotas.values()), 20)
        self.assertGreater(quotas['House finch'], quotas['Blue jay'])
        self.assertGreaterEqual(quotas['Fish crow'], 1)
        self.assertEqual(review.allocate({'a': 1, 'b': 1}, 5), {'a': 1, 'b': 1})
        self.assertEqual(sum(review.allocate({k: 3 for k in 'abcdef'}, 4).values()), 4)

    def test_key_mixes_sightings_with_what_the_ai_leaves_out(self):
        with patch('kiwibit_hub.review.MAX_PER_VIDEO', 3):
            self.assertEqual(review.build(self.db, seed=1), 3)
        strata = self.strata()
        self.assertEqual(set(strata), {'sighting', 'not_bird', 'no_sighting'})
        self.assertEqual(strata['no_sighting']['frame'], 15)
        # The squirrel track is pictured where the detector was most confident.
        self.assertEqual(json.loads(strata['not_bird']['box']), [.6, .5, .9, .9])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            review.build(self.db)

    def second_recording(self):
        """Another recording: the finch again, plus a bird seen too briefly to name (an uncertain sighting)."""
        import shutil
        video = self.root / 'downloads' / '20260921T090000Z_feeder.avi'
        shutil.copy(self.video, video)
        identity = self.db.ingest(video)
        frames = [{'ms': n*100, 'animals': [animal(1, [.1, .1, .5, .6])] + ([animal(5, [.6, .1, .8, .3])] if n < 2 else [])}
                  for n in range(21)]
        self.db.save_analysis(identity, {**self.result, 'frames': frames}, threading.Event())
        return identity

    def test_added_batches_take_new_recordings_and_keep_earlier_answers(self):
        with self.assertRaisesRegex(ValueError, 'first key'):
            review.add_batch(self.db)
        review.build(self.db, seed=1)
        first = {row['id'] for row in self.db.db.execute('SELECT id FROM answer_key')}
        review.answer(self.db, next(iter(first)), 'species', 'House finch')
        with self.assertRaisesRegex(ValueError, 'no processed recordings'):
            review.add_batch(self.db, seed=1)  # its only recording is already in the key
        second = self.second_recording()
        batch, added = review.add_batch(self.db, seed=1)
        rows = self.db.db.execute('SELECT * FROM answer_key ORDER BY position').fetchall()
        self.assertEqual((batch, added), (2, len(rows) - len(first)))
        new = [row for row in rows if row['batch'] == 2]
        self.assertTrue(new and all(row['video_id'] == second for row in new))
        self.assertIn('uncertain', {row['stratum'] for row in new}, 'part of the sightings are uncertain ones')
        self.assertEqual([row['position'] for row in rows], list(range(len(rows))), 'positions continue')
        self.assertEqual(sum(row['verdict'] is not None for row in rows), 1, 'earlier answers stay')
        self.assertEqual(review.batches(self.db), [1, 2])
        self.assertEqual(review.score(self.db, 2)['total'], len(new))
        self.assertEqual(review.score(self.db)['total'], len(rows))

    def test_excluded_recordings_are_never_picked(self):
        review.build(self.db, seed=1)
        second = self.second_recording()
        with self.assertRaisesRegex(ValueError, 'no processed recordings'):
            review.add_batch(self.db, seed=1, exclude={second})

    def test_a_moment_shows_one_animal(self):
        result = {'frames': [{'animals': [animal(1, [.1, .1, .5, .6]), animal(2, [.4, .4, .7, .8])]},
                             {'animals': [animal(1, [.1, .1, .5, .6]), animal(2, [.6, .6, .9, .9])]}]}
        self.assertFalse(review._alone(result, 0, [.1, .1, .5, .6]))
        self.assertTrue(review._alone(result, 1, [.1, .1, .5, .6]))

    def test_best_photo_lookup_uses_an_index(self):
        # Without one, picking moments took minutes on the hub's Docker data volume.
        plan = ' '.join(str(row[-1]) for row in self.db.db.execute(
            'EXPLAIN QUERY PLAN SELECT ms FROM photos WHERE sighting_id=? ORDER BY quality DESC LIMIT 1', ('x',)))
        self.assertIn('photos_sighting', plan)

    def test_items_hide_the_ai_guess_and_a_recording_gives_at_most_two_moments(self):
        review.build(self.db, seed=1)
        items = review.items(self.db)
        self.assertEqual(len(items), 2)
        self.assertEqual(set(items[0]), {'id', 'batch', 'video_id', 'name', 'ms', 'box', 'view', 'verdict', 'answer', 'answered'})

    def test_answers_are_validated_and_can_be_cleared(self):
        review.build(self.db, seed=1)
        item = review.items(self.db)[0]['id']
        review.answer(self.db, item, 'species', '  Black-capped chickadee ')
        self.assertEqual(review.items(self.db)[0]['answer'], 'Black-capped chickadee')
        review.answer(self.db, item, 'not_bird', 'ignored')
        self.assertEqual(review.items(self.db)[0]['answer'], None)
        review.answer(self.db, item, None)
        self.assertIsNone(review.items(self.db)[0]['verdict'])
        with self.assertRaises(ValueError):
            review.answer(self.db, item, 'species', ' ')
        with self.assertRaises(ValueError):
            review.answer(self.db, item, 'maybe')
        with self.assertRaises(LookupError):
            review.answer(self.db, 'missing', 'unsure')

    def test_picture_is_the_context_square_of_the_exact_frame(self):
        with patch('kiwibit_hub.review.MAX_PER_VIDEO', 3):
            review.build(self.db, seed=1)
        row = self.strata()['no_sighting']
        path = review.image(self.db, row['id'])
        picture = cv2.imread(str(path))
        self.assertEqual(picture.shape[0], picture.shape[1])
        self.assertAlmostEqual(float(picture.mean()), 150, delta=6)
        modified = path.stat().st_mtime_ns
        self.assertEqual(review.image(self.db, row['id']).stat().st_mtime_ns, modified)

    def test_context_view_is_square_in_pixels_and_inside_the_frame(self):
        x1, y1, x2, y2 = review.context_view([.9, .8, 1., 1.], 3840, 2160)
        self.assertAlmostEqual((x2-x1)*3840, (y2-y1)*2160, places=0)
        self.assertTrue(0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1)
        self.assertEqual(review.context_view([0, 0, 1, 1], 3840, 2160)[1::2], [0, 1])

    def test_outcomes(self):
        sighting = {'kind': 'sighting', 'species': 'House finch', 'guess': 'House finch'}
        uncertain = {'kind': 'sighting', 'species': 'Uncertain bird', 'guess': 'House finch'}
        cases = [('species', 'House finch', sighting, 'correct'), ('species', 'Purple finch', sighting, 'wrong'),
                 ('species', 'House finch', uncertain, 'uncertain'), ('species', 'House finch', {'kind': 'bird'}, 'missed'),
                 ('species', 'House finch', {'kind': 'not_bird'}, 'missed'), ('not_bird', None, sighting, 'shown_as_bird'),
                 ('not_bird', None, {'kind': 'nothing'}, 'left_out'),
                 ('species', 'Eastern gray squirrel', {'kind': 'not_bird'}, 'left_out'),
                 ('unsure', None, sighting, None), ('several', None, sighting, None),
                 ('species', 'House finch', {'kind': 'unprocessed'}, None)]
        for verdict, species, ai, expected in cases:
            self.assertEqual(review.outcome(verdict, species, ai), expected, (verdict, species, ai))

    def test_score_follows_the_moment_across_reprocessing(self):
        with patch('kiwibit_hub.review.MAX_PER_VIDEO', 3):
            review.build(self.db, seed=1)
        strata = self.strata()
        review.answer(self.db, strata['sighting']['id'], 'species', 'House finch')
        review.answer(self.db, strata['not_bird']['id'], 'not_bird')
        review.answer(self.db, strata['no_sighting']['id'], 'species', 'Purple finch')
        score = review.score(self.db)
        self.assertEqual(score['answered'], 3)
        self.assertEqual((score['birds']['correct'], score['birds']['missed']), (1, 1))
        self.assertEqual(score['not_birds'], {'graded': 1, 'left_out': 1, 'shown_as_bird': 0})
        self.assertEqual(score['birds']['guess_correct'], 1)
        # Re-tracking renumbers every track; the answer still grades the same bird.
        renumbered = copy.deepcopy(self.result)
        for frame in renumbered['frames']:
            for item in frame['animals']:
                item['id'] += 40
        self.db.save_analysis(self.identity, renumbered, threading.Event())
        self.assertEqual(review.score(self.db)['birds']['correct'], 1)

    def test_api(self):
        library = self.root / 'library'
        self.db.close()
        app = create_app(JobRunner(library, self.root / 'downloads', self.root), Credentials('kiwibit', 'pw'),
                         library, self.root, diagnostics=False)
        auth = {'Authorization': 'Basic ' + base64.b64encode(b'kiwibit:pw').decode()}
        with TestClient(app, headers=auth) as client:
            self.assertEqual(client.get('/api/review').json(), [])
            created = client.post('/api/review')
            self.assertEqual(created.status_code, 201)
            items = created.json()
            self.assertEqual(items[0]['recorded_at'], '2026-09-20T14:01:05+00:00')
            self.assertEqual(client.post('/api/review').status_code, 409)
            item = items[0]['id']
            self.assertEqual(client.put(f'/api/review/{item}', json={'verdict': 'guess'}).status_code, 422)
            self.assertEqual(client.put('/api/review/missing', json={'verdict': 'unsure'}).status_code, 404)
            self.assertEqual(client.put(f'/api/review/{item}', json={'verdict': 'unsure'}).status_code, 200)
            picture = client.get(f'/api/review/{item}/image')
            self.assertEqual((picture.status_code, picture.headers['content-type']), (200, 'image/jpeg'))
            self.assertEqual(client.get('/api/review/score').json()['excluded'], 1)
            # A second batch: only recordings not yet in the key; its pictures are prepared in the background.
            self.assertEqual(client.post('/api/review/batch').status_code, 409, 'nothing new to pick from')
            self.db = Catalog(library, self.root)
            self.second_recording()
            self.db.close()
            added = client.post('/api/review/batch')
            self.assertEqual(added.status_code, 201)
            client.app.state.review_pictures.join(30)  # it has the recordings open
            picture = next(row['id'] for row in added.json() if row['batch'] == 2)
            self.assertTrue((library / 'review' / f'{picture}.jpg').is_file(), 'pictures are ready ahead of time')
            self.assertEqual({row['batch'] for row in added.json()}, {1, 2})
            second = client.get('/api/review/score', params={'batch': 2}).json()
            self.assertEqual((second['batch'], second['batches'], second['answered']), (2, [1, 2], 0))
            self.assertEqual(client.get('/api/review/score').json()['total'], len(added.json()))
        self.db = Catalog(library, self.root)


if __name__ == '__main__':
    unittest.main()
