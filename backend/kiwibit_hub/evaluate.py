"""Measure tracking and sighting variants on saved analyses, without touching the library.

Reads a copy of the catalog (the recordings and your answer key) and the saved
per-frame results, re-runs tracking and sighting summaries in memory under each
variant's settings, and reports consistency numbers and the answer-key score.
No model runs. Photo quality is not recomputed (every view counts as 50), so
compare variants with each other rather than with the hub's own score.

    python -m kiwibit_hub.evaluate --library ../data/library [--variants baseline,stitch]
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import itertools
import json
import math
import sqlite3
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from . import review, sightings, tracking
from .tracking import NONBIRD_LABELS

# Each variant overrides module settings; everything else is the current code.
# 'baseline' is tracker v2 with a view per second, as deployed before stitching.
# 'exclude' drops species from the saved rankings, as a shorter species list would.
VARIANTS = {
    'baseline': {'tracking.STITCH_MAX_GAP_MS': 0, 'sightings.VIEW_GAP_MS': 1000},
    'stitch': {'sightings.VIEW_GAP_MS': 1000},
    'dense': {'tracking.STITCH_MAX_GAP_MS': 0},
    'stitch+dense': {},
    'stitch+dense-carolina': {'exclude': frozenset({'Carolina chickadee'})},
}
MODULES = {'tracking': tracking, 'sightings': sightings, 'review': review}


@contextlib.contextmanager
def settings(overrides):
    previous = {}
    try:
        for name, value in overrides.items():
            if name == 'exclude':
                continue
            module, attribute = name.split('.')
            previous[name] = getattr(MODULES[module], attribute)
            setattr(MODULES[module], attribute, value)
        yield
    finally:
        for name, value in previous.items():
            module, attribute = name.split('.')
            setattr(MODULES[module], attribute, value)


def snapshot(library, target):
    """A consistent copy of the catalog, taken through a read-only connection."""
    source = sqlite3.connect((Path(library) / 'library.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    copy_db = sqlite3.connect(target)
    try:
        source.backup(copy_db)
    finally:
        copy_db.close()
        source.close()


def restarts_nearby(frames):
    """Bird tracks that end and have a new bird track start close by within 1.5 s."""
    spans = defaultdict(list)
    for frame in frames:
        for animal in frame['animals']:
            if animal['category'] == 'bird':
                spans[animal['id']].append((frame['ms'], animal['box']))
    count = 0
    for track, seen in spans.items():
        end_ms, box = seen[-1]
        size = max(box[2]-box[0], box[3]-box[1], .02)
        for other, later in spans.items():
            start_ms, first = later[0]
            if other != track and 0 < start_ms-end_ms <= 1500 and math.dist(tracking.center(box), tracking.center(first)) <= size:
                count += 1
                break
    return count, len(spans)


def mixed_tracks(frames):
    """Tracks with a sustained run of bird guesses and a sustained run of other animals."""
    guesses = defaultdict(list)
    for frame in frames:
        for animal in frame['animals']:
            if animal.get('ranked'):
                guesses[animal['id']].append(animal['ranked'][0]['label'] in NONBIRD_LABELS)
    count = 0
    for sequence in guesses.values():
        runs = [(key, len(list(group))) for key, group in itertools.groupby(sequence)]
        count += any(k and n >= 8 for k, n in runs) and any(not k and n >= 8 for k, n in runs)
    return count


def without(result, labels):
    """The analysis with these species removed from every ranking (never emptied)."""
    frames = [{**frame, 'animals': [{**animal, 'ranked': [r for r in animal['ranked'] if r['label'] not in labels]
                                                  or animal['ranked']} if animal.get('ranked') else animal
                                    for animal in frame['animals']]} for frame in result['frames']]
    return {**result, 'frames': frames}


def evaluate(result, answers, totals):
    """Run the current settings over one saved analysis and add its numbers to totals."""
    # A version no tracker has, so the saved detections are re-tracked under this variant.
    tracked = tracking.retrack_result({**result, 'tracker_version': 0})
    rows = []
    for group in sightings.collect_sightings(tracked['frames']):
        evidence = sightings.summarize(group['views'], NONBIRD_LABELS)
        rows.append({'track': group['track'], 'start_ms': group['start_ms'], 'end_ms': group['end_ms'],
                     'ai_species': evidence['species'], 'evidence': json.dumps(evidence)})
        totals['sightings'] += 1
        totals['uncertain'] += evidence['species'] == sightings.UNCERTAIN
        totals['one view'] += len(group['views']) == 1
        totals['reason: ' + evidence['reason']] += 1
    restarts, tracks = restarts_nearby(tracked['frames'])
    totals['bird tracks'] += tracks
    totals['restarts nearby'] += restarts
    totals['bird/other mixed tracks'] += mixed_tracks(tracked['frames'])
    totals['stitched'] += tracked['tracking_stats'].get('stitched', 0)
    for item in answers:
        ai = review.ai_view(tracked, rows, item['frame'], json.loads(item['box']))
        outcome = review.outcome(item['verdict'], item['answer'], ai)
        totals['key: ' + (outcome or 'excluded')] += 1
        if outcome in review.GRADES and ai['kind'] == 'sighting':
            totals['key: guessed'] += 1
            totals['key: guess correct'] += ai['guess'] == item['answer']


def run(library, variants, database=None):
    library = Path(library)
    with tempfile.TemporaryDirectory() as folder:
        if database is None:
            database = Path(folder) / 'library.sqlite3'
            snapshot(library, database)
        db = sqlite3.connect(database)
        db.row_factory = sqlite3.Row
        videos = db.execute("SELECT id, result FROM videos WHERE status='ready' AND result IS NOT NULL ORDER BY id").fetchall()
        answers = defaultdict(list)
        for item in db.execute('SELECT * FROM answer_key WHERE verdict IS NOT NULL'):
            answers[item['video_id']].append(dict(item))
        db.close()
    totals = {name: Counter() for name in variants}
    for video in videos:
        with gzip.open(library / video['result'], 'rt', encoding='utf-8') as source:
            result = json.load(source)
        for name in variants:
            with settings(VARIANTS[name]):
                excluded = VARIANTS[name].get('exclude')
                evaluate(without(result, excluded) if excluded else result, answers[video['id']], totals[name])
    return totals, sum(len(items) for items in answers.values())


def report(totals, answered):
    names = list(totals)
    keys = sorted({key for counts in totals.values() for key in counts}, key=lambda key: (key.startswith('key'), key))
    width = max(len(key) for key in keys)
    lines = [f'{"":{width}}  ' + '  '.join(f'{name:>13}' for name in names)]
    for key in keys:
        lines.append(f'{key:{width}}  ' + '  '.join(f'{totals[name][key]:>13}' for name in names))
    for name in names:
        counts = totals[name]
        graded = sum(counts['key: ' + grade] for grade in review.GRADES)
        share = lambda n, d: f'{100*n/d:.0f}%' if d else '-'
        lines.append(f'{name}: uncertain {share(counts["uncertain"], counts["sightings"])} of sightings; '
                     f'answer key: {graded} graded birds, correct {share(counts["key: correct"], graded)}, '
                     f'uncertain {share(counts["key: uncertain"], graded)}, wrong {share(counts["key: wrong"], graded)}, '
                     f'missed {share(counts["key: missed"], graded)}, best guess right '
                     f'{share(counts["key: guess correct"], counts["key: guessed"])}')
    lines.append(f'({answered} answered moments in the key)')
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--library', type=Path, required=True, help='the library folder (read only)')
    parser.add_argument('--variants', default=','.join(VARIANTS), help=f'comma-separated, from {", ".join(VARIANTS)}')
    parser.add_argument('--database', type=Path, help='use this catalog copy instead of snapshotting the library')
    arguments = parser.parse_args()
    chosen = [name.strip() for name in arguments.variants.split(',') if name.strip()]
    unknown = set(chosen) - set(VARIANTS)
    if unknown:
        parser.error(f'unknown variants: {", ".join(sorted(unknown))}')
    print(report(*run(arguments.library, chosen, arguments.database)))
