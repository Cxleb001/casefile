"""Zetaris login tokens expire (about 15 minutes): long runs must sign in again instead of failing every query."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from casefile.config import get_settings
from casefile.data.zetaris import ZetarisBackend, ZetarisError

# The fake server is local: never route it through an HTTP proxy.
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _jwt(exp: float) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': int(exp)})}.sig"


class FakeZetaris:
    """Issues tokens with a chosen lifetime; rejects stale or revoked ones the way Zetaris did: 400 Bad Request."""

    def __init__(self, lifetime: float) -> None:
        self.lifetime, self.logins, self.revoked = lifetime, 0, set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/auth/login":
                    fake.logins += 1
                    return self._send(200, {"idToken": _jwt(time.time() + fake.lifetime) + str(fake.logins)})
                token = self.headers.get("Authorization", "")[7:]
                exp = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==")).get("exp")
                if token in fake.revoked or time.time() > exp:
                    return self._send(400, {"msg": "Bad Request"})
                return self._send(200, {"headers": [{"name": "one"}], "data": [["1"]]})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture()
def settings():
    return replace(get_settings(), zetaris_route="proxy", zetaris_org_id="1", zetaris_api_key="",
                   zetaris_username="admin@zetaris.com", zetaris_password="pw")


def test_a_token_near_its_expiry_is_renewed_before_the_query(settings):
    fake = FakeZetaris(lifetime=30)  # inside the 60 s safety margin: every call renews
    backend = ZetarisBackend(replace(settings, zetaris_base_url=fake.url))
    for _ in range(3):
        assert backend.query("SELECT 1 AS one", columns=["one"]).rows == [[1]]
    assert fake.logins == 3


def test_a_token_refused_mid_run_triggers_one_new_login_and_a_retry(settings):
    fake = FakeZetaris(lifetime=3600)
    backend = ZetarisBackend(replace(settings, zetaris_base_url=fake.url))
    backend.query("SELECT 1 AS one", columns=["one"])
    fake.revoked.add(backend._token)  # the server drops the session early
    assert backend.query("SELECT 1 AS one", columns=["one"]).rows == [[1]]
    assert fake.logins == 2


def test_a_genuine_bad_request_still_fails_after_one_retry(settings):
    fake = FakeZetaris(lifetime=-10)  # every token is already expired: nothing can succeed
    backend = ZetarisBackend(replace(settings, zetaris_base_url=fake.url))
    with pytest.raises(ZetarisError, match="HTTP 400"):
        backend.query("SELECT 1 AS one", columns=["one"])
    assert fake.logins == 2  # one login, one retry, then a clear error: no loop
