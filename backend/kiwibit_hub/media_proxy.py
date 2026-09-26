"""Small loopback HLS bridge using Python's Windows-trusted HTTPS stack.

Some Windows FFmpeg builds cannot validate the cloud segment host's TLS chain.
This bridge fetches playlists and segments with urllib (which uses the Windows
certificate store) and gives ffplay opaque local URLs. It never disables TLS
verification or writes media or signed URLs to disk.
"""

import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath


URI_ATTRIBUTE = re.compile(r'URI="([^"]+)"')


class MediaProxy:
    def __init__(self):
        self.secret = secrets.token_urlsafe(24)
        self.urls = {}
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parts = self.path.split("/")
                if len(parts) != 4 or parts[1] != "media" or parts[2] != proxy.secret:
                    self.send_error(404)
                    return
                with proxy.lock:
                    remote_url = proxy.urls.get(parts[3])
                if remote_url is None:
                    self.send_error(404)
                    return
                headers = {"User-Agent": "KiwibitDesktop/POC"}
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                try:
                    request = urllib.request.Request(remote_url, headers=headers)
                    with urllib.request.urlopen(request, timeout=30) as response:
                        if urllib.parse.urlparse(response.url).scheme != "https":
                            raise ValueError("Media redirect did not use HTTPS")
                        ctype = response.headers.get("Content-Type", "")
                        is_playlist = ".m3u8" in urllib.parse.urlparse(response.url).path or "mpegurl" in ctype
                        if is_playlist:
                            body = proxy._rewrite_playlist(response.read().decode("utf-8"), response.url)
                            encoded = body.encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                            self.send_header("Content-Length", str(len(encoded)))
                            self.end_headers()
                            self.wfile.write(encoded)
                        else:
                            self.send_response(response.status)
                            self.send_header("Content-Type", ctype or "application/octet-stream")
                            for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                                value = response.headers.get(name)
                                if value:
                                    self.send_header(name, value)
                            self.end_headers()
                            while chunk := response.read(65536):
                                self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # ffplay closed the window while a segment was downloading
                except (urllib.error.URLError, ValueError, UnicodeError, OSError):
                    try:
                        self.send_error(502, "Cloud media request failed")
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url_for(self, remote_url):
        parsed = urllib.parse.urlparse(remote_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Cloud media URL must use HTTPS")
        # FFmpeg's HLS demuxer checks segment filename extensions.
        suffix = PurePosixPath(parsed.path).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ""
        media_id = secrets.token_urlsafe(18) + suffix
        with self.lock:
            self.urls[media_id] = remote_url
        return f"http://127.0.0.1:{self.server.server_port}/media/{self.secret}/{media_id}"

    def _rewrite_playlist(self, body, base_url):
        lines = []
        for line in body.splitlines():
            if line.startswith("#"):
                line = URI_ATTRIBUTE.sub(
                    lambda match: 'URI="' + self.url_for(urllib.parse.urljoin(base_url, match.group(1))) + '"',
                    line,
                )
            elif line.strip():
                line = self.url_for(urllib.parse.urljoin(base_url, line.strip()))
            lines.append(line)
        return "\n".join(lines) + "\n"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
