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
| `data/` | Runtime data, git-ignored: `library/`, `downloads/`, `sdcard/`, `cache/`, `models/`, `logs/` |

## Pages

- **Home**: today's visitors, totals, latest recordings, the last 7 days, top
  visitors and busiest hours; fetch new recordings now.
- **Live**: live video in the browser (720p, 1080p or 4K), snapshots, and
  recording clips straight into the library.
- **Recordings**: every clip with frame-accurate bird boxes, the sightings in
  it with their evidence, the top matches for any bird, and corrections. Camera
  dropouts (seconds where the camera's own recording has no frames and the
  picture freezes) are marked; a bird that sits through one stays one sighting.
- **Birds**: species gallery of the best photos per sighting, sightings that
  need review, and corrections.
- **Camera**: the camera's status and its current settings (read-only for now,
  see below), automatic fetching, GPU batch sizes, re-analyzing every recording
  after an AI change, adding SD-card footage, sign-out.

To add footage from the camera's SD card, move its clips (the card's own
`DCIM` folders are fine) into `data/sdcard`, press **Add card clips** on the
Camera page, then **Analyze waiting recordings** when you're ready for the GPU
run. Scheduled fetches only analyze cloud recordings, never card clips.
**Add card clips** also reads each card clip's length from its file header.
A cloud recording whose moment lies within card footage is then left out of
Recordings and the Home totals in favour of the card clip: a **Show** switch
lists the cloud copies again, and each one links to its card copy and can be
kept in the list. Nothing is deleted. Matching needs no video decoding: the
difference between the cloud's and the camera's clocks is measured from
clips that start close together (Kiwibit's cloud labels read 3 s early), and
with one camera only. Anything unclear keeps the cloud recording listed.
`GET /api/recordings/card-copies` reports the measured offset and the count.
- **Check the AI**: an answer key of about 150 moments you identify by eye (the
  AI's guess stays hidden), and a score comparing the AI with your answers after
  every change. The answers only measure the AI; they are never used to train it.
  A field guide (key G) shows reference photos from Wikipedia, including females
  and young birds where the article has them, next to the moments you have
  already identified; the browser loads these photos from Wikipedia.

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
`data/models` on first use if it is not already there. It is the extra-large
model (`yolov8x-worldv2.pt`, 146 MB); set `KIWIBIT_DETECTOR` to another weights
file under `data/models` to change it (a name starting with `yoloe` loads
YOLOE). Each analysis records its detector, and each detector has its own
confidence for starting a track. After changing it, re-analyze all recordings
from the Camera page.

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
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | Sign in and out; the session says the account's role |
| `GET/POST /api/users`, `PUT/DELETE /api/users/{name}` | Admins: list, add (`{"name", "password", "role": "member" \| "admin"}`), change role or password, remove |
| `PUT /api/account/password` | Change your own password (`{"current", "new"}`) |
| `GET /api/status` | Counts and current job |
| `GET /api/recordings?q=&limit=` | Recordings, newest first, with species, duration and cover photo |
| `GET /api/recordings/{id}/video` | Original video (supports seeking; `?download=true`) |
| `GET /api/recordings/{id}/analysis` | Per-frame boxes and predictions, with sighting labels |
| `GET /api/recordings/{id}/sightings` | Sightings with evidence, correction and best photo |
| `GET /api/species`, `GET /api/species/{name}/photos` | Gallery (`?include_duplicates=true` for all photos) |
| `GET /api/photos/{path}` | A photo; `?size=thumb` for a cached 360 px thumbnail |
| `POST /api/sightings/{id}/correction` | `{"label": "..."}` to correct, `null` to restore AI |
| `GET /api/labels` | Candidate species names |
| `GET /api/review`, `POST /api/review` | The answer key's moments; pick them (once) |
| `PUT /api/review/{id}` | `{"verdict": "species" \| "not_bird" \| "several" \| "unsure", "species": "..."}`; `null` verdict clears |
| `GET /api/review/{id}/image` | The moment's picture, cut from the original frame |
| `GET /api/review/score` | How the AI's current results compare with your answers |
| `POST /api/jobs` | `{"action": "scan" \| "process" \| "selected" \| "reanalyze" \| "reanalyze_all" \| "fetch" \| "verify" \| "upgrade", "ids": [...]}` |
| `POST /api/jobs/cancel` | Stop after the current operation |
| `WS /api/events` | Job state, messages, progress, library changes and live status |
| `GET/PUT /api/settings/processing` | GPU batch sizes |
| `GET/PUT /api/settings/fetch` | Automatic fetch on/off and interval; last and next run |
| `GET /api/camera`, `POST /api/camera/wake` | Camera summary; wake it |
| `GET /api/camera/settings` | The camera's current settings, grouped (read-only) |
| `GET /api/live`, `POST /api/live/start` | Live status; start at `{"resolution": "1280x720" \| "1920x1080" \| "3840x2160"}` |
| `POST /api/live/offer`, `POST /api/live/stop` | Browser SDP offer → answer; end the session |
| `POST /api/live/record`, `POST /api/live/record/stop` | Record a clip from live view |

Sign in on the web page (a 30-day HttpOnly session cookie). The account in
`.env` is the owner, always an admin. Admins add more accounts under **Hub
accounts** on the Camera page: members use everything else in the hub; admins
also manage accounts. Added accounts live in `data/users.json` as salted scrypt
hashes, never as passwords. Changing an account's password signs it out on
every device, and removing it signs it out at once; everyone but the owner can
change their own password there (the owner's stays in `.env`). Scripts can use
HTTP Basic with any account. Everything under `/api` except health and sign-in
requires one of the two, and cross-site requests are refused. Five failed
sign-ins from one address, on the page or through HTTP Basic, lock sign-in for
five minutes.

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

To measure a tracking or sighting change before deploying it, compare variants
against the saved analyses and the "Check the AI" answers. It reads a copy of
the catalog, runs no models and changes nothing (about 30 seconds for 800 clips).
While the hub runs in Docker, run it inside the container:

```powershell
docker compose exec hub python -m kiwibit_hub.evaluate --library /data/library
```

Don't open `data/library/library.sqlite3` from Windows while the container is
running, even read-only: the catalog uses SQLite's WAL mode, whose shared memory
doesn't work across the Docker boundary, and the hub's own reads start failing
with "disk I/O error".

## Notes

- The server must run as a single process (`--workers 1`). It owns the GPU
  models, the job queue, the fetch schedule and the live relay.
- The AI models hold about 4 GB of GPU memory while loaded. It is freed after
  5 minutes without a job (`KIWIBIT_GPU_IDLE_SECONDS`); the next job reloads
  them in about 20 seconds. The CUDA context (about 0.3 GB) stays until the
  hub stops.
- Species results are AI predictions. Match values are cosine similarities,
  not probabilities. Corrections relabel a sighting; they don't retrain the model.
- Ultralytics (YOLO-World) is AGPL-3.0. Keep that in mind before distributing
  or offering the hub to others.
