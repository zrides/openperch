# Kiwibit Hub

A self-hosted replacement for the Kiwibit mobile app: cloud recordings, local
bird identification (BioCLIP 2.5 + YOLO-World), a species photo library, and
(planned) live view and camera settings, all behind a web UI.

This is an unofficial client built from a reverse-engineered API.

| Folder | Contents |
|---|---|
| `backend/` | Python package `kiwibit_hub`: API server, job runner, AI pipeline, catalog, cloud downloader |
| `live/` | Go WebRTC live-stream client (to be adapted to relay video to the browser) |
| `data/` | Runtime data, git-ignored: `library/`, `downloads/`, `cache/`, `models/`, `logs/` |

## Run with Docker

Requirements: Docker with the NVIDIA container runtime (Docker Desktop on WSL2
works) and an NVIDIA driver supporting CUDA 12.8.

```powershell
Copy-Item .env.example .env   # then set KIWIBIT_HUB_PASSWORD
docker compose up -d --build
```

Open <http://127.0.0.1:8765/docs> for the interactive API and sign in with the
`.env` credentials. To reach it from a phone, set `KIWIBIT_HUB_BIND=0.0.0.0`
(use a VPN such as Tailscale rather than exposing it to the internet).

`compose.yaml` mounts, relative to this folder:

- `./data` → `/data`: the library and downloads (read-write).
- `../bioclip-2.5-vith14` → `/models/bioclip`: the BioCLIP weights (read-only).
- `../kiwibit-analysis/kiwibit-viewer/config.json`: the account token, as a
  read-only Docker secret. It is never copied into the image or `data/`.

Override any of these in `.env`. The YOLO-World detector downloads into
`data/models` on first use if it is not already there.

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | Counts and current job |
| `GET /api/recordings?q=` | Catalogued recordings |
| `GET /api/recordings/{id}/video` | Original video (supports seeking) |
| `GET /api/recordings/{id}/analysis` | Per-frame boxes and predictions, with sighting labels |
| `GET /api/species`, `GET /api/species/{name}/photos` | Gallery |
| `POST /api/sightings/{id}/correction` | `{"label": "..."}` to correct, `null` to restore AI |
| `POST /api/jobs` | `{"action": "scan" \| "process" \| "selected" \| "reanalyze" \| "fetch" \| "verify" \| "upgrade", "ids": [...]}` |
| `POST /api/jobs/cancel` | Stop after the current operation |
| `WS /api/events` | Live job state, messages and progress |
| `GET/PUT /api/settings/processing` | GPU batch sizes |

Everything except `/api/health` requires HTTP Basic authentication.

## Develop without Docker

```powershell
cd backend
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The tests need no GPU or ML libraries. To run analysis natively, install
PyTorch from `https://download.pytorch.org/whl/cu128` and then `-e ".[ml]"`.
Paths default to `../data`; set `KIWIBIT_DATA_DIR`, `KIWIBIT_BIOCLIP_DIR` and
`KIWIBIT_ACCOUNT_CONFIG` to change them.

## Notes

- The server must run as a single process (`--workers 1`). It owns the GPU
  models and the one job queue.
- Species results are AI predictions. Match values are cosine similarities,
  not probabilities.
- Ultralytics (YOLO-World) is AGPL-3.0. Keep that in mind before distributing
  or offering the hub to others.
