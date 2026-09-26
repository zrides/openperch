# Kiwibit Hub

A self-hosted replacement for the Kiwibit mobile app: cloud recordings, local
bird identification (BioCLIP 2.5 + YOLO-World), a species photo library, live
view in the browser and (planned) camera settings, all behind a web UI.

This is an unofficial client built from a reverse-engineered API.

| Folder | Contents |
|---|---|
| `frontend/` | Web UI (Svelte + TypeScript, built with Vite) |
| `backend/` | Python package `kiwibit_hub`: API server, job runner, AI pipeline, catalog, cloud downloader |
| `live/` | Go WebRTC client; `cmd/relay` re-serves the camera's live stream to browsers |
| `data/` | Runtime data, git-ignored: `library/`, `downloads/`, `cache/`, `models/`, `logs/` |

## Run with Docker

Requirements: Docker with the NVIDIA container runtime (Docker Desktop on WSL2
works) and an NVIDIA driver supporting CUDA 12.8.

```powershell
Copy-Item .env.example .env   # then set KIWIBIT_HUB_PASSWORD
docker compose up -d --build
```

Open <http://127.0.0.1:8765> and sign in with the `.env` credentials. The
interactive API is at `/docs`.

To use it from a phone on your network, set in `.env`:

```
KIWIBIT_HUB_BIND=0.0.0.0
KIWIBIT_LIVE_HOSTS=127.0.0.1,<this PC's LAN address>
```

Use a VPN such as Tailscale rather than exposing the hub to the internet.

`compose.yaml` mounts, relative to this folder:

- `./data` → `/data`: the library and downloads (read-write).
- `../bioclip-2.5-vith14` → `/models/bioclip`: the BioCLIP weights (read-only).
- `../kiwibit-analysis/kiwibit-viewer/config.json`: the account token, as a
  read-only Docker secret. It is never copied into the image or `data/`.

Override any of these in `.env`. The YOLO-World detector downloads into
`data/models` on first use if it is not already there.

### Live view

Starting live view wakes the camera, and the hub runs `kiwibit-relay`, which
joins the camera's WebRTC session and forwards its H.264 video to browsers
without re-encoding. Several browsers can watch one session. It ends about 30
seconds after the last viewer leaves, so the battery camera can sleep again.

Browser video uses UDP port 8766 (`KIWIBIT_LIVE_UDP_PORT`), published by
compose on all interfaces (`KIWIBIT_LIVE_BIND`). `KIWIBIT_LIVE_HOSTS` lists the
addresses browsers dial: put this PC's LAN address first, then `127.0.0.1`.
Firefox won't use a `127.0.0.1` route, and phones need the LAN address. If a
viewer can't connect, the hub log's `viewer offer candidates` line shows which
connection routes its browser offered. Live view is video only for now.

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
| `GET /api/camera`, `POST /api/camera/wake` | Camera summary (battery, firmware, awake); wake it |
| `GET /api/live`, `POST /api/live/start` | Live session status; start at `{"resolution": "1280x720" \| "1920x1080" \| "3840x2160"}` |
| `POST /api/live/offer`, `POST /api/live/stop` | Browser SDP offer → answer; end the session |

Everything except `/api/health` requires HTTP Basic authentication.

## Develop without Docker

```powershell
cd backend
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The tests need no GPU or ML libraries. For the web UI and relay:

```powershell
cd frontend; npm install; npm run dev     # UI with hot reload, proxies /api to :8765
cd live; go test ./...; go build -o kiwibit-relay.exe ./cmd/relay
```

To run analysis natively, install
PyTorch from `https://download.pytorch.org/whl/cu128` and then `-e ".[ml]"`.
Paths default to `../data`; set `KIWIBIT_DATA_DIR`, `KIWIBIT_BIOCLIP_DIR` and
`KIWIBIT_ACCOUNT_CONFIG` to change them.

## Notes

- The server must run as a single process (`--workers 1`). It owns the GPU
  models, the one job queue and the live relay.
- Species results are AI predictions. Match values are cosine similarities,
  not probabilities.
- Ultralytics (YOLO-World) is AGPL-3.0. Keep that in mind before distributing
  or offering the hub to others.
