"""Bring an existing downloads folder into the hub without copying or changing it.

Each finished MP4 is hard-linked into the hub's downloads folder (same bytes on
disk, no extra space; falls back to a copy across drives) and its verified
manifest record comes along, so Fetch treats it as already downloaded.

    python -m kiwibit_hub.importer ../kiwibit-downloads
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from . import config
from .catalog import EXTENSIONS
from .downloader import append_manifest, read_manifest


def link_or_copy(source: Path, target: Path) -> str:
    try:
        os.link(source, target)
        return 'linked'
    except OSError:
        shutil.copy2(source, target)
        return 'copied'


def import_downloads(source, destination=None, log=print):
    source = Path(source).resolve()
    destination = Path(destination or config.DOWNLOADS).resolve()
    if not source.is_dir():
        raise ValueError(f'{source} is not a folder')
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Source and destination folders must be separate')
    destination.mkdir(parents=True, exist_ok=True)
    old_manifest = read_manifest(source / 'download_manifest.jsonl')
    manifest_path = destination / 'download_manifest.jsonl'
    new_manifest = read_manifest(manifest_path)
    counts = {'linked': 0, 'copied': 0, 'present': 0, 'skipped': 0}
    for file in sorted(source.rglob('*')):
        if (not file.is_file() or file.suffix.lower() not in EXTENSIONS or '.part.' in file.name
                or file.stat().st_size == 0):
            continue
        relative = file.relative_to(source).as_posix()
        target = destination / relative
        if target.exists():
            same = target.stat().st_size == file.stat().st_size
            counts['present' if same else 'skipped'] += 1
            if not same:
                log(f'Skipped {relative}: a different file already exists in the hub')
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        counts[link_or_copy(file, target)] += 1
        record = old_manifest.get(relative)
        if record and relative not in new_manifest:
            append_manifest(manifest_path, record)
            new_manifest[relative] = record
    log(f"Imported {counts['linked']} linked and {counts['copied']} copied; "
        f"{counts['present']} already present; {counts['skipped']} skipped.")
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('source', type=Path, help='existing downloads folder (left unchanged)')
    parser.add_argument('--to', type=Path, default=None, help='hub downloads folder (default: $KIWIBIT_DATA_DIR/downloads)')
    args = parser.parse_args(argv)
    import_downloads(args.source, args.to)


if __name__ == '__main__':
    main()
