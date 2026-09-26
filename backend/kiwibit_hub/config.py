"""Runtime locations, all overridable by environment so the same code runs natively or in Docker.

Layout under KIWIBIT_DATA_DIR (default: <repo>/data):
    library/    SQLite catalog, saved analyses, bird photos
    downloads/  cloud recordings (MP4) and download_manifest.jsonl
    cache/      analysis and text-feature caches
    models/     detector weights and third-party model caches
    logs/       diagnostics
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent.parent


def _path(name, default):
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else Path(default).resolve()


DATA = _path('KIWIBIT_DATA_DIR', REPO / 'data')
LIBRARY = DATA / 'library'
DOWNLOADS = DATA / 'downloads'
CACHE = DATA / 'cache'
MODELS = DATA / 'models'
LOGS = DATA / 'logs'

# BioCLIP folder containing open_clip_config.json and open_clip_model.safetensors.
BIOCLIP = _path('KIWIBIT_BIOCLIP_DIR', MODELS / 'bioclip-2.5-vith14')
SPECIES = _path('KIWIBIT_SPECIES_FILE', PACKAGE / 'species.txt')
# Kiwibit account configuration (bearer token). Mounted read-only; never copied into data.
ACCOUNT_CONFIG = _path('KIWIBIT_ACCOUNT_CONFIG', REPO / 'secrets' / 'kiwibit-account.json')
