import importlib.util
import json
import os
import queue
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
os.environ["ANTIGRAVITY_CREDENTIALS_DIR"] = TMP.name
os.environ["ANTIGRAVITY_CONTROL_ORIGIN"] = "https://gpt4free.example.test"
spec = importlib.util.spec_from_file_location("oauth_control", ROOT / "docker/antigravity-oauth-control.py")
oauth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oauth)

ACCESS_FIXTURE = "access-fixture-must-not-leak"
REFRESH_FIXTURE = "refresh-fixture-must-not-leak"

class DummyAuth:
    OAUTH_CLIENT_ID = "client-id-fixture"
    OAUTH_CLIENT_SECRET = "client-secret-fixture"

class FakeFlow:
    def __init__(self):
        self.started = self.cleared = 0
    def status(self):
        return {"authorized": False, "state": "not_authorized"}
    def start(self):
        self.started += 1
        return True
    def clear(self):
        self.cleared += 1
        return True

class ServerMixin:
    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

class CredentialTests(unittest.TestCase):
    def setUp(self):
        for item in Path(TMP.name).iterdir():
            item.unlink()

    def test_atomic_credentials_are_private_and_status_is_boolean_only(self):
        oauth.write_credentials({"access_token": ACCESS_FIXTURE, "refresh_token": REFRESH_FIXTURE, "expiry_date": 123}, DummyAuth)
        path = oauth.credential_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertTrue(oauth.safe_credentials_present())
        serialized = json.dumps(oauth.FlowState().status())
        self.assertNotIn(ACCESS_FIXTURE, serialized)
        self.assertNotIn(REFRESH_FIXTURE, serialized)

    def test_unsafe_mode_is_rejected(self):
        oauth.write_credentials({"access_token": ACCESS_FIXTURE, "refresh_token": REFRESH_FIXTURE, "expiry_date": 123}, DummyAuth)
        oauth.credential_path().chmod(0o644)
        self.assertFalse(oauth.safe_credentials_present())
        self.assertEqual(oauth.credentials_state(), "unsafe_credentials")

class CallbackTests(unittest.TestCase):
    def request(self, base, path):
        try:
            with urllib.request.urlopen(base + path) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_callback_rejects_untrusted_state_without_reflection(self):
        results = queue.Queue(maxsize=1)
        server = HTTPServer(("127.0.0.1", 0), oauth.make_callback_handler("expected-state", results))
        thread = threading.Thread(target=server.handle_request, daemon=True); thread.start()
        status, body = self.request(f"http://127.0.0.1:{server.server_port}", "/oauthcallback?code=private-code&state=attacker-state")
        thread.join(2); server.server_close()
        self.assertEqual(status, 400)
        self.assertTrue(results.empty())
        self.assertNotIn(b"private-code", body)
        self.assertNotIn(b"attacker-state", body)

    def test_callback_accepts_exact_state_once_without_reflection(self):
        results = queue.Queue(maxsize=1)
        server = HTTPServer(("127.0.0.1", 0), oauth.make_callback_handler("expected-state", results))
        thread = threading.Thread(target=server.handle_request, daemon=True); thread.start()
        status, body = self.request(f"http://127.0.0.1:{server.server_port}", "/oauthcallback?code=private-code&state=expected-state")
        thread.join(2); server.server_close()
        self.assertEqual(status, 200)
        self.assertEqual(results.get_nowait(), ("private-code", "expected-state"))
        self.assertNotIn(b"private-code", body)
        self.assertNotIn(b"expected-state", body)

class ControlHttpTests(unittest.TestCase, ServerMixin):
    def setUp(self):
        self.flow = FakeFlow()
        self.flow_patch = mock.patch.object(oauth, "FLOW", self.flow)
        self.flow_patch.start(); self.addCleanup(self.flow_patch.stop)
        self.base = self.start_server(oauth.ControlHandler)

    def open(self, request):
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def test_status_and_page_never_expose_credentials(self):
        for path in ("/", "/status"):
            status, _headers, body = self.open(self.base + path)
            self.assertEqual(status, 200)
            self.assertNotIn(ACCESS_FIXTURE.encode(), body)
            self.assertNotIn(REFRESH_FIXTURE.encode(), body)

    def test_post_requires_exact_origin_cookie_and_csrf(self):
        status, headers, page = self.open(self.base + "/")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        body = urllib.parse.urlencode({"csrf": oauth.CSRF_TOKEN}).encode()
        missing_origin = urllib.request.Request(self.base + "/start", data=body, headers={"Content-Type":"application/x-www-form-urlencoded", "Cookie":cookie})
        self.assertEqual(self.open(missing_origin)[0], 403)
        wrong_csrf = urllib.request.Request(self.base + "/start", data=b"csrf=wrong", headers={"Content-Type":"application/x-www-form-urlencoded", "Cookie":cookie, "Origin":oauth.CONTROL_ORIGIN})
        self.assertEqual(self.open(wrong_csrf)[0], 403)
        valid = urllib.request.Request(self.base + "/start", data=body, headers={"Content-Type":"application/x-www-form-urlencoded", "Cookie":cookie, "Origin":oauth.CONTROL_ORIGIN})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as redirect:
            opener.open(valid)
        self.assertEqual(redirect.exception.code, 303)
        self.assertEqual(self.flow.started, 1)

class ManifestTests(unittest.TestCase):
    def test_routes_stay_authenticated_and_callback_is_not_proxied(self):
        for name in ("manifest.yml", "lzc-manifest.yml"):
            text = (ROOT / name).read_text()
            self.assertNotIn("public_path", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")))
            self.assertIn("location: /antigravity-oauth/", text)
            self.assertNotIn("51121", text)
        self.assertEqual(oauth.CALLBACK_HOST, "127.0.0.1")
        self.assertEqual(oauth.CALLBACK_PATH, "/oauthcallback")

if __name__ == "__main__":
    unittest.main()
