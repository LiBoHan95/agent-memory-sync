"""Syncthing 摘要模块测试：用本地 HTTP 服务器模拟 Syncthing REST。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from amem import syncthing

SELF_ID = "SELFDEVICEID00000000000000000000000000000000000000000000000000000"
PEER_ID = "PEERDEVICEID00000000000000000000000000000000000000000000000000000"
PAUSED_ID = "PAUSEDDEVICE000000000000000000000000000000000000000000000000000000"

ROUTES = {
    "/rest/system/status": {"myID": SELF_ID},
    "/rest/config/devices": [
        {"deviceID": SELF_ID, "name": "server"},
        {"deviceID": PEER_ID, "name": "laptop"},
        {"deviceID": PAUSED_ID, "name": "windows", "paused": True},
    ],
    "/rest/system/connections": {"connections": {
        SELF_ID: {"connected": True},
        PEER_ID: {"connected": True, "address": "192.0.2.1:22000"},
    }},
    "/rest/stats/device": {PEER_ID: {"lastSeen": "2026-11-18T00:00:00Z"}},
}


class Handler(BaseHTTPRequestHandler):
    api_key = "test-key"
    requests: list[str] = []

    def do_GET(self):
        type(self).requests.append(self.path)
        if self.headers.get("X-API-Key") != type(self).api_key:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = ROUTES.get(self.path)
        if payload is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


class SyncthingTest(unittest.TestCase):
    def setUp(self):
        syncthing.clear_cache()
        Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        syncthing.clear_cache()

    def test_summary(self):
        result = syncthing.summarize(url=self.url, api_key="test-key", ttl=0)
        self.assertTrue(result["ok"])
        self.assertEqual((result["online"], result["total"]), (1, 2))
        self.assertEqual(result["my_id"], SELF_ID[:7])
        by_name = {d["name"]: d for d in result["devices"]}
        self.assertTrue(by_name["laptop"]["connected"])
        self.assertFalse(by_name["windows"]["connected"])
        self.assertTrue(by_name["windows"]["paused"])
        self.assertEqual(by_name["laptop"]["last_seen"], "2026-11-18T00:00:00Z")
        self.assertTrue(by_name["server"]["self"])
        self.assertNotIn("api_key", json.dumps(result))

    def test_missing_key_is_unconfigured(self):
        result = syncthing.summarize(url=self.url, api_key="")
        self.assertFalse(result["configured"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["devices"], [])

    def test_wrong_key_is_unreachable(self):
        result = syncthing.summarize(url=self.url, api_key="wrong", ttl=0)
        self.assertTrue(result["configured"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["online"], 0)

    def test_invalid_url_scheme(self):
        result = syncthing.summarize(url="file:///etc/passwd", api_key="x")
        self.assertFalse(result["ok"])
        self.assertIn("URL", result["error"])

    def test_cache(self):
        first = syncthing.summarize(url=self.url, api_key="test-key", ttl=60)
        count = len(Handler.requests)
        second = syncthing.summarize(url=self.url, api_key="test-key", ttl=60)
        self.assertEqual(first, second)
        self.assertEqual(len(Handler.requests), count)


if __name__ == "__main__":
    unittest.main()
