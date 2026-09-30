# Kiwibit live stream client

OpenPerch runs `cmd/relay`, which uses this client to re-serve the camera's live
stream to browsers. `cmd/vicostream` is the original standalone client.

This is a local adaptation of [bbielsa/vicostream](https://github.com/bbielsa/vicostream) for the Kiwibit app and WebRTC cameras, including the Beako Smart Bird Feeder KF126A-P7G2. It obtains a fresh Kiwibit WebRTC ticket, joins the signaling session, and writes raw H.264 video to standard output. Audio is received but not written to the output stream.

## Play with a fresh token

Set `KIWIBIT_TOKEN` to the JWT from the Kiwibit app's `Authorization: Bearer ...` request header, and `KIWIBIT_SN` to the camera serial. Do not include the word `Bearer` in the token value. Then run the client through `ffplay`:

```powershell
$env:KIWIBIT_TOKEN = '<JWT>'
$env:KIWIBIT_SN = '<camera serial>'
$env:KIWIBIT_RESOLUTION = '3840x2160' # optional; default is 1280x720
cmd /c 'kiwibit-stream.exe | ffplay -f h264 -'
Remove-Item Env:KIWIBIT_TOKEN,Env:KIWIBIT_SN,Env:KIWIBIT_RESOLUTION
```

Use `cmd` for the binary video pipe on Windows. To record instead of play, replace `ffplay -f h264 -` with `ffmpeg -f h264 -i - -c copy recording.mp4`.

## Build from source

Requires Go. From this folder:

```powershell
go build -o kiwibit-stream.exe .\cmd\vicostream
go test ./...
```

## What was adapted

- Kiwibit API host and Android 2.24.0 app metadata in the wake and ticket requests.
- WebSocket `AUTH` version, `JOIN_LIVE` trace fields, and ordinary `SDP_OFFER` format from the APK.
- Local ICE candidate buffering until the SDP answer, plus candidate normalization used by the Android SDK.
- Credential-safe signaling logs. Account tokens, ticket access tokens, SDP, and ICE addresses are not printed.

Live checks received both 1280×720 and 3840×2160 H.264 streams. This is an unofficial client; camera sleep or a server change can interrupt playback. A first attempt may end before video starts; rerun it after the device wakes. Keep account tokens private.
