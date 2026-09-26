"""Stand-in for kiwibit-relay: same stdout events and HTTP control API, no camera.

FAKE_RELAY_MODE=fail exits before listening, like a rejected account token.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def emit(**event):
    print(json.dumps(event), flush=True)


if os.environ.get('FAKE_RELAY_MODE') == 'fail':
    emit(event='state', state='ended', detail='could not get a live ticket: API error (result=-1): token expired')
    sys.exit(1)

resolution = os.environ['KIWIBIT_RESOLUTION']
assert os.environ['KIWIBIT_TOKEN'] and os.environ['KIWIBIT_SN']
stop = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        # FAKE_RELAY_STREAM: an Annex-B H264 file, sent in small timed chunks like live video.
        if self.path != '/stream.h264' or not os.environ.get('FAKE_RELAY_STREAM'):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/h264')
        self.end_headers()
        data = open(os.environ['FAKE_RELAY_STREAM'], 'rb').read()
        try:
            for start in range(0, len(data), 2048):
                if stop.is_set():
                    break
                self.wfile.write(data[start:start + 2048])
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
        if self.path == '/whep':
            answer = f'v=0\r\ns=answer {resolution} for {len(body)} bytes\r\n'.encode()
            self.send_response(201)
            self.send_header('Content-Type', 'application/sdp')
            self.send_header('Content-Length', str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)
            emit(event='viewers', count=1)
        elif self.path == '/stop':
            self.send_response(202)
            self.end_headers()
            stop.set()


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
emit(event='listening', port=server.server_port)
emit(event='state', state='waking', detail='')
emit(event='state', state='streaming', detail='')
stop.wait(30)
emit(event='state', state='ended', detail='stopped')
server.shutdown()
