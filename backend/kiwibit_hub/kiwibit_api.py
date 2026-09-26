"""Minimal client for the Kiwibit (Addx/A4x) camera cloud API.

Reverse-engineered from a mitmproxy capture of the Kiwibit iOS app v2.24.0.
Every call is POST + JSON, wrapped in a common envelope, and answered with
{"result": 0, "msg": "...", "data": {...}}. Responses are gzip encoded.
"""

import gzip
import json
import os
import secrets
import urllib.error
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

USER_AGENT = "AddxAi/2.24.0 (com.kb.kiwibit; build:3022; iOS 27.0.0) Alamofire/3.2.1"


class KiwibitError(RuntimeError):
    def __init__(self, result, msg, path):
        super().__init__("%s failed: result=%s msg=%s" % (path, result, msg))
        self.result = result
        self.msg = msg


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class KiwibitClient:
    def __init__(self, config=None):
        self.cfg = config or load_config()
        self.base = self.cfg["baseUrl"].rstrip("/")

    # ---- transport -------------------------------------------------------

    def _envelope(self, extra=None, app=None):
        body = {
            "app": app or self.cfg["app"],
            "requestId": secrets.token_hex(16),
            "countryNo": "US",
            "language": "en",
            "userDeviceId": self.cfg["userDeviceId"],
            "userDeviceName": self.cfg["userDeviceName"],
        }
        if extra:
            body.update(extra)
        return body

    def post(self, path, extra=None, app=None):
        payload = json.dumps(self._envelope(extra, app)).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Language": "en-US;q=1.0",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
                "Authorization": "Bearer " + self.cfg["token"],
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                raw = gzip.decompress(raw)
        doc = json.loads(raw.decode("utf-8"))
        if doc.get("result") != 0:
            raise KiwibitError(doc.get("result"), doc.get("msg"), path)
        return doc.get("data")

    # ---- endpoints -------------------------------------------------------

    def node(self):
        """Service discovery: API node, analytics tracker, support hosts."""
        return self.post("/management/queryNode/v1")

    def devices(self):
        """All cameras on the account."""
        return self.post("/device/listuserdevices", {"needLoginAlert": True})["list"]

    def device(self, serial):
        return self.post(
            "/device/selectsingledevice",
            {"serialNumber": serial, "supportBindFlow": 0, "list": []},
        )

    def user_config(self, serial):
        return self.post(
            "/device/getuserconfig",
            {"serialNumber": serial, "supportBindFlow": 0, "list": []},
        )

    def push_images(self):
        """Latest motion-triggered snapshot per device (presigned URLs)."""
        return self.post("/device/devicePushImage")

    def wake(self, serial):
        """Wake a sleeping battery camera. Actuates the physical device."""
        return self.post(
            "/device/wakeupDevice",
            {"serialNumber": serial, "supportBindFlow": 0, "list": []},
        )

    def clips(self, start_ts, end_ts, serials=None, limit=20, offset=0):
        """Recorded event clips. Each carries an HLS .m3u8 URL + thumbnail."""
        return self.post(
            "/library/newselectlibrary/newevent",
            {
                "serialNumber": serials or [],
                "startTimestamp": int(start_ts),
                "endTimestamp": int(end_ts),
                "from": offset,
                "to": offset + limit,
                "marked": 0,
                "missing": 0,
                "highFramerate": 0,
                "tags": [],
                "doorbellTags": [],
                "objectNames": [],
                "deviceCallEventTag": "",
                "serialNumberToActivityZone": {},
            },
        )

    def webrtc_ticket(self, serial, wake_device=False):
        """Live-view credentials: signaling server, TURN list, access token.

        Uses a different `app` block than every other endpoint (it carries
        channelId/countlyId/userId), mirroring the iOS app.
        """
        body = {
            "serialNumber": serial,
            "verify4GVIPStatus": True,
            "needWakeupDevice": bool(wake_device),
            "verifyDormancyStatus": True,
            "supportUnlimitedWebsocket": True,
            "countryNo": "US",
            "language": "en",
        }
        payload = dict(body)
        payload["app"] = self.cfg["webrtcApp"]
        req = urllib.request.Request(
            self.base + "/device/getWebrtcTicket",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
                "Authorization": "Bearer " + self.cfg["token"],
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                raw = gzip.decompress(raw)
        doc = json.loads(raw.decode("utf-8"))
        if doc.get("result") != 0:
            raise KiwibitError(doc.get("result"), doc.get("msg"), "/device/getWebrtcTicket")
        return doc["data"]
