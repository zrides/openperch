"""Within-clip animal tracking with motion, appearance and global association.

Low-score detections can support existing tracks, never create new ones. Predicted
boxes are used for matching only: output boxes always come from the detector.
This is a small custom tracker, not the ByteTrack implementation or bird re-ID.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import math

TRACKER_VERSION = 1
HIGH_CONFIDENCE = .22
LOW_CONFIDENCE = .10
MAX_GAP_MS = 1200


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


@dataclass
class Track:
    id: int
    box: list
    category: str
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
        if detection['category'] != track.category:
            return -1.
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
        usable.sort(key=lambda d: (d['category'], *d['box']))
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
                    track = Track(self.next_id, list(detection['box']), detection['category'], ms, ms,
                                  detection.get('appearance'))
                    self.next_id += 1
                    self.tracks[track.id] = track
                    used.add(track.id)
                    self.stats['tracks_started'] += 1
                    outputs.append(dict(detection, id=track.id, tracking_state='tentative', reacquired=False, gap_ms=0))
        return outputs

    def finalize(self, frames):
        for frame in frames:
            for animal in frame['animals']:
                animal['track_confirmed'] = animal['id'] in self.confirmed_ids
        return dict(self.stats, confirmed_tracks=len(self.confirmed_ids))


def retrack_result(result, cancel=None, video=None, status=None, progress=None):
    """Re-associate saved detections without detector/BioCLIP inference.

    An optional video supplies color descriptors for legacy analyses. Original
    boxes, rankings and prior IDs remain available; missing detections are not made up.
    """
    from .cancel import check_cancel
    if cancel is not None:
        check_cancel(cancel)
    if result.get('tracker_version') == TRACKER_VERSION:
        return result
    import cv2
    output = copy.deepcopy(result)
    capture = cv2.VideoCapture(str(video)) if video is not None else None
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
                if image is not None:
                    animal['appearance'] = appearance_descriptor(image, animal['box'])
                for key in ('display_label', 'identification_note', 'sighting_id'):
                    animal.pop(key, None)
            frame['animals'] = tracker.update(frame['animals'], frame['ms'])
            if progress and index % 15 == 0:
                progress(index+1, len(output['frames']))
        output['tracking_stats'] = tracker.finalize(output['frames'])
        output['tracker_version'] = TRACKER_VERSION
        return output
    finally:
        if capture is not None:
            capture.release()
