"""Read every playable event from the existing Kiwibit API client."""

import time


PAGE_SIZE = 100


def load_recordings(client, serial, progress=None, page_size=PAGE_SIZE):
    """Return all cloud recordings for one camera, newest first.

    The library endpoint is paged. A single large request does not return the
    whole account history, so keep requesting until the reported count is met
    or the service returns an empty page.
    """
    end_time = int(time.time())
    offset = 0
    events = []
    total = None
    while True:
        response = client.clips(0, end_time, serials=[serial], limit=page_size, offset=offset)
        page = response.get("list") or []
        if not isinstance(page, list):
            raise ValueError("Kiwibit returned an invalid recordings list")
        count = response.get("libraryCount")
        if isinstance(count, int) and count >= 0:
            total = count
        if not page:
            break
        events.extend(page)
        offset += len(page)
        if progress:
            progress(offset, total)
        if total is not None and offset >= total:
            break
    recordings = [event for event in events if event.get("videoUrl")]
    return recordings, len(events), total
