# Kiwibit Hub

A self-hosted replacement for the Kiwibit mobile app. It downloads the camera's
cloud recordings, identifies birds locally (BioCLIP 2.5 + YOLO-World on your
GPU), keeps a species photo library you can correct, and shows live view and
the camera's settings, all in a web UI that works on desktop and phone.

This is an unofficial client built from a reverse-engineered API.

| Folder | Contents |
|---|---|
| `frontend/` | Web UI (Svelte + TypeScript, built with Vite) |
| `backend/` | Python package `kiwibit_hub`: API server, jobs, scheduler, AI pipeline, catalog, cloud downloader |
| `live/` | Go WebRTC client; `cmd/relay` re-serves the camera's live stream to browsers |
| `data/` | Runtime data, git-ignored: `library/`, `downloads/`, `cache/`, `models/`, `logs/` |

## Pages

- **Home**: today's visitors, totals, latest recordings, the last 7 days, top
  visitors and busiest hours; fetch new recordings now.
- **Live**: live video in the browser (720p, 1080p or 4K), snapshots, and
  recording clips straight into the library.
- **Recordings**: every clip with frame-accurate bird boxes, the sightings in
  it with their evidence, the top matches for any bird, and corrections.
- **Birds**: species gallery of the best photos per sighting, sightings that
  need review, and corrections.
- **Camera**: the camera's status and its current settings (read-only for now,
  see below), automatic fetching, GPU batch sizes, sign-out.

## Run with Docker

Requirements: Docker with the NVIDIA container runtime (Docker Desktop on WSL2
works) and an NVIDIA driver supporting CUDA 12.8.

```powershell
Copy-Item .env.example .env   # then set KIWIBIT_HUB_PASSWORD and KIWIBIT_LIVE_HOSTS
docker compose up -d --build
```

Open <http://127.0.0.1:8765> and sign in with the `.env` credentials. The
interactive API is at `/docs`.

To use it from a phone on your network, set in `.env`:

```
KIWIBIT_HUB_BIND=0.0.0.0
KIWIBIT_LIVE_HOSTS=<this PC's LAN address>,127.0.0.1
```

Use a VPN such as Tailscale rather than exposing the hub to the internet.

`compose.yaml` mounts, relative to this folder:

- `./data` → `/data`: the library and downloads (read-write).
- `../bioclip-2.5-vith14` → `/models/bioclip`: the BioCLIP weights (read-only).
- `../kiwibit-analysis/kiwibit-viewer/config.json`: the account token, as a
  read-only Docker secret. It is never copied into the image or `data/`.

Override any of these in `.env`. The YOLO-World detector downloads into
`data/models` on first use if it is not already there.

### Recordings and identification

Every 30 minutes by default (Camera page), the hub lists the cloud library,
downloads new recordings as verified MP4s and identifies their birds. Fetching
never wakes the camera. A recording that fails processing is not retried
automatically; use Process now on it, or `POST /api/jobs {"action": "process"}`.

To bring an existing downloads folder into the hub without copying it
(hard links; the source is left untouched), stop the hub and run:

```powershell
cd backend
$env:KIWIBIT_DATA_DIR = "..\data"; .venv\Scripts\python.exe -m kiwibit_hub.importer <folder>
```

then start the hub and run a scan followed by process (`POST /api/jobs`).

### Live view

Starting live view wakes the camera, and the hub runs `kiwibit-relay`, which
joins the camera's WebRTC session and forwards its H.264 video to browsers
without re-encoding. Several browsers can watch one session. It ends about 30
seconds after the last viewer leaves, so the battery camera can sleep again.
Recorded clips (up to 10 minutes) start at the camera's next keyframe, are
saved as `downloads/live/<UTC time>_live.mp4` and are identified like any
recording.

Browser video uses UDP port 8766 (`KIWIBIT_LIVE_UDP_PORT`), published by
compose on all interfaces (`KIWIBIT_LIVE_BIND`). `KIWIBIT_LIVE_HOSTS` lists the
addresses browsers dial: put this PC's LAN address first, then `127.0.0.1`.
Firefox won't use a `127.0.0.1` route, and phones need the LAN address. If a
viewer can't connect, the hub log's `viewer offer candidates` line shows which
connection routes its browser offered. Live view is video only for now.

### Camera settings

The Camera page reads the camera's own description of its settings (about 45
of them: motion, video, night vision, sound, AI, power, device). Changing them
from the hub needs the phone app's write calls, which have not been captured
yet. To add that, capture the phone app with mitmproxy while changing one
setting at a time. Firmware updates, resets and removing the camera stay in
the Kiwibit app.

## API overview

| Endpoint | Purpose |
|---|---|
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | Sign in and out |
| `GET /api/status` | Counts and current job |
| `GET /api/recordings?q=&limit=` | Recordings, newest first, with species, duration and cover photo |
| `GET /api/recordings/{id}/video` | Original video (supports seeking; `?download=true`) |
| `GET /api/recordings/{id}/analysis` | Per-frame boxes and predictions, with sighting labels |
| `GET /api/recordings/{id}/sightings` | Sightings with evidence, correction and best photo |
| `GET /api/species`, `GET /api/species/{name}/photos` | Gallery (`?include_duplicates=true` for all photos) |
| `GET /api/photos/{path}` | A photo; `?size=thumb` for a cached 360 px thumbnail |
| `POST /api/sightings/{id}/correction` | `{"label": "..."}` to correct, `null` to restore AI |
| `GET /api/labels` | Candidate species names |
| `POST /api/jobs` | `{"action": "scan" \| "process" \| "selected" \| "reanalyze" \| "fetch" \| "verify" \| "upgrade", "ids": [...]}` |
| `POST /api/jobs/cancel` | Stop after the current operation |
| `WS /api/events` | Job state, messages, progress, library changes and live status |
| `GET/PUT /api/settings/processing` | GPU batch sizes |
| `GET/PUT /api/settings/fetch` | Automatic fetch on/off and interval; last and next run |
| `GET /api/camera`, `POST /api/camera/wake` | Camera summary; wake it |
| `GET /api/camera/settings` | The camera's current settings, grouped (read-only) |
| `GET /api/live`, `POST /api/live/start` | Live status; start at `{"resolution": "1280x720" \| "1920x1080" \| "3840x2160"}` |
| `POST /api/live/offer`, `POST /api/live/stop` | Browser SDP offer → answer; end the session |
| `POST /api/live/record`, `POST /api/live/record/stop` | Record a clip from live view |

Sign in on the web page (a 30-day HttpOnly session cookie; changing the
password signs every device out). Scripts can use HTTP Basic with the same
account. Everything under `/api` except health and sign-in requires one of the
two, and cross-site requests are refused. Five failed sign-ins lock sign-in
for five minutes.

## Develop without Docker

```powershell
cd backend
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The tests need no GPU or ML libraries (recording tests use ffmpeg when it is
on PATH). For the web UI and relay:

```powershell
cd frontend; npm install; npm run dev     # UI with hot reload, proxies /api to :8765
cd live; go test ./...; go build -o kiwibit-relay.exe ./cmd/relay
```

To run analysis natively, install PyTorch from
`https://download.pytorch.org/whl/cu128` and then `-e ".[ml]"`. Paths default
to `../data`; set `KIWIBIT_DATA_DIR`, `KIWIBIT_BIOCLIP_DIR` and
`KIWIBIT_ACCOUNT_CONFIG` to change them.

## Notes

- The server must run as a single process (`--workers 1`). It owns the GPU
  models, the job queue, the fetch schedule and the live relay.
- Species results are AI predictions. Match values are cosine similarities,
  not probabilities. Corrections relabel a sighting; they don't retrain the model.
- Ultralytics (YOLO-World) is AGPL-3.0. Keep that in mind before distributing
  or offering the hub to others.
