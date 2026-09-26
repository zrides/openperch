"""Bounded, lossless frame prefetch. One owner per OpenCV decoder."""
import math
import queue
import threading

from .cancel import check_cancel

SUPPORTED_BATCH_SIZES = (1, 2, 4, 8, 16, 32)
MAX_BATCH_SIZE = max(SUPPORTED_BATCH_SIZES)


class VideoFrames:
    def __init__(self, path, cancel, buffer_size=2):
        self.path, self.cancel = str(path), cancel
        self.buffer_size = max(0, min(MAX_BATCH_SIZE, buffer_size))
        self.queue = queue.Queue(maxsize=max(1, self.buffer_size))
        self.stop = threading.Event()
        self.thread = None
        self.capture = None
        self.index = 0
        self.last_ms = -1
        self.info = None
        self.finished = False

    def _open(self):
        import cv2
        capture = cv2.VideoCapture(self.path)
        if not capture.isOpened():
            capture.release()
            raise ValueError('Could not open this video. Choose a completed, playable video file.')
        fps = capture.get(cv2.CAP_PROP_FPS)
        fps = fps if math.isfinite(fps) and fps > 0 else 30.
        info = {'fps': fps, 'frame_count': int(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
                'width': int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), 'height': int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}
        return capture, info

    def _read(self, capture, info):
        import cv2
        ok, frame = capture.read()
        if not ok:
            return None
        ms = capture.get(cv2.CAP_PROP_POS_MSEC)
        if not math.isfinite(ms) or ms <= self.last_ms:
            ms = max(self.last_ms+1000/info['fps'], self.index*1000/info['fps'])
        self.last_ms = ms
        item = (self.index, ms, frame)
        self.index += 1
        return item

    def _put(self, value):
        while not self.stop.is_set() and not self.cancel.is_set():
            try:
                self.queue.put(value, timeout=.05)
                return True
            except queue.Full:
                pass
        return False

    def _produce(self):
        capture = None
        try:
            capture, info = self._open()
            # Keep at most 1 GiB of decoded images queued, even with 8K sources.
            image_bytes = max(1, info['width']*info['height']*3)
            self.queue.maxsize = min(self.queue.maxsize, max(1, 1024**3//image_bytes))
            if not self._put(('info', info)):
                return
            while not self.stop.is_set() and not self.cancel.is_set():
                item = self._read(capture, info)
                if item is None or not self._put(('frame', item)):
                    break
        except Exception as exc:
            self._put(('error', exc))
        finally:
            if capture is not None:
                capture.release()
            self._put(('end', None))

    def _get(self):
        while True:
            check_cancel(self.cancel)
            try:
                kind, value = self.queue.get(timeout=.05)
                if kind == 'error':
                    raise value
                return kind, value
            except queue.Empty:
                if self.thread is not None and not self.thread.is_alive():
                    raise RuntimeError('Video decoder stopped without an end marker')

    def __enter__(self):
        check_cancel(self.cancel)
        try:
            if self.buffer_size:
                self.thread = threading.Thread(target=self._produce, name='bird-video-decode')
                self.thread.start()
                kind, self.info = self._get()
                if kind != 'info':
                    raise ValueError('Could not read video metadata')
            else:
                self.capture, self.info = self._open()
            return self
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        return self

    def __next__(self):
        if self.finished:
            raise StopIteration
        check_cancel(self.cancel)
        if self.thread:
            kind, value = self._get()
            if kind == 'end':
                self.finished = True
                raise StopIteration
            return value
        item = self._read(self.capture, self.info)
        if item is None:
            self.finished = True
            raise StopIteration
        return item

    def batches(self, size, max_bytes=1024**3):
        size = max(1, min(MAX_BATCH_SIZE, size))
        items = []
        buffered_bytes = 0
        for item in self:
            frame_bytes = item[2].nbytes
            if items and buffered_bytes+frame_bytes > max_bytes:
                yield items
                items, buffered_bytes = [], 0
            items.append(item)
            buffered_bytes += frame_bytes
            if len(items) == size:
                yield items
                items, buffered_bytes = [], 0
        if items:
            yield items

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        # Release queued full-resolution frames promptly after cancellation.
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break

    def __exit__(self, *_):
        self.close()
