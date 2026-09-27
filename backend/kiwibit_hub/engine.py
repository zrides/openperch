"""Local animal detection and BioCLIP crop classification, independent of the UI."""
from __future__ import annotations

import gzip
import bisect
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import config
from .cancel import Cancelled, check_cancel
from .video_pipeline import MAX_BATCH_SIZE, SUPPORTED_BATCH_SIZES

DATA = config.CACHE
MODELS = config.MODELS
os.environ.setdefault("HF_HOME", str(MODELS / "huggingface"))
os.environ.setdefault("YOLO_CONFIG_DIR", str(DATA / "ultralytics"))
os.environ.setdefault("XDG_CACHE_HOME", str(MODELS / "cache"))
os.environ.setdefault("YOLO_AUTOINSTALL", "false")

VERSION = 3
DEFAULT_DETECTOR_BATCH = 4
DEFAULT_CLASSIFICATION_BATCH = 32
ANIMALS = ["bird", "squirrel", "chipmunk", "rabbit", "raccoon", "opossum", "deer",
           "fox", "cat", "dog", "bear", "rodent", "horse", "cow", "sheep", "goat",
           "pig", "elephant", "giraffe", "zebra", "monkey", "lizard", "snake", "turtle", "frog"]


def read_labels(path):
    labels = list(dict.fromkeys(line.strip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
                               if line.strip() and not line.lstrip().startswith("#")))
    if len(labels) < 2:
        raise ValueError("The species file must contain at least two names, one per line.")
    return labels


def restrict_rankings(result, labels):
    """Saved rankings limited to the current species list.

    Removing a species then applies to earlier analyses without running the model
    again: the next species in each saved top five moves up. A ranking whose five
    are all removed is kept as it was.
    """
    allowed = set(labels)
    return {**result, 'frames': [
        {**frame, 'animals': [dict(animal, ranked=[rank for rank in animal['ranked'] if rank['label'] in allowed]
                                            or animal['ranked']) if animal.get('ranked') else animal
                              for animal in frame['animals']]}
        for frame in result['frames']]}


def cache_key(video, model_dir, labels, version=VERSION):
    def signature(path):
        path = Path(path).resolve()
        stat = path.stat()
        return [str(path), stat.st_size, stat.st_mtime_ns]
    spec = {"version": version, "video": signature(video), "labels": labels,
            "model": signature(Path(model_dir) / "open_clip_model.safetensors"),
            "config": signature(Path(model_dir) / "open_clip_config.json"), "detector": ANIMALS}
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def read_cache(key, version=VERSION):
    path = DATA / "analysis" / f"{key}.json.gz"
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            result = json.load(source)
        if result.get("complete") and result.get("frames") and result.get("version") == version:
            return result
    except (OSError, ValueError, EOFError):
        pass
    return None


def write_cache(key, result):
    folder = DATA / "analysis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.json.gz"
    temp = path.with_suffix(".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as sink:
        json.dump(result, sink, separators=(",", ":"))
    temp.replace(path)


def decoded_enough(decoded, frame_count, fps):
    """Whether decoding reached the end of the video.

    Containers report an *estimated* frame count for variable-frame-rate video
    (e.g. clips recorded from live view), so allow a shortfall of up to one
    second or 10% of the frames, but never more than half the video. A
    truncated, half-written file still fails.
    """
    if frame_count <= 0:
        return True
    allowance = min(max(2, int(fps), int(frame_count * 0.1)), frame_count // 2)
    return decoded >= frame_count - allowance


def iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def animals_at(frames, timestamps, milliseconds, fps):
    """Use the decoded frame timestamp, and never extend boxes across a gap."""
    index = bisect.bisect_right(timestamps, milliseconds + 1) - 1
    if 0 <= index < len(timestamps) and abs(milliseconds - timestamps[index]) <= max(100, 2000 / fps):
        return frames[index]["animals"]
    return []


class Engine:
    def __init__(self, detector_batch_size=DEFAULT_DETECTOR_BATCH, classification_batch_size=DEFAULT_CLASSIFICATION_BATCH, decode_queue=None):
        self.model_identity = None
        self.model = self.preprocess = self.detector = None
        self.labels = None
        self.detector_batch_size = max(1, min(MAX_BATCH_SIZE, detector_batch_size))
        self.classification_batch_size = max(1, min(MAX_BATCH_SIZE, classification_batch_size))
        self.auto_decode_queue = decode_queue is None
        self.decode_queue = (max(self.detector_batch_size, self.classification_batch_size) if self.auto_decode_queue
                             else max(0, min(MAX_BATCH_SIZE, decode_queue)))
        self.detector_batches = []
        self.classification_batches = []

    @staticmethod
    def _cuda_oom(exc):
        import torch
        return isinstance(exc, torch.cuda.OutOfMemoryError)

    def detect_batch(self, images, cancel):
        """One model owner; shrink batches on CUDA memory pressure without dropping frames."""
        from .tracking import LOW_CONFIDENCE
        results = []
        start = 0
        while start < len(images):
            check_cancel(cancel)
            end = min(len(images), start+self.detector_batch_size)
            batch = images[start:end]
            try:
                predictions = self.detector.predict(batch if len(batch) > 1 else batch[0],
                    imgsz=640, conf=LOW_CONFIDENCE, iou=.45, agnostic_nms=True,
                    device=self.device, max_det=12, verbose=False)
            except Exception as exc:
                if len(batch) == 1 or not self._cuda_oom(exc):
                    raise
                self.detector_batch_size = max(1, len(batch)//2)
                import torch
                torch.cuda.empty_cache()
                continue
            if len(predictions) != len(batch):
                raise ValueError('Detector did not return one result per input frame')
            results.extend(predictions)
            self.detector_batches.append(len(batch))
            start = end
        return results

    def classify_many(self, crops, cancel):
        rankings = []
        start = 0
        while start < len(crops):
            check_cancel(cancel)
            end = min(len(crops), start+self.classification_batch_size)
            try:
                ranked = self.classify(crops[start:end])
            except Exception as exc:
                if end-start == 1 or not self._cuda_oom(exc):
                    raise
                self.classification_batch_size = max(1, (end-start)//2)
                import torch
                torch.cuda.empty_cache()
                continue
            if len(ranked) != end-start:
                raise ValueError('Classifier did not return one ranking per crop')
            rankings.extend(ranked)
            self.classification_batches.append(end-start)
            start = end
        return rankings

    def prepare(self, model_dir, labels, status, cancel):
        import torch
        import open_clip

        check_cancel(cancel)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        model_dir = Path(model_dir).resolve()
        config_path = model_dir / "open_clip_config.json"
        weights = model_dir / "open_clip_model.safetensors"
        identity = (str(weights), weights.stat().st_mtime_ns, config_path.stat().st_mtime_ns)
        if identity != self.model_identity:
            status(f"Loading BioCLIP on {torch.cuda.get_device_name(0) if self.device == 'cuda' else 'CPU'}…")
            self.model = None
            if self.device == "cuda":
                torch.cuda.empty_cache()
            # Local config + safetensors; no duplicate download of the user's weights.
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config_dir = DATA / "model-config"
            config_dir.mkdir(parents=True, exist_ok=True)
            local_config = config_dir / "LocalBioCLIP.json"
            local_config.write_text(json.dumps(config["model_cfg"]), encoding="utf-8")
            open_clip.add_model_config(local_config)
            self.model, _, self.preprocess = open_clip.create_model_and_transforms(
                "LocalBioCLIP", pretrained=str(weights), device=self.device,
                precision="fp16" if self.device == "cuda" else "fp32",
                **{"image_" + key: value for key, value in config["preprocess_cfg"].items()
                   if key in ("mean", "std", "interpolation", "resize_mode")},
            )
            self.model.eval()
            self.tokenizer = open_clip.get_tokenizer("LocalBioCLIP")
            self.model_identity = identity
            self.labels = None
        check_cancel(cancel)
        if labels != self.labels:
            status(f"Preparing {len(labels)} candidate animal names…")
            from safetensors.torch import load_file, save_file
            signature = hashlib.sha256(json.dumps([identity, labels]).encode()).hexdigest()
            text_cache = DATA / "text-features" / f"{signature}.safetensors"
            if text_cache.is_file():
                self.text_features = load_file(str(text_cache))["features"].to(self.device, dtype=self.dtype)
            else:
                vectors = []
                with torch.inference_mode():
                    for start in range(0, len(labels), 32):
                        check_cancel(cancel)
                        tokens = self.tokenizer([f"a photo of a {name}" for name in labels[start:start + 32]]).to(self.device)
                        vectors.append(self.model.encode_text(tokens, normalize=True))
                self.text_features = torch.cat(vectors)
                text_cache.parent.mkdir(parents=True, exist_ok=True)
                save_file({"features": self.text_features.cpu().contiguous()}, str(text_cache))
            self.labels = list(labels)
        check_cancel(cancel)
        if self.detector is None:
            (Path(os.environ["YOLO_CONFIG_DIR"]) / "Ultralytics").mkdir(parents=True, exist_ok=True)
            from ultralytics import YOLOWorld
            import ultralytics.utils
            status("Loading the animal detector (first launch downloads its weights)…")
            MODELS.mkdir(parents=True, exist_ok=True)
            ultralytics.utils.SETTINGS.update({"weights_dir": str(MODELS), "sync": False})
            ultralytics.utils.WEIGHTS_DIR = MODELS
            detector = YOLOWorld(str(MODELS / "yolov8s-worldv2.pt"))
            detector.set_classes(ANIMALS)
            self.detector = detector
        check_cancel(cancel)

    def classify(self, rgb_crops):
        import torch
        from PIL import Image
        if not rgb_crops:
            return []
        inputs = torch.stack([self.preprocess(Image.fromarray(crop)) for crop in rgb_crops]).to(self.device, dtype=self.dtype)
        with torch.inference_mode():
            features = self.model.encode_image(inputs, normalize=True)
            similarities = features @ self.text_features.T
            scores = (self.model.logit_scale.exp() * similarities).float().softmax(dim=-1)
            matches, indices = similarities.float().topk(min(5, len(self.labels)), dim=-1)
            values = scores.gather(1, indices)
        return [[{"label": self.labels[i], "score": float(value), "similarity": round(float(match), 5)}
                 for value, match, i in zip(row_values, row_matches, row_indices)]
                for row_values, row_matches, row_indices in zip(values.cpu().tolist(), matches.cpu().tolist(), indices.cpu().tolist())]

    def analyze(self, video, model_dir, labels, status, progress, cancel, force=False):
        import cv2
        from .tracking import TemporalTracker, TRACKER_VERSION, appearance_descriptor, retrack_result
        key = cache_key(video, model_dir, labels)
        cached = None if force else read_cache(key)
        if cached and cached.get('tracker_version') == TRACKER_VERSION:
            check_cancel(cancel)
            status("Loaded saved analysis")
            return cached
        # Saved detections from an older tracker only need re-tracking, not inference.
        legacy = cached or (None if force else read_cache(cache_key(video, model_dir, labels, version=2), version=2))
        if legacy:
            upgraded = retrack_result(legacy, cancel, video, status, progress)
            check_cancel(cancel)
            if cache_key(video, model_dir, labels) != key:
                raise ValueError('Video or model changed while updating saved tracking')
            upgraded['version'] = VERSION
            write_cache(key, upgraded)
            return upgraded
        started = time.perf_counter()
        self.prepare(model_dir, labels, status, cancel)
        prepared = time.perf_counter()
        self.detector_batches = []
        self.classification_batches = []
        last_status = prepared
        timings = {'model_seconds': prepared-started, 'detector_seconds': 0., 'classifier_seconds': 0.}
        frames = []
        tracker = TemporalTracker()
        classifications = {}
        index = 0
        status("Detecting animals and ranking species…")
        from .video_pipeline import VideoFrames
        with VideoFrames(video, cancel, self.decode_queue) as source:
            fps, frame_count = source.info['fps'], source.info['frame_count']
            width, height = source.info['width'], source.info['height']
            # Gather crops over a wider chronological window without forcing the
            # detector to use the same batch size (and different numeric kernels).
            for batch in source.batches(max(self.detector_batch_size, self.classification_batch_size)):
                check_cancel(cancel)
                began = time.perf_counter()
                predictions = self.detect_batch([entry[2] for entry in batch], cancel)
                timings['detector_seconds'] += time.perf_counter()-began
                crops, destinations = [], []
                # Tracking is still strictly chronological. Each scheduled crop gets
                # its own ranking list; future classifications never backfill older views.
                for (_, time_ms, frame), prediction in zip(batch, predictions):
                    check_cancel(cancel)
                    boxes = prediction.boxes.xyxy.cpu().tolist()
                    confidence = prediction.boxes.conf.cpu().tolist()
                    categories = prediction.boxes.cls.int().cpu().tolist()
                    current, detections = [], []
                    for box, conf, category in zip(boxes, confidence, categories):
                        normalized = [box[0] / width, box[1] / height, box[2] / width, box[3] / height]
                        detections.append({'box': [round(value, 5) for value in normalized],
                            'detection': round(conf, 4), 'category': ANIMALS[category],
                            'appearance': appearance_descriptor(frame, normalized)})
                    for item in tracker.update(detections, time_ms):
                        tid = item['id']
                        track = classifications.setdefault(tid, {'ranked': [], 'classified': -10000})
                        item['ranked'] = track['ranked']
                        current.append(item)
                        if item['tracking_state'] != 'weak' and (item['reacquired'] or time_ms - track['classified'] >= 500):
                            x1, y1, x2, y2 = (item['box'][0]*width, item['box'][1]*height,
                                              item['box'][2]*width, item['box'][3]*height)
                            pad_x, pad_y = (x2-x1)*.05, (y2-y1)*.05
                            crop = frame[max(0, int(y1-pad_y)):min(height, int(y2+pad_y)),
                                         max(0, int(x1-pad_x)):min(width, int(x2+pad_x))]
                            if crop.size:
                                destination = []
                                track.update(ranked=destination, classified=time_ms)
                                item['ranked'] = destination
                                crops.append(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
                                destinations.append(destination)
                    # Unrounded: frames of variable-rate live clips can be under 0.01 ms apart,
                    # and saved timestamps must stay strictly increasing for re-tracking.
                    frames.append({'ms': time_ms, 'animals': current})
                    classifications = {tid: state for tid, state in classifications.items() if tid in tracker.tracks}
                    index += 1
                began = time.perf_counter()
                for destination, ranked in zip(destinations, self.classify_many(crops, cancel)):
                    destination.extend(ranked)
                timings['classifier_seconds'] += time.perf_counter()-began
                progress(index, frame_count)
                now = time.perf_counter()
                if now-last_status >= 1:
                    rate = index/max(.001, now-prepared)
                    remaining = max(0, frame_count-index)/rate
                    status(f'Analyzing · {rate:.1f} frames/sec · approximately {remaining:.0f}s remaining in analysis')
                    last_status = now
        check_cancel(cancel)
        if not frames:
            raise ValueError("No video frames could be decoded.")
        if not decoded_enough(index, frame_count, fps):
            raise ValueError(f"The decoder stopped at frame {index} of {frame_count}. The video may be incomplete.")
        if cache_key(video, model_dir, labels) != key:
            raise ValueError("The video or model changed during analysis. Retry once the file is complete.")
        result = {"version": VERSION, "complete": True, "video": str(Path(video).resolve()),
                  "width": width, "height": height, "fps": fps, "labels": labels, "frames": frames,
                  "model": str(model_dir), "device": self.device,
                  "tracker_version": TRACKER_VERSION, "tracking_stats": tracker.finalize(frames, labels),
                  'performance': {**{k: round(v, 3) for k, v in timings.items()},
                      'analysis_seconds': round(time.perf_counter()-prepared, 3),
                      'detector_batch': self.detector_batch_size, 'classifier_batch': self.classification_batch_size,
                      'largest_detector_batch': max(self.detector_batches, default=0),
                      'largest_classifier_batch': max(self.classification_batches, default=0),
                      'detector_calls': len(self.detector_batches), 'classifier_calls': len(self.classification_batches),
                      'decode_queue': self.decode_queue}}
        write_cache(key, result)
        progress(index, index)
        return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--model", type=Path, default=config.BIOCLIP)
    parser.add_argument("--labels", type=Path, default=config.SPECIES)
    parser.add_argument('--fresh', action='store_true', help='run detection and classification again instead of using saved analysis')
    parser.add_argument('--batch-size', type=int, choices=SUPPORTED_BATCH_SIZES, default=DEFAULT_DETECTOR_BATCH)
    parser.add_argument('--crop-batch-size', type=int, choices=SUPPORTED_BATCH_SIZES, default=DEFAULT_CLASSIFICATION_BATCH)
    parser.add_argument('--decode-queue', type=int, choices=[0, *SUPPORTED_BATCH_SIZES], default=None,
                        help='prefetched frames; default matches the larger GPU batch, with a memory bound')
    args = parser.parse_args()
    result = Engine(args.batch_size, args.crop_batch_size, args.decode_queue).analyze(args.video, args.model, read_labels(args.labels),
                              lambda message: print(message, flush=True),
                              lambda n, total: print(f"{n}/{total}", flush=True), threading.Event(), force=args.fresh)
    populated = [frame for frame in result["frames"] if frame["animals"]]
    print(f"Analyzed {len(result['frames'])} frames; {len(populated)} contain detections")
    if populated:
        print(json.dumps(populated[len(populated) // 2], indent=2))
