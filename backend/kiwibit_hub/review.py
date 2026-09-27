"""The answer key: moments identified by eye, to measure the AI after each change.

Each moment is a video frame and a box, never a sighting or track number, so it
survives re-tracking and re-analysis. The AI's guess is not shown while answering.
These answers are test data: training on them would make the score look better than it is.
"""
from __future__ import annotations

import bisect
import json
import math
import random
import uuid
from collections import Counter, defaultdict

from .engine import context_square, iou
from .sightings import UNCERTAIN
from .tracking import NONBIRD_LABELS

VERDICTS = ('species', 'not_bird', 'several', 'unsure')
GRADES = ('correct', 'uncertain', 'wrong', 'missed')
DEFAULT_SIZE = 150
# Besides sightings, part of the key checks what the AI leaves out: tracks it called
# something other than a bird, and bird boxes that never became a sighting.
MISS_SHARE = {'not_bird': .13, 'no_sighting': .13}
MAX_PER_VIDEO = 2
# Guesses the AI made fewer times than this share one group, so dozens of one-off
# guesses don't crowd out the birds that actually visit.
MIN_GROUP = 5
# The picture shows a square this many times the box's longer side, for context.
CONTEXT = 1.8
MATCH_IOU = .3


def context_view(box, width, height):
    """The square pictured around the box, in normalized coordinates."""
    return [round(value, 5) for value in context_square(box, width, height, CONTEXT)]


def allocate(sizes, budget):
    """Picks per group in proportion to the square root of its size.

    Common species get more moments; rare ones get at least one while the budget allows.
    """
    if not sizes or budget <= 0:
        return {}
    weights = {key: math.sqrt(size) for key, size in sizes.items()}
    total = sum(weights.values())
    quotas = {key: min(sizes[key], max(1, round(budget*weight/total))) for key, weight in weights.items()}
    while sum(quotas.values()) > budget:
        # Trim the largest group; once every group is down to one, drop the smallest.
        largest = max(quotas, key=lambda key: (quotas[key], key))
        if quotas[largest] > 1:
            quotas[largest] -= 1
        else:
            quotas[min((key for key in quotas if quotas[key]), key=lambda key: (weights[key], key))] = 0
    while sum(quotas.values()) < budget:
        open_groups = [key for key in quotas if quotas[key] < sizes[key]]
        if not open_groups:
            break
        quotas[max(open_groups, key=lambda key: (budget*weights[key]/total - quotas[key], key))] += 1
    return quotas


class _Picker:
    """Accepts moments while keeping one per sighting or track and a few per recording."""

    def __init__(self):
        self.per_video = Counter()
        self.sources = set()
        self.moments = []

    def take(self, candidates, quota, stratum):
        taken = 0
        for moment in candidates:
            if taken >= quota:
                break
            if moment['source'] in self.sources or self.per_video[moment['video_id']] >= MAX_PER_VIDEO:
                continue
            self.sources.add(moment['source'])
            self.per_video[moment['video_id']] += 1
            self.moments.append(dict(moment, stratum=stratum))
            taken += 1
        return taken


def _frame_index(result, ms):
    times = [frame['ms'] for frame in result['frames']]
    index = bisect.bisect_left(times, ms)
    return min((i for i in (index-1, index) if 0 <= i < len(times)), key=lambda i: abs(times[i]-ms))


def _moment(video_id, result, frame, box, source):
    return {'video_id': video_id, 'frame': frame, 'ms': result['frames'][frame]['ms'], 'box': box,
            'view': context_view(box, result['width'], result['height']), 'source': source}


def _clearest(frames_and_animals):
    """The frame where the detector was most confident, as a stand-in for the clearest view."""
    frame, animal = max(frames_and_animals, key=lambda item: (item[1].get('detection', 0), -item[0]))
    return frame, animal['box']


def build(db, size=DEFAULT_SIZE, seed=None):
    """Pick the moments to answer. Refuses to replace an existing key."""
    if db.db.execute('SELECT count(*) FROM answer_key').fetchone()[0]:
        raise ValueError('The answer key already exists')
    rng = random.Random(seed)
    ready = {row['id'] for row in db.videos() if row['status'] == 'ready' and row['result']}
    results = {}

    def result(video_id):
        if video_id not in results:
            try:
                results[video_id] = db.result(video_id, annotated=False)
            except (OSError, ValueError):
                results[video_id] = None
        return results[video_id]

    by_guess = defaultdict(list)
    for row in db.db.execute('''SELECT s.*, (SELECT p.ms FROM photos p WHERE p.sighting_id=s.id
                                   ORDER BY p.quality DESC LIMIT 1) AS best_ms
                                FROM sightings s WHERE s.active=1 ORDER BY s.id'''):
        if row['video_id'] in ready:
            evidence = json.loads(row['evidence'])
            by_guess[evidence.get('suggestion') or row['ai_species']].append(row)
    sightings = defaultdict(list)
    for guess, rows in by_guess.items():
        sightings[guess if len(rows) >= MIN_GROUP else ''].extend(rows)
    miss_budget = {stratum: round(size*share) for stratum, share in MISS_SHARE.items()}
    picker = _Picker()

    # Sightings, grouped by the AI's best guess so every species it often names gets checked.
    quotas = allocate({guess: len(rows) for guess, rows in sightings.items()}, size-sum(miss_budget.values()))
    for guess in sorted(quotas):
        rows = sightings[guess][:]
        rng.shuffle(rows)
        candidates = []
        for row in rows:
            if len(candidates) >= quotas[guess]*3:
                break
            analysis = result(row['video_id'])
            anchors = json.loads(row['anchors'])
            if analysis is None or not anchors:
                continue
            # The sighting's best photo, else its middle view.
            wanted = row['best_ms'] if row['best_ms'] is not None else anchors[len(anchors)//2]['ms']
            anchor = min(anchors, key=lambda a: abs(a['ms']-wanted))
            candidates.append(_moment(row['video_id'], analysis, _frame_index(analysis, anchor['ms']), anchor['box'],
                                      ('sighting', row['id'])))
        picker.take(candidates, quotas[guess], 'sighting')

    # What the AI leaves out, from a random selection of recordings.
    pools = {stratum: [] for stratum in MISS_SHARE}
    videos = sorted(ready)
    rng.shuffle(videos)
    for video_id in videos:
        if all(len(pools[s]) >= 4*miss_budget[s] for s in pools):
            break
        analysis = result(video_id)
        if analysis is None:
            continue
        covered = defaultdict(list)
        for row in db.sightings(video_id):
            covered[row['track']].append((row['start_ms'], row['end_ms']))
        tracks = defaultdict(list)
        for index, frame in enumerate(analysis['frames']):
            for animal in frame['animals']:
                tracks[animal['id']].append((index, animal))
        for track, seen in sorted(tracks.items()):
            category = seen[0][1]['category']
            if category != 'bird' and any(animal.get('ranked') for _, animal in seen):
                frame, box = _clearest(seen)
                pools['not_bird'].append(_moment(video_id, analysis, frame, box, ('track', video_id, track)))
            elif category == 'bird':
                uncovered = [(i, a) for i, a in seen if not any(start <= analysis['frames'][i]['ms'] <= end
                                                                 for start, end in covered[track])]
                if uncovered:
                    frame, box = _clearest(uncovered)
                    pools['no_sighting'].append(_moment(video_id, analysis, frame, box, ('track', video_id, track)))
    for stratum, pool in pools.items():
        rng.shuffle(pool)
        picker.take(pool, miss_budget[stratum], stratum)

    moments = picker.moments
    if not moments:
        raise ValueError('There are no processed recordings with animals to pick from yet')
    # Mixed order, so one species or group never comes in a long run.
    rng.shuffle(moments)
    with db.db:
        db.db.execute('BEGIN IMMEDIATE')
        if db.db.execute('SELECT count(*) FROM answer_key').fetchone()[0]:
            raise ValueError('The answer key already exists')
        db.db.executemany('''INSERT INTO answer_key(id,position,video_id,frame,ms,box,view,stratum)
                             VALUES(?,?,?,?,?,?,?,?)''',
                          [(uuid.uuid4().hex, position, m['video_id'], m['frame'], m['ms'],
                            json.dumps(m['box']), json.dumps(m['view']), m['stratum'])
                           for position, m in enumerate(moments)])
    return len(moments)


def items(db):
    """Every moment with its answer. Deliberately without the AI's guess or why it was picked."""
    rows = db.db.execute('''SELECT k.id, k.video_id, v.name, k.ms, k.box, k.view, k.verdict, k.answer, k.answered
                            FROM answer_key k JOIN videos v ON v.id=k.video_id ORDER BY k.position''')
    return [{**dict(row), 'box': json.loads(row['box']), 'view': json.loads(row['view'])} for row in rows]


def answer(db, item_id, verdict, species=None):
    """Record what the moment really shows; verdict None clears the answer."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f'The answer must be one of {", ".join(VERDICTS)}')
    species = species.strip() if species is not None else None
    if verdict == 'species':
        if not species or len(species) > 120 or any(ord(c) < 32 for c in species):
            raise ValueError('Enter a species name (1–120 characters)')
    else:
        species = None
    with db.db:
        changed = db.db.execute('''UPDATE answer_key SET verdict=?, answer=?,
                                   answered=CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id=?''',
                                (verdict, species, verdict, item_id)).rowcount
    if not changed:
        raise LookupError('This moment is not in the answer key')


def image(db, item_id, size=960):
    """The moment's picture: the context square from the original frame, cached as a JPEG."""
    row = db.db.execute('SELECT * FROM answer_key WHERE id=?', (item_id,)).fetchone()
    if row is None:
        raise LookupError('This moment is not in the answer key')
    target = db.root / 'review' / f'{item_id}.jpg'
    if target.is_file():
        return target
    import cv2
    video = db.db.execute('SELECT * FROM videos WHERE id=?', (row['video_id'],)).fetchone()
    capture = cv2.VideoCapture(str(db.video_path(video)))
    try:
        # Decode in order, as analysis did: frame numbers then match exactly.
        for _ in range(row['frame']+1):
            if not capture.grab():
                raise ValueError('The recording ended before this moment')
        ok, frame = capture.retrieve()
        if not ok:
            raise ValueError('Could not decode this moment')
    finally:
        capture.release()
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = json.loads(row['view'])
    crop = frame[int(y1*height):max(int(y1*height)+1, round(y2*height)),
                 int(x1*width):max(int(x1*width)+1, round(x2*width))]
    scale = min(1., size/max(crop.shape[:2]))
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('Could not encode this moment')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'{item_id}.{uuid.uuid4().hex}.tmp')
    temporary.write_bytes(encoded.tobytes())
    temporary.replace(target)
    return target


def ai_view(result, sightings, frame_index, box):
    """What the AI currently says about the animal in this box at this frame."""
    if result is None:
        return {'kind': 'unprocessed'}
    if not 0 <= frame_index < len(result['frames']):
        return {'kind': 'nothing'}
    frame = result['frames'][frame_index]
    overlaps = [(iou(animal['box'], box), animal) for animal in frame['animals']]
    best, animal = max(overlaps, key=lambda item: item[0], default=(0, None))
    if animal is None or best < MATCH_IOU:
        return {'kind': 'nothing'}
    if animal['category'] != 'bird':
        return {'kind': 'not_bird'}
    for row in sightings:
        if row['track'] == animal['id'] and row['start_ms'] <= frame['ms'] <= row['end_ms']:
            evidence = json.loads(row['evidence'])
            return {'kind': 'sighting', 'species': row['ai_species'],
                    'guess': evidence.get('suggestion') or row['ai_species']}
    return {'kind': 'bird'}


def outcome(verdict, species, ai):
    """How the AI did on one answered moment, or None when the answer can't grade it."""
    if verdict in ('unsure', 'several') or verdict is None or ai['kind'] == 'unprocessed':
        return None
    if verdict == 'species' and species not in NONBIRD_LABELS:
        if ai['kind'] != 'sighting':
            return 'missed'
        if ai['species'] == species:
            return 'correct'
        return 'uncertain' if ai['species'] == UNCERTAIN else 'wrong'
    return 'shown_as_bird' if ai['kind'] == 'sighting' else 'left_out'


def score(db):
    """Compare every answer with what the AI says at that moment now."""
    rows = db.db.execute('SELECT * FROM answer_key ORDER BY position').fetchall()
    results, sightings = {}, {}
    birds = Counter()
    not_birds = Counter()
    per_species = defaultdict(Counter)
    confusions = Counter()
    for row in rows:
        if row['verdict'] is None:
            continue
        video_id = row['video_id']
        if video_id not in results:
            try:
                results[video_id] = db.result(video_id, annotated=False)
            except (OSError, ValueError):
                results[video_id] = None
            sightings[video_id] = db.sightings(video_id)
        ai = ai_view(results[video_id], sightings[video_id], row['frame'], json.loads(row['box']))
        result = outcome(row['verdict'], row['answer'], ai)
        if result is None:
            birds['excluded'] += 1
        elif result in ('shown_as_bird', 'left_out'):
            not_birds[result] += 1
        else:
            birds[result] += 1
            per_species[row['answer']][result] += 1
            if ai['kind'] == 'sighting':
                birds['guessed'] += 1
                birds['guess_correct'] += ai['guess'] == row['answer']
                if ai['guess'] != row['answer']:
                    confusions[(row['answer'], ai['guess'])] += 1
    graded = sum(birds[key] for key in GRADES)
    return {
        'total': len(rows), 'answered': sum(row['verdict'] is not None for row in rows),
        'excluded': birds['excluded'],
        'birds': {'graded': graded, **{key: birds[key] for key in GRADES},
                  'guessed': birds['guessed'], 'guess_correct': birds['guess_correct']},
        'not_birds': {'graded': not_birds['shown_as_bird']+not_birds['left_out'],
                      'left_out': not_birds['left_out'], 'shown_as_bird': not_birds['shown_as_bird']},
        'species': sorted(({'species': name, 'graded': sum(counts.values()), **{key: counts[key] for key in GRADES}}
                           for name, counts in per_species.items()), key=lambda s: (-s['graded'], s['species'])),
        'confusions': [{'answer': a, 'guess': g, 'count': n}
                       for (a, g), n in sorted(confusions.items(), key=lambda item: (-item[1], item[0]))],
    }
