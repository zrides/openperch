"""Which cloud clips the SD card already holds, so the library can show the card copy instead.

The same moment on the cloud and on the card is the same footage, but the two sides time it
differently: the cloud labels a clip by its event start, the card by the camera's clock. For
one camera the difference is steady (Kiwibit cloud starts read about 3 s early), so it is
measured from the clips themselves rather than assumed. Nothing here decodes video: start
times and lengths are enough. Anything unknown keeps the cloud clip visible.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import statistics
import subprocess
from datetime import datetime, timezone

from . import config

# Downloaded recordings are named <UTC start>_<digest>.mp4, e.g. 20260925T185753Z_3a88a9623053ee38.mp4.
RECORDED_AT = re.compile(r'^(\d{8}T\d{6}Z)_')
# Firmware uses either 12-hour AM/PM or 24-hour local time, with a UTC offset.
CARD_RECORDED_AT = re.compile(r'^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})(AM|PM)?([+-]\d{2})-(\d{2})')

# Where a recording came from, decided when it joins the library.
ORIGINS = ('cloud', 'sdcard', 'live', 'unknown')

PAIR_WINDOW = 10.0  # s: a card clip starting this close to a cloud clip counts as the same event
MIN_PAIRS = 5       # with fewer pairs the offset stays 0
MAX_SPREAD = 1.0    # s: the pairs' interquartile range must be this tight to trust their median
JOIN_GAP = 1.0      # s: card files this close together are one continuous recording
START_SLACK = 1.0   # s
# Cloud lengths run a few seconds long: their timestamps jump and their last frames freeze.
END_SLACK = 3.0     # s

PROBE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def recorded_at(name):
    """UTC start of a recording as ISO text, from its file name; None when the name has no time."""
    match = RECORDED_AT.match(name)
    try:
        if match:
            return datetime.strptime(match.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).isoformat()
        card = CARD_RECORDED_AT.match(name)
        if card:
            date, hour, minute, second, half, zone_hours, zone_minutes = card.groups()
            hour = int(hour)
            if half:
                if not 1 <= hour <= 12:
                    return None
                hour = hour % 12 + (12 if half == 'PM' else 0)
            elif not 0 <= hour <= 23:
                return None
            local = datetime.fromisoformat(f'{date}T{hour:02d}:{minute}:{second}{zone_hours}:{zone_minutes}')
            return local.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass  # a date that does not exist
    return None


def start_seconds(name):
    text = recorded_at(name)
    return datetime.fromisoformat(text).timestamp() if text else None


def origin_of(stored, cloud_files):
    """Origin of a recording from the inbox it arrived through (its catalog path)."""
    if stored.startswith(config.SDCARD_FOLDER + '/'):
        return 'sdcard'
    if stored.startswith('downloads/live/'):
        return 'live'
    if stored in cloud_files:
        return 'cloud'
    return 'unknown'


def read_cloud_files(manifest):
    """Catalog paths of the downloads the cloud manifest vouches for."""
    files = set()
    try:
        with open(manifest, encoding='utf-8') as source:
            for line in source:
                try:
                    files.add('downloads/' + json.loads(line)['file'])
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return files


def probe(ffprobe, path):
    """(length in seconds, creation time as epoch seconds or None). Length 0 means unplayable."""
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration:format_tags=creation_time:stream=codec_type',
             '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, creationflags=PROBE_FLAGS)
        data = json.loads(result.stdout) if not result.returncode else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0, None
    if not any(stream.get('codec_type') == 'video' for stream in data.get('streams', [])):
        return 0.0, None
    media = data.get('format', {})
    try:
        length = float(media.get('duration') or 0)
    except ValueError:
        length = 0.0
    created = None
    try:
        text = media.get('tags', {}).get('creation_time')
        created = datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() if text else None
    except (AttributeError, ValueError):
        pass
    return (round(length, 3) if math.isfinite(length) and length > 0 else 0.0), created


def usable(start, length):
    return (start is not None and length is not None and math.isfinite(start)
            and math.isfinite(length) and length > 0)


def estimate_offset(cloud_starts, card_starts):
    """(seconds to add to cloud times to get card times, number of pairs it rests on).

    Each cloud clip pairs with the card clip starting nearest to it, if within PAIR_WINDOW.
    Too few pairs, or pairs that disagree, give 0: then little is hidden, never too much.
    """
    cards = sorted(card_starts)
    differences = []
    for start in cloud_starts:
        index = bisect.bisect_left(cards, start)
        near = [cards[i] - start for i in (index - 1, index) if 0 <= i < len(cards)]
        if near:
            nearest = min(near, key=abs)
            if abs(nearest) <= PAIR_WINDOW:
                differences.append(nearest)
    if len(differences) < MIN_PAIRS:
        return 0.0, len(differences)
    low, _, high = statistics.quantiles(differences, n=4)
    if high - low > MAX_SPREAD:
        return 0.0, len(differences)
    return statistics.median(differences), len(differences)


def coverage(cloud, cards, offset):
    """{cloud id: {'id': card id, 'offset_ms': where the moment starts in it}} for covered cloud clips.

    cloud and cards are (id, start seconds, length seconds). A cloud clip is covered when, shifted
    by `offset`, it lies within one card clip or a run of card clips with gaps up to JOIN_GAP,
    give or take START_SLACK at its start and END_SLACK at its end.
    """
    spans = []  # [start, end, [(card start, card id), ...]]
    for identity, start, length in sorted((c for c in cards if usable(c[1], c[2])), key=lambda c: c[1]):
        if spans and start - spans[-1][1] <= JOIN_GAP:
            spans[-1][1] = max(spans[-1][1], start + length)
            spans[-1][2].append((start, identity))
        else:
            spans.append([start, start + length, [(start, identity)]])
    starts = [span[0] for span in spans]
    covered = {}
    for identity, start, length in cloud:
        if not usable(start, length):
            continue
        begin = start + offset
        end = begin + length
        index = bisect.bisect_right(starts, begin + START_SLACK) - 1
        if index < 0:
            continue
        span_start, span_end, members = spans[index]
        if begin >= span_end or end > span_end + END_SLACK:
            continue
        # The card file holding the clip's first moment.
        moment = max(begin, span_start)
        card_start, card_id = members[bisect.bisect_right([m[0] for m in members], moment) - 1]
        covered[identity] = {'id': card_id, 'offset_ms': round((moment - card_start) * 1000)}
    return covered
