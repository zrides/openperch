import math
import unittest

from kiwibit_hub.overlap import coverage, estimate_offset, origin_of, recorded_at, start_seconds

T = 1_780_000_000.0  # any moment; clips below are placed relative to it


class CoverageTests(unittest.TestCase):
    def hidden(self, cloud, cards, offset=0.0):
        return set(coverage(cloud, cards, offset))

    def test_containment_hides_and_partial_overlap_does_not(self):
        cards = [('card', T, 60)]
        self.assertEqual(self.hidden([('inside', T + 10, 20)], cards), {'inside'})
        self.assertEqual(self.hidden([('same', T, 60)], cards), {'same'})
        # Starts before the card clip, or runs well past its end: part of it is only in the cloud.
        self.assertEqual(self.hidden([('early', T - 5, 20), ('late', T + 50, 20)], cards), set())
        self.assertEqual(self.hidden([('apart', T + 100, 10)], cards), set())

    def test_endpoint_contact_is_not_overlap(self):
        # A cloud clip starting where the card clip ends shares no footage with it.
        self.assertEqual(self.hidden([('after', T + 60, 2)], [('card', T, 60)]), set())

    def test_slack_absorbs_small_timing_differences(self):
        cards = [('card', T, 60)]
        self.assertEqual(self.hidden([('a', T - 0.9, 20), ('b', T + 45, 17.9)], cards), {'a', 'b'})
        self.assertEqual(self.hidden([('a', T - 1.1, 20), ('b', T + 45, 18.1)], cards), set())

    def test_adjacent_card_files_cover_together_but_a_gap_does_not(self):
        # Cards split long recordings into files; a cloud clip may span the split.
        joined = [('first', T, 180), ('second', T + 180.4, 60)]
        self.assertEqual(coverage([('cloud', T + 170, 30)], joined, 0), {'cloud': {'id': 'first', 'offset_ms': 170000}})
        gap = [('first', T, 180), ('second', T + 190, 60)]
        self.assertEqual(self.hidden([('cloud', T + 170, 30)], gap), set())

    def test_points_at_the_card_file_and_moment_to_play(self):
        cards = [('first', T, 180), ('second', T + 180, 180)]
        self.assertEqual(coverage([('cloud', T + 200, 10)], cards, 0), {'cloud': {'id': 'second', 'offset_ms': 20000}})
        # Starting a moment before the card clip: play from its beginning.
        self.assertEqual(coverage([('cloud', T - 0.5, 10)], cards, 0), {'cloud': {'id': 'first', 'offset_ms': 0}})

    def test_unknown_times_or_lengths_keep_the_cloud_clip(self):
        cards = [('card', T, 60)]
        for start, length in [(None, 10), (T + 1, None), (T + 1, 0), (T + 1, -3), (T + 1, math.nan), (T + 1, math.inf)]:
            self.assertEqual(self.hidden([('cloud', start, length)], cards), set(), (start, length))
        for start, length in [(None, 60), (T, 0), (T, None), (T, math.nan)]:
            self.assertEqual(self.hidden([('cloud', T + 1, 10)], [('card', start, length)]), set(), (start, length))

    def test_offset_moves_cloud_times_onto_the_card_clock(self):
        # Kiwibit cloud clips are labelled about 3 s before the card clip of the same moment.
        cards = [('card', T + 3, 10)]
        self.assertEqual(self.hidden([('cloud', T, 10)], cards, offset=0), set())
        self.assertEqual(self.hidden([('cloud', T, 10)], cards, offset=3), {'cloud'})


class OffsetTests(unittest.TestCase):
    def test_consistent_pairs_give_their_median(self):
        cloud = [T + 1000 * n for n in range(12)]
        cards = [start + (3.0 if n % 5 else 4.0) for n, start in enumerate(cloud)]
        self.assertEqual(estimate_offset(cloud, cards), (3.0, 12))

    def test_too_few_or_scattered_pairs_give_no_offset(self):
        cloud = [T + 1000 * n for n in range(4)]
        self.assertEqual(estimate_offset(cloud, [start + 3 for start in cloud]), (0.0, 4))
        scattered = [T + 1000 * n for n in range(12)]
        cards = [start + (n % 6) * 1.5 - 4 for n, start in enumerate(scattered)]
        self.assertEqual(estimate_offset(scattered, cards)[0], 0.0)

    def test_distant_card_clips_are_not_pairs(self):
        cloud = [T + 1000 * n for n in range(10)]
        self.assertEqual(estimate_offset(cloud, [start + 30 for start in cloud]), (0.0, 0))
        self.assertEqual(estimate_offset(cloud, []), (0.0, 0))

    def test_measured_case(self):
        """The shape of real Kiwibit footage: card clips start 3 s after the cloud label, and cloud
        lengths run up to ~3 s past the card clip (timestamp jumps, frozen tails)."""
        cloud, cards = [], []
        for n in range(30):
            start = T + 600 * n
            card_length = 10 + n % 7
            cards.append((f'card{n}', start + 3 + (n % 10 == 0), card_length))
            cloud.append((f'cloud{n}', start, card_length + (n % 4) * 0.9))
        cloud.append(('cloud-only tail', T + 600 * 30, 40))
        cards.append(('short card', T + 600 * 30 + 3, 15))
        offset, pairs = estimate_offset([c[1] for c in cloud], [c[1] for c in cards])
        self.assertEqual((offset, pairs), (3.0, 31))
        hidden = set(coverage(cloud, cards, offset))
        self.assertEqual(hidden, {f'cloud{n}' for n in range(30)})


class NameTests(unittest.TestCase):
    def test_cloud_and_card_names_agree_across_midnight_and_dst(self):
        # 11:30 PM EDT on Sept 18 is 03:30 UTC on Sept 19.
        self.assertEqual(start_seconds('2026-09-18T11-30-00PM-04-00.mp4'),
                         start_seconds('20260919T033000Z_0123456789abcdef.mp4'))
        # Winter time (-05:00) after the November change.
        self.assertEqual(recorded_at('2026-11-02T07-00-00AM-05-00.mp4'), '2026-11-02T12:00:00+00:00')

    def test_impossible_or_missing_times(self):
        self.assertIsNone(recorded_at('2026-02-30T07-00-00AM-05-00.mp4'))
        self.assertIsNone(recorded_at('20261340T000000Z_0123456789abcdef.mp4'))
        self.assertIsNone(start_seconds('bird.avi'))

    def test_origin_comes_from_the_inbox(self):
        cloud = {'downloads/abc/20260919T033000Z_0123456789abcdef.mp4'}
        self.assertEqual(origin_of('sdcard/DCIM/day/clip.mp4', cloud), 'sdcard')
        self.assertEqual(origin_of('downloads/live/clip.mp4', cloud), 'live')
        self.assertEqual(origin_of('downloads/abc/20260919T033000Z_0123456789abcdef.mp4', cloud), 'cloud')
        # A download the manifest does not vouch for, or an imported copy, is of unknown origin.
        self.assertEqual(origin_of('downloads/other.mp4', cloud), 'unknown')
        self.assertEqual(origin_of('/elsewhere/library/videos/0a1b.mp4', cloud), 'unknown')


if __name__ == '__main__':
    unittest.main()
