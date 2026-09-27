"""Within-clip animal tracking with motion, appearance and global association.

Low-score detections can support existing tracks, never create new ones. Predicted
boxes are used for matching only: output boxes always come from the detector.
Tracks follow the animal, not the detector's label: YOLO-World renames the same
animal between frames, so each track's category is decided once from all its frames.
After the clip, tracks split by a brief miss are joined again (stitch_tracks).
This is a small custom tracker, not the ByteTrack implementation or bird re-ID.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from dataclasses import dataclass
import math
from pathlib import Path

TRACKER_VERSION = 4
HIGH_CONFIDENCE = .22
LOW_CONFIDENCE = .10
MAX_GAP_MS = 1200
NONBIRD_LABELS = set((Path(__file__).parent / 'nonbird-labels.txt').read_text(encoding='utf-8-sig').splitlines())
# BioCLIP separates birds from other animals far more reliably than the detector's labels.
CLASSIFIER_WEIGHT = .75
# Joining a track to one that starts soon after it ends (see stitch_tracks).
STITCH_MAX_GAP_MS = 1500
STITCH_MAX_SIZE_RATIO = 6
STITCH_MIN_SCORE = .55


def overlap(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection/union if union > 0 else 0.


def center(box):
    return ((box[0]+box[2])/2, (box[1]+box[3])/2)


def global_assignment(scores, minimum):
    """Maximum-total-score one-to-one matching, with an unmatched option per row.

    Rectangular primal/dual assignment, O(rows^2 * columns). At most 12 detector
    boxes per frame; no SciPy installation or exponential search is required.
    Rows are detections, columns are live tracks. Invalid pairs use score -1.
    """
    n = len(scores)
    if not n or not scores[0]:
        return []
    tracks = len(scores[0])
    costs = [[-(score-minimum) if score >= minimum else 1e6 for score in row] + [0.]*n for row in scores]
    m = tracks+n
    u, v, p, way = [0.]*(n+1), [0.]*(m+1), [0]*(m+1), [0]*(m+1)
    for i in range(1, n+1):
        p[0], j0 = i, 0
        distance, used = [math.inf]*(m+1), [False]*(m+1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, m+1):
                if used[j]:
                    continue
                current = costs[i0-1][j-1]-u[i0]-v[j]
                if current < distance[j]:
                    distance[j], way[j] = current, j0
                if distance[j] < delta:
                    delta, j1 = distance[j], j
            for j in range(m+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    distance[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return [(p[j]-1, j-1) for j in range(1, tracks+1)
            if p[j] and scores[p[j]-1][j-1] >= minimum]


def appearance_descriptor(image, box):
    """Compact color distribution from the central crop; no extra AI inference."""
    import cv2
    import numpy as np
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    dx, dy = (x2-x1)*.10, (y2-y1)*.10
    crop = image[max(0, int((y1+dy)*height)):min(height, int((y2-dy)*height)),
                 max(0, int((x1+dx)*width)):min(width, int((x2-dx)*width))]
    if not crop.size:
        return None
    hsv = cv2.cvtColor(cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV)
    hs = cv2.calcHist([hsv], [0, 1], None, [12, 4], [0, 180, 0, 256]).ravel()
    value = cv2.calcHist([hsv], [2], None, [8], [0, 256]).ravel()
    descriptor = np.sqrt(np.concatenate([.8*hs/hs.sum(), .2*value/value.sum()]))
    return [round(float(x), 5) for x in descriptor]


def appearance_similarity(a, b):
    if a is None or b is None or len(a) != len(b):
        return None
    norm = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b))/norm if norm else None


def resolve_categories(frames, labels=None, nonbirds=NONBIRD_LABELS):
    """Give each track one category, decided from all of its frames.

    The detector's per-frame label is kept as 'detector_category'. BioCLIP's top
    species votes bird or not bird, unless the species list has no non-bird names.
    """
    classifier_knows_nonbirds = labels is None or not nonbirds.isdisjoint(labels)
    votes = defaultdict(lambda: {'detected': 0., 'bird': 0., 'other': defaultdict(float), 'ranked': 0, 'ranked_bird': 0})
    for frame in frames:
        for animal in frame['animals']:
            vote = votes[animal['id']]
            category = animal.setdefault('detector_category', animal['category'])
            weight = animal.get('detection', .5)
            vote['detected'] += weight
            if category == 'bird':
                vote['bird'] += weight
            else:
                vote['other'][category] += weight
            if classifier_knows_nonbirds and animal.get('ranked'):
                vote['ranked'] += 1
                vote['ranked_bird'] += animal['ranked'][0]['label'] not in nonbirds
    resolved = {}
    for track, vote in votes.items():
        evidence = vote['bird']/vote['detected']
        if vote['ranked']:
            evidence = (1-CLASSIFIER_WEIGHT)*evidence + CLASSIFIER_WEIGHT*vote['ranked_bird']/vote['ranked']
        other = vote['other']
        resolved[track] = 'bird' if evidence >= .5 else max(sorted(other), key=other.get, default='animal')
    for frame in frames:
        for animal in frame['animals']:
            animal['category'] = resolved[animal['id']]


def _pieces(frames):
    """Each track's first and last sighting, colors, and summed species similarities."""
    pieces = {}
    for index, frame in enumerate(frames):
        for animal in frame['animals']:
            piece = pieces.get(animal['id'])
            if piece is None:
                piece = pieces[animal['id']] = {'first': (index, frame['ms'], animal), 'species': defaultdict(float)}
            piece['last'] = (index, frame['ms'], animal)
            for rank in animal.get('ranked') or []:
                piece['species'][rank['label']] += rank.get('similarity', 0)
    for piece in pieces.values():
        species = piece.pop('species')
        ranked = sorted(species, key=lambda label: (-species[label], label))
        piece['best'] = ranked[0] if ranked else None
        piece['top'] = set(ranked[:3])
    return pieces


def stitch_score(earlier, later):
    """How likely `later` continues `earlier` after a short gap; -1 when it can't."""
    _, end_ms, last = earlier['last']
    _, start_ms, first = later['first']
    gap = start_ms-end_ms
    if not 0 < gap <= STITCH_MAX_GAP_MS:
        return -1.
    # Each piece's best species guess must be among the other's top three: never join a
    # chickadee that left to a titmouse or nuthatch that arrived, or a bird to a squirrel.
    # (Merely sharing a name let look-alikes join, and a wrong join is named confidently.)
    if earlier['best'] and later['best'] and not (earlier['best'] in later['top'] and later['best'] in earlier['top']):
        return -1.
    a, b = last['box'], first['box']
    size = max(a[2]-a[0], a[3]-a[1], b[2]-b[0], b[3]-b[1], .02)
    ca, cb = center(a), center(b)
    distance = math.hypot(ca[0]-cb[0], ca[1]-cb[1])/size
    ratio = ((b[2]-b[0])*(b[3]-b[1]))/max(1e-8, (a[2]-a[0])*(a[3]-a[1]))
    if distance > 1. or not 1/STITCH_MAX_SIZE_RATIO <= ratio <= STITCH_MAX_SIZE_RATIO:
        return -1.
    appearance = appearance_similarity(last.get('appearance'), first.get('appearance'))
    if appearance is not None and appearance < .6:
        return -1.
    return (.35*math.exp(-distance*distance/.5) + .25*math.exp(-gap/1000)
            + .25*(.5 if appearance is None else appearance) + .15*math.exp(-abs(math.log(ratio))/2))


def stitch_tracks(frames):
    """Join tracks the frame-by-frame tracker split: the same animal, briefly missed.

    Offline, over the whole clip. Each track gets at most one continuation and one
    predecessor, by best total score, so two birds never share one. Returns the
    joined track numbers as {old: new}; boxes stay the detector's.
    """
    pieces = _pieces(frames)
    ids = sorted(pieces)
    matrix = [[stitch_score(pieces[a], pieces[b]) if a != b else -1. for b in ids] for a in ids]
    successor = {ids[row]: ids[column] for row, column in global_assignment(matrix, STITCH_MIN_SCORE)}
    predecessor = {later: earlier for earlier, later in successor.items()}
    joined = {}
    for track in ids:
        root = track
        while root in predecessor:
            root = predecessor[root]
        if root != track:
            joined[track] = root
    for frame in frames:
        for animal in frame['animals']:
            if animal['id'] in joined:
                animal['tracklet'] = animal['id']
                animal['id'] = joined[animal['id']]
    return joined


@dataclass
class Track:
    id: int
    box: list
    seen: float
    strong_seen: float
    appearance: list | None = None
    velocity: tuple = (0., 0.)
    strong_hits: int = 1
    observations: int = 1

    def predicted(self, ms):
        dt = min(350., ms-self.seen)
        dx, dy = self.velocity[0]*dt, self.velocity[1]*dt
        return [self.box[0]+dx, self.box[1]+dy, self.box[2]+dx, self.box[3]+dy]


class TemporalTracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 1
        self.last_ms = None
        self.confirmed_ids = set()
        self.stats = {'tracks_started': 0, 'gap_recoveries': 0, 'weak_recoveries': 0, 'ambiguous_restarts': 0}

    def score(self, detection, track, ms, weak=False):
        gap = ms-track.seen
        if weak and (track.strong_hits < 2 or ms-track.strong_seen > 1200 or gap > 400):
            return -1.
        box, old = detection['box'], track.box
        predicted = track.predicted(ms)
        w, h = max(.015, (old[2]-old[0]+box[2]-box[0])/2), max(.015, (old[3]-old[1]+box[3]-box[1])/2)
        a, b = center(predicted), center(box)
        distance = math.hypot((a[0]-b[0])/w, (a[1]-b[1])/h)
        ratio = ((box[2]-box[0])*(box[3]-box[1])) / max(1e-8, (old[2]-old[0])*(old[3]-old[1]))
        if not .25 <= ratio <= 4 or distance > (0.8 if weak else 1.4):
            return -1.
        appearance = appearance_similarity(track.appearance, detection.get('appearance'))
        if appearance is not None and appearance < .45:
            return -1.
        proximity = math.exp(-distance*distance/2)
        intersection = max(overlap(predicted, box), .8*overlap(old, box))
        if gap > 400 and (appearance is None or appearance < .8) and (intersection < .3 or distance > .5):
            return -1.
        if track.strong_hits < 2 and intersection < .1:
            return -1.
        if appearance is None:
            return .6*intersection + .4*proximity
        return .4*intersection + .4*proximity + .2*appearance

    def _associate(self, detections, tracks, ms, weak=False):
        minimum = .64 if weak else .48
        matrix = [[self.score(d, t, ms, weak) for t in tracks] for d in detections]
        assigned = global_assignment(matrix, minimum)
        ambiguous = set()
        # Exactly competing overlaps are an identity boundary. Prefer a fresh track
        # to letting an arbitrary assignment carry one bird's species into another.
        for row, column in assigned:
            score = matrix[row][column]
            rivals = [j for j, value in enumerate(matrix[row]) if j != column and value >= minimum and abs(value-score) < .015]
            if rivals and any(i != row and matrix[i][column] >= minimum and abs(matrix[i][column]-score) < .015
                              for i in range(len(detections))):
                ambiguous.update([column, *rivals])
        if ambiguous:
            for column in ambiguous:
                self.tracks.pop(tracks[column].id, None)
            self.stats['ambiguous_restarts'] += len(ambiguous)
        return [(row, tracks[column]) for row, column in assigned if column not in ambiguous]

    def update(self, detections, ms):
        if not math.isfinite(ms) or (self.last_ms is not None and ms <= self.last_ms):
            raise ValueError('Tracking requires strictly increasing frame timestamps')
        self.last_ms = ms
        self.tracks = {tid: track for tid, track in self.tracks.items()
                       if ms-track.seen <= (MAX_GAP_MS if track.strong_hits >= 2 else 250)
                       and ms-track.strong_seen <= 2000}
        usable = []
        for item in detections:
            box = item['box']
            if (len(box) == 4 and all(math.isfinite(v) for v in box)
                    and box[2] > box[0] and box[3] > box[1]
                    and math.isfinite(item['detection']) and item['detection'] >= LOW_CONFIDENCE):
                usable.append(item)
        # Spatial ordering makes births and ties independent of detector output order.
        usable.sort(key=lambda d: (*d['box'], d['category']))
        high = [d for d in usable if d['detection'] >= HIGH_CONFIDENCE]
        low = [d for d in usable if d['detection'] < HIGH_CONFIDENCE]
        outputs, used = [], set()
        for candidates, weak in [(high, False), (low, True)]:
            tracks = [t for tid, t in sorted(self.tracks.items()) if tid not in used]
            matched = self._associate(candidates, tracks, ms, weak)
            matched_rows = set()
            for index, track in matched:
                detection = candidates[index]
                gap = ms-track.seen
                strong_gap = ms-track.strong_seen
                old_center, new_center = center(track.box), center(detection['box'])
                if not weak:
                    measured = tuple((new_center[i]-old_center[i])/gap for i in (0, 1))
                    track.velocity = measured if track.strong_hits == 1 else tuple(.7*measured[i]+.3*track.velocity[i] for i in (0, 1))
                    track.strong_hits += 1
                    track.strong_seen = ms
                    desc = detection.get('appearance')
                    if desc is not None:
                        track.appearance = desc if track.appearance is None else [.8*a+.2*b for a, b in zip(track.appearance, desc)]
                    self.confirmed_ids.add(track.id)
                track.box, track.seen = list(detection['box']), ms
                track.observations += 1
                used.add(track.id)
                matched_rows.add(index)
                if gap > 250:
                    self.stats['gap_recoveries'] += 1
                if weak:
                    self.stats['weak_recoveries'] += 1
                outputs.append(dict(detection, id=track.id, tracking_state='weak' if weak else 'confirmed',
                                    reacquired=gap > 250 or (not weak and strong_gap > 250), gap_ms=gap))
            if not weak:
                for index, detection in enumerate(candidates):
                    if index in matched_rows:
                        continue
                    track = Track(self.next_id, list(detection['box']), ms, ms, detection.get('appearance'))
                    self.next_id += 1
                    self.tracks[track.id] = track
                    used.add(track.id)
                    self.stats['tracks_started'] += 1
                    outputs.append(dict(detection, id=track.id, tracking_state='tentative', reacquired=False, gap_ms=0))
        return outputs

    def finalize(self, frames, labels=None):
        joined = stitch_tracks(frames)
        # A joined track is confirmed when any of its pieces was.
        confirmed = self.confirmed_ids | {joined[track] for track in self.confirmed_ids if track in joined}
        resolve_categories(frames, labels)
        for frame in frames:
            for animal in frame['animals']:
                animal['track_confirmed'] = animal['id'] in confirmed
        return dict(self.stats, confirmed_tracks=len(self.confirmed_ids), stitched=len(joined))


def retrack_result(result, cancel=None, video=None, status=None, progress=None):
    """Re-associate saved detections without detector/BioCLIP inference.

    An optional video supplies color descriptors for legacy analyses that lack them.
    Original boxes, rankings and prior IDs remain available; missing detections are not made up.
    """
    from .cancel import check_cancel
    if cancel is not None:
        check_cancel(cancel)
    if result.get('tracker_version') == TRACKER_VERSION:
        return result
    output = copy.deepcopy(result)
    capture = None
    if video is not None and any('appearance' not in animal for frame in output['frames'] for animal in frame['animals']):
        import cv2
        capture = cv2.VideoCapture(str(video))
    tracker = TemporalTracker()
    try:
        if capture is not None and not capture.isOpened():
            raise ValueError('Cannot decode video for tracking upgrade')
        if status:
            status('Improving motion and appearance tracking from saved detections…')
        for index, frame in enumerate(output['frames']):
            if cancel is not None:
                check_cancel(cancel)
            image = None
            if capture is not None:
                if not capture.grab():
                    raise ValueError('Video ended before the saved detection frames')
                if frame['animals']:
                    ok, image = capture.retrieve()
                    if not ok:
                        raise ValueError('Cannot decode a saved detection frame')
            for animal in frame['animals']:
                animal.setdefault('original_id', animal['id'])
                animal.setdefault('detection', .5)
                # Re-track from what the detector saw, not a previous track decision.
                animal['category'] = animal.pop('detector_category', animal['category'])
                if image is not None:
                    animal['appearance'] = appearance_descriptor(image, animal['box'])
                for key in ('display_label', 'identification_note', 'sighting_id', 'tracklet'):
                    animal.pop(key, None)
            frame['animals'] = tracker.update(frame['animals'], frame['ms'])
            if progress and index % 15 == 0:
                progress(index+1, len(output['frames']))
        output['tracking_stats'] = tracker.finalize(output['frames'], output.get('labels'))
        output['tracker_version'] = TRACKER_VERSION
        return output
    finally:
        if capture is not None:
            capture.release()
