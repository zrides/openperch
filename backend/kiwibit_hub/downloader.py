"""Download every Kiwibit cloud video to verified, resumable local MP4 files."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .kiwibit_api import KiwibitClient, load_config
from .media_proxy import MediaProxy
from .recordings import load_recordings

# Native Windows runs would otherwise flash a console window for every ffmpeg/ffprobe call.
BACKGROUND_PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def recording_path(output, event):
    serial = str(event.get("serialNumber") or "unknown-camera")
    safe_serial = "".join(char if char.isalnum() or char in "-_" else "_" for char in serial)
    safe_serial = safe_serial[:80] or "unknown-camera"
    try:
        stamp = datetime.fromtimestamp(int(event["startTime"]), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except (KeyError, ValueError, TypeError, OSError, OverflowError):
        stamp = "unknown-time"
    identity = {
        "serial": serial,
        "start": event.get("startTime"),
        "end": event.get("endTime"),
        "key": event.get("videoEventKey"),
        "libraryIds": event.get("libraryIds"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return output / safe_serial / f"{stamp}_{digest}.mp4"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_video(ffprobe, path):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL, creationflags=BACKGROUND_PROCESS_FLAGS,
    )
    if result.returncode:
        raise ValueError("ffprobe could not read the MP4")
    data = json.loads(result.stdout)
    if not any(stream.get("codec_type") == "video" for stream in data.get("streams", [])):
        raise ValueError("MP4 has no video stream")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("MP4 has no positive duration")
    return round(duration, 3)


def read_manifest(path):
    entries = {}
    if not path.exists():
        return entries
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if line.strip():
                try:
                    record = json.loads(line)
                    entries[record["file"]] = record
                except (ValueError, KeyError, TypeError):
                    print(f"Warning: ignoring unreadable manifest line {line_number}; source file preserved")
    return entries


def append_manifest(path, record):
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        sink.flush()
        os.fsync(sink.fileno())


def manifest_record(output, target, event, duration):
    before = target.stat()
    digest = file_hash(target)
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("MP4 changed during verification; retry when the file is stable")
    return {
        "file": str(target.relative_to(output)).replace("\\", "/"),
        "serialNumber": event.get("serialNumber"),
        "startTime": event.get("startTime"),
        "endTime": event.get("endTime"),
        "durationSeconds": duration,
        "bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest,
    }


def verify_existing(ffprobe, output, manifest_path, manifest, target, event=None, full=False):
    """Use saved verification for unchanged files; never bless a hash mismatch.

    Legacy manifests lack mtime_ns. Hash them once against the saved digest before
    recording a timestamp baseline. Matching bytes need no redundant media probe.
    """
    relative = str(target.relative_to(output)).replace("\\", "/")
    before = target.stat()
    prior = manifest.get(relative)
    trusted = (prior and isinstance(prior.get('sha256'), str)
               and re.fullmatch(r'[0-9a-f]{64}', prior['sha256'])
               and isinstance(prior.get('durationSeconds'), (int, float))
               and prior['durationSeconds'] > 0)
    same_size = prior and prior.get('bytes') == before.st_size
    if (not full and trusted and same_size
            and prior.get('mtime_ns') == before.st_mtime_ns):
        return 'skipped', target
    digest = file_hash(target)
    if prior and prior.get('sha256') != digest:
        raise ValueError("existing MP4 hash differs from the manifest; preserved without overwrite")
    duration = (prior['durationSeconds'] if trusted and same_size and not full
                else probe_video(ffprobe, target))
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("MP4 changed during verification; retry when the file is stable")
    event = event or {}
    record = dict(prior or {}, file=relative, bytes=after.st_size,
                  mtime_ns=after.st_mtime_ns, sha256=digest, durationSeconds=duration)
    for key in ('serialNumber', 'startTime', 'endTime'):
        if key in event:
            record[key] = event[key]
    append_manifest(manifest_path, record)
    manifest[relative] = record
    return 'skipped', target


EMPTY_AUDIO = re.compile(r"sample rate not set|channel layout not set", re.IGNORECASE)


def remux(ffmpeg, url, target, timeout, audio=True):
    """Copy the stream into a fresh unique .part.mp4; returns (partial, completed process)."""
    partial = target.with_name(target.stem + "." + uuid.uuid4().hex[:8] + ".part.mp4")
    streams = ["-map", "0:v:0", "-map", "0:a:0?"] if audio else ["-map", "0:v:0", "-an"]
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", url,
             *streams, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(partial)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=BACKGROUND_PROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"download timed out; {discard_if_empty(partial)}") from exc
    return partial, result


def discard_if_empty(partial):
    """Empty partials carry no diagnostic value; keep anything with bytes for inspection."""
    try:
        if partial.stat().st_size == 0:
            partial.unlink()
            return "empty partial removed"
    except FileNotFoundError:
        return "no partial written"
    return f"partial kept at {partial.name}"


def download_one(ffmpeg, ffprobe, output, manifest_path, manifest, event, timeout, verify=False):
    target = recording_path(output, event)
    relative = str(target.relative_to(output)).replace("\\", "/")
    if target.exists():
        return verify_existing(ffprobe, output, manifest_path, manifest, target, event, full=verify)

    target.parent.mkdir(parents=True, exist_ok=True)
    audio = True
    proxy = MediaProxy()
    try:
        local_url = proxy.url_for(event["videoUrl"])
        partial, result = remux(ffmpeg, local_url, target, timeout, audio=True)
        if result.returncode and EMPTY_AUDIO.search(result.stderr.decode("utf-8", errors="replace")):
            # Some cameras advertise an AAC track with no samples (sample rate 0).
            # The MP4 muxer rejects it; the video itself is intact, so keep it without audio.
            discard_if_empty(partial)
            audio = False
            partial, result = remux(ffmpeg, local_url, target, timeout, audio=False)
    finally:
        proxy.close()
    if result.returncode:
        details = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = re.sub(r"https?://\S+", "[media URL]", details[-1])[:180] if details else "no details"
        raise RuntimeError(f"ffmpeg exited {result.returncode} ({detail}); {discard_if_empty(partial)}")
    try:
        duration = probe_video(ffprobe, partial)
    except Exception as exc:
        raise ValueError(f"download verification failed ({exc}); partial kept at {partial.name}") from exc
    # On Windows, rename refuses to replace an existing destination.
    if target.exists():
        raise FileExistsError(f"destination appeared during download; partial kept at {partial.name}")
    partial.rename(target)
    record = manifest_record(output, target, event, duration)
    if not audio:
        record["audioDropped"] = True
    append_manifest(manifest_path, record)
    manifest[relative] = record
    return "saved", target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download all Kiwibit cloud videos as local MP4 files")
    parser.add_argument("--output", type=Path, default=config.DOWNLOADS,
                        help="destination folder (default: $KIWIBIT_DATA_DIR/downloads)")
    parser.add_argument("--camera", help="download only this camera serial number")
    parser.add_argument("--limit", type=int, help="download at most this many videos (useful for a trial run)")
    parser.add_argument("--dry-run", action="store_true", help="list planned files without downloading")
    parser.add_argument("--verify-existing", action="store_true",
                        help="fully hash and probe existing videos instead of skipping unchanged verified files")
    parser.add_argument("--timeout", type=int, default=300, help="seconds allowed per video (default: 300)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not args.dry_run and (not ffmpeg or not ffprobe):
        parser.error("ffmpeg and ffprobe must be on PATH")

    output = args.output.expanduser().resolve()
    client = KiwibitClient(load_config())
    devices = client.devices()
    if args.camera:
        devices = [device for device in devices if device.get("serialNumber") == args.camera]
        if not devices:
            parser.error("camera serial number was not found on this account")
    if not devices:
        print("No cameras found on this account.")
        return 0

    events = []
    for device in devices:
        serial = device["serialNumber"]
        print(f"Listing cloud videos for {device.get('deviceName') or 'Camera'} ({serial})…", flush=True)
        recordings, seen, total = load_recordings(client, serial)
        print(f"  {len(recordings)} playable videos in {seen} cloud events", flush=True)
        events.extend(recordings)
    events.sort(key=lambda event: event.get("startTime") or 0, reverse=True)
    if args.limit is not None:
        events = events[:args.limit]
    print(f"Destination: {output}")
    print(f"Videos to check: {len(events)}")
    if args.dry_run:
        for event in events:
            print(recording_path(output, event))
        return 0

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "download_manifest.jsonl"
    manifest = read_manifest(manifest_path)
    saved = skipped = failed = 0
    for index, event in enumerate(events, 1):
        target = recording_path(output, event)
        print(f"[{index}/{len(events)}] {target.name}: ", end="", flush=True)
        try:
            action, path = download_one(ffmpeg, ffprobe, output, manifest_path, manifest, event, args.timeout,
                                        verify=args.verify_existing)
            if action == "saved":
                saved += 1
                print(f"saved ({path.stat().st_size:,} bytes)", flush=True)
            else:
                skipped += 1
                print("already verified", flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failed += 1
            print(f"FAILED: {exc}", flush=True)
    print(f"Done: {saved} saved, {skipped} already verified, {failed} failed. Files: {output}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Finished files and any partial downloads were kept.", file=sys.stderr)
        raise SystemExit(130)
