"""Conservative within-video sighting summaries and photo selection.

These are reviewable heuristics, not calibrated species probabilities or individual
bird recognition. Raw frame predictions are always retained separately.
"""
from collections import defaultdict
import math

from .engine import iou

SMART_VERSION = 4
UNCERTAIN = 'Uncertain bird'


def collect_sightings(frames):
    """Split detector tracks at long gaps; never merge separate tracked animals."""
    groups = []
    active = {}
    for index, frame in enumerate(frames):
        for animal in frame['animals']:
            if animal['category'] != 'bird' or not animal['ranked'] or animal.get('track_confirmed') is False:
                continue
            track = animal['id']
            group = active.get(track)
            if group is None or frame['ms'] - group['end_ms'] > 1500:
                group = {'track': track, 'start_ms': frame['ms'], 'end_ms': frame['ms'], 'views': []}
                groups.append(group)
                active[track] = group
            group['end_ms'] = frame['ms']
            # Sample independent moments instead of counting repeated frame ranks.
            if animal.get('tracking_state') != 'weak' and (not group['views'] or frame['ms'] - group['views'][-1]['ms'] >= 1000):
                group['views'].append({'frame': index, 'ms': frame['ms'], 'animal': animal})
    return groups


def photo_quality(crop, box):
    import cv2
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # A fixed working size makes the blur measure comparable across crop sizes.
    gray = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    clarity = min(1., math.log1p(sharpness) / math.log1p(1500))
    area = max(0., (box[2]-box[0]) * (box[3]-box[1]))
    size = min(1., math.sqrt(area / .12))
    # Border clipping is only a visibility proxy, not an occlusion/head detector.
    visibility = sum([box[0] > .01, box[1] > .01, box[2] < .99, box[3] < .99]) / 4
    quality = round(100 * (.60*clarity + .25*size + .15*visibility), 2)
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    fingerprint = 0
    for bit in (small[:, 1:] > small[:, :-1]).ravel():
        fingerprint = (fingerprint << 1) | int(bit)
    return quality, f'{fingerprint:016x}'


def summarize(views, nonbirds):
    votes = defaultdict(float)
    samples = []
    for view in views:
        animal = view['animal']
        weight = max(.05, animal.get('detection', .5)) * (.25 + .75*view.get('quality', 50)/100)
        scores = {p['label']: p['similarity'] for p in animal['ranked']}
        winner = animal['ranked'][0]['label']
        votes[winner] += weight
        samples.append((weight, scores, winner))
    if not samples:
        return {'species': UNCERTAIN, 'suggestion': '', 'samples': 0, 'support': 0,
                'agreement': 0, 'similarity': 0, 'margin': 0, 'reason': 'No usable views'}
    total = sum(weight for weight, _, _ in samples)
    averages = {label: sum(weight*scores.get(label, min(scores.values())-.03)
                           for weight, scores, _ in samples)/total
                for label in {label for _, scores, _ in samples for label in scores}}
    ranked = sorted(averages, key=lambda label: (-averages[label], label))
    winner = ranked[0]
    agreement = votes[winner] / total
    support = sum(top == winner for _, _, top in samples)
    margin = averages[winner] - (averages[ranked[1]] if len(ranked) > 1 else 0)
    reason = ''
    if winner in nonbirds:
        reason = 'Bird detector and species model disagree'
    elif len(samples) < 3:
        reason = 'Fewer than three sampled views'
    elif agreement < .65 or support / len(samples) < .60:
        reason = 'Predictions disagree across views'
    elif margin < .025:
        reason = 'Top species are too close to separate'
    elif averages[winner] < .20:
        reason = 'Weak image-to-species match'
    return {'species': UNCERTAIN if reason else winner, 'suggestion': winner,
            'samples': len(samples), 'support': support,
            'agreement': round(agreement, 4), 'similarity': round(averages[winner], 5),
            'margin': round(margin, 5), 'reason': reason or 'Consistent sampled views'}


def best_photos(views, maximum=3):
    """Keep all crops; feature only the clearest visually distinct examples."""
    selected = []
    for view in sorted(views, key=lambda item: (-item['quality'], item['ms'])):
        if all((int(view['fingerprint'], 16) ^ int(other['fingerprint'], 16)).bit_count() > 8
               and abs(view['ms']-other['ms']) >= 750 for other in selected):
            selected.append(view)
            if len(selected) >= maximum:
                break
    return {view['path'] for view in selected}


def sighting_matches(groups, old):
    """Match only mutual, unambiguous temporal/spatial overlaps after reprocessing.

    Track numbers are not identities. Ambiguous splits/merges leave corrections on
    the prior sighting for review instead of assigning them to the wrong bird.
    """
    candidates = []
    for index, group in enumerate(groups):
        for previous in old:
            start = max(group['start_ms'], previous['start_ms'])
            end = min(group['end_ms'], previous['end_ms'])
            union = max(group['end_ms'], previous['end_ms']) - min(group['start_ms'], previous['start_ms'])
            temporal = max(0, end-start+1) / max(1, union+1)
            if temporal < .60:
                continue
            anchors = previous['anchors']
            overlaps = []
            for view in group['views']:
                anchor = min(anchors, key=lambda a: abs(a['ms']-view['ms']), default=None)
                if anchor and abs(anchor['ms']-view['ms']) <= 550:
                    overlaps.append(iou(view['animal']['box'], anchor['box']))
            spatial = sum(overlaps)/len(overlaps) if overlaps else 0
            if spatial >= .50:
                candidates.append((temporal*spatial, index, previous['id']))
    matches = {}
    for score, index, old_id in candidates:
        rivals = [s for s, i, oid in candidates if (i == index or oid == old_id) and (i, oid) != (index, old_id)]
        if not rivals or score - max(rivals) >= .15:
            matches[index] = old_id
    return matches
