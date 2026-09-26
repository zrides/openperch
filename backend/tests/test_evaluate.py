import unittest
from unittest.mock import patch

from kiwibit_hub import evaluate, review
import test_review as fixtures


class EvaluateTests(unittest.TestCase):
    setUp = fixtures.ReviewTests.setUp
    tearDown = fixtures.ReviewTests.tearDown
    strata = fixtures.ReviewTests.strata

    def test_variants_are_scored_against_the_key_without_changing_the_library(self):
        with patch('kiwibit_hub.review.MAX_PER_VIDEO', 3):
            review.build(self.db, seed=1)
        review.answer(self.db, self.strata()['sighting']['id'], 'species', 'House finch')
        review.answer(self.db, self.strata()['not_bird']['id'], 'not_bird')
        before = [tuple(row) for row in self.db.db.execute('SELECT * FROM sightings ORDER BY id')]
        totals, answered = evaluate.run(self.root / 'library', ['baseline', 'stitch+dense'])
        self.assertEqual(answered, 2)
        for name in ('baseline', 'stitch+dense'):
            self.assertEqual(totals[name]['sightings'], 1)
            self.assertEqual((totals[name]['key: correct'], totals[name]['key: left_out']), (1, 1))
        # Denser views: one per classification instead of one per second.
        self.assertGreater(totals['stitch+dense']['reason: Consistent sampled views'], 0)
        self.assertEqual([tuple(row) for row in self.db.db.execute('SELECT * FROM sightings ORDER BY id')], before)
        self.assertIn('stitch+dense: uncertain', evaluate.report(totals, answered))


if __name__ == '__main__':
    unittest.main()
