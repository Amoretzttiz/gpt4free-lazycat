#!/usr/bin/env python3
"""Safe control surface around g4f upstream Antigravity OAuth API."""
from __future__ import annotations
import asyncio, html, json, os, queue, secrets, stat, threading, time, webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CONTROL_HOST = os.environ.get("ANTIGRAVITY_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("ANTIGRAVITY_CONTROL_PORT", "8766"))
CONTROL_ORIGIN = os.environ.get("ANTIGRAVITY_CONTROL_ORIGIN", "").rstrip("/")
CONTROL_EXTERNAL_PATH = "/antigravity-oauth/"
CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH = "127.0.0.1", 51121, "/oauthcallback"
FLOW_TIMEOUT_SECONDS, MAX_REQUEST_BYTES = 300, 4096
CREDENTIALS_DIR = Path(os.environ.get("ANTIGRAVITY_CREDENTIALS_DIR", "/app/har_and_cookies"))
CREDENTIALS_NAME = "auth_Antigravity.json"

def credential_path():
    return CREDENTIALS_DIR / CREDENTIALS_NAME

def safe_credentials_present():
    """Read credentials only to validate; return no credential metadata."""
    try:
        info = credential_path().lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            return False
        with credential_path().open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return bool(value.get("access_token") and value.get("refresh_token"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False

def credentials_state():
    try:
        credential_path().lstat()
    except FileNotFoundError:
        return "not_authorized"
    except OSError:
        return "unsafe_credentials"
    return "authorized" if safe_credentials_present() else "unsafe_credentials"

def write_credentials(tokens, auth_manager_type):
    """Atomically install mode-0600 credentials in the persistent mount."""
    if any(not tokens.get(key) for key in ("access_token", "refresh_token", "expiry_date")):
        raise RuntimeError("incomplete OAuth result")
    CREDENTIALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CREDENTIALS_DIR.is_symlink() or not CREDENTIALS_DIR.is_dir():
        raise RuntimeError("credential directory is not a real directory")
    os.chmod(CREDENTIALS_DIR, 0o700)
    payload = {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
               "expiry_date": tokens["expiry_date"], "email": tokens.get("email"),
               "project_id": tokens.get("project_id"), "client_id": auth_manager_type.OAUTH_CLIENT_ID,
               "client_secret": auth_manager_type.OAUTH_CLIENT_SECRET}
    temp_path = CREDENTIALS_DIR / f".{CREDENTIALS_NAME}.{secrets.token_hex(12)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, separators=(",", ":")); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_path, credential_path()); os.chmod(credential_path(), 0o600)
        dir_fd = os.open(CREDENTIALS_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if descriptor is not None: os.close(descriptor)
        try: temp_path.unlink()
        except FileNotFoundError: pass

def clear_credentials():
    try: credential_path().unlink()
    except FileNotFoundError: pass

class FlowState:
    """Coordinate one flow without retaining returned tokens."""
    def __init__(self):
        self.lock, self.phase, self.worker, self.auth_url = threading.Lock(), credentials_state(), None, None
    def status(self):
        with self.lock:
            phase, running = self.phase, bool(self.worker and self.worker.is_alive())
        if not running:
            disk_state = credentials_state()
            if disk_state in ("authorized", "unsafe_credentials"): phase = disk_state
            elif phase == "authorized": phase = "not_authorized"
        return {"authorized": phase == "authorized", "state": phase, **({"authorization_url": self.auth_url} if phase == "waiting_for_google" and self.auth_url else {})}
    def start(self):
        with self.lock:
            if (self.worker and self.worker.is_alive()) or credentials_state() == "authorized": return False
            self.phase = "starting"; self.auth_url = None; self.worker = threading.Thread(target=self._run, daemon=True); self.worker.start(); return True
    def clear(self):
        with self.lock:
            if self.worker and self.worker.is_alive(): return False
            clear_credentials(); self.phase = "not_authorized"; self.auth_url = None; return True
    def _set_phase(self, value):
        with self.lock: self.phase = value
    def _run(self):
        callback_server = tokens = None
        try:
            from g4f.Provider.needs_auth.Antigravity import AntigravityAuthManager
            auth_url, _verifier, expected_state = AntigravityAuthManager.build_authorization_url()
            with self.lock: self.auth_url = auth_url
            callback_values = queue.Queue(maxsize=1)
            callback_server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), make_callback_handler(expected_state, callback_values), bind_and_activate=False)
            callback_server.allow_reuse_address = False; callback_server.server_bind(); callback_server.server_activate(); callback_server.timeout = 0.5
            self._set_phase("waiting_for_google")
            webbrowser.open(auth_url, new=2, autoraise=True)
            deadline = time.monotonic() + FLOW_TIMEOUT_SECONDS
            while callback_values.empty() and time.monotonic() < deadline: callback_server.handle_request()
            if callback_values.empty(): raise RuntimeError("callback timed out")
            result = callback_values.get_nowait()
            if result is None: raise RuntimeError("authorization denied")
            code, callback_state = result; self._set_phase("saving")
            tokens = asyncio.run(AntigravityAuthManager.exchange_code_for_tokens(code, callback_state))
            write_credentials(tokens, AntigravityAuthManager)
            with self.lock: self.auth_url = None
            self._set_phase("authorized")
        except Exception:
            with self.lock: self.auth_url = None
            self._set_phase("failed")  # Never log exception text; it may contain secrets.
        finally:
            if tokens is not None: tokens.clear()
            if callback_server is not None: callback_server.server_close()

def make_callback_handler(expected_state, result_queue):
    """Loopback callback accepting only this flow exact PKCE state."""
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args): pass
        def do_GET(self):
            if len(self.path) > MAX_REQUEST_BYTES: self._reply(HTTPStatus.REQUEST_URI_TOO_LONG, False); return
            parsed = urlsplit(self.path)
            if parsed.path != CALLBACK_PATH or not result_queue.empty(): self._reply(HTTPStatus.NOT_FOUND, False); return
            try:
                values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            except ValueError:
                self._reply(HTTPStatus.BAD_REQUEST, False); return
            states, codes, errors = values.get("state", []), values.get("code", []), values.get("error", [])
            trusted_state = len(states) == 1 and secrets.compare_digest(states[0], expected_state)
            if not trusted_state: self._reply(HTTPStatus.BAD_REQUEST, False); return
            if len(errors) == 1: result_queue.put_nowait(None); self._reply(HTTPStatus.BAD_REQUEST, False); return
            trusted = len(codes) == 1 and bool(codes[0])
            if not trusted: self._reply(HTTPStatus.BAD_REQUEST, False); return
            result_queue.put_nowait((codes[0], states[0])); self._reply(HTTPStatus.OK, True)
        def _reply(self, status, success):
            message = "Authorization received. Return to the control page." if success else "Authorization callback rejected."
            body = ("<!doctype html><meta charset=utf-8><title>Antigravity OAuth</title>" f"<p>{html.escape(message)}</p>").encode()
            self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'"); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    return CallbackHandler

FLOW, CSRF_TOKEN, CSRF_COOKIE = FlowState(), secrets.token_urlsafe(32), "g4f_antigravity_csrf"
def control_page(status):
    labels = {"authorized":"Authorized", "not_authorized":"Not authorized", "unsafe_credentials":"Credential file rejected (owner or mode is unsafe)",
              "starting":"Starting local browser flow", "waiting_for_google":"Waiting for Google in the noVNC browser", "saving":"Saving credentials", "failed":"Authorization failed or timed out"}
    label = labels.get(str(status["state"]), "Unavailable")
    auth_link = (f' <p><a href="{html.escape(status["authorization_url"], quote=True)}" target="_blank" rel="noreferrer">Open Google authorization</a> in the noVNC browser.</p>' if status.get("authorization_url") else "")
    disabled_start = " disabled" if status["authorized"] else ""
    disabled_clear = " disabled" if status["state"] == "not_authorized" else ""
    return f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Antigravity OAuth</title>
<h1>Google Antigravity OAuth</h1><p>Status: <strong>{html.escape(label)}</strong></p><p>Start authorization, then open the shown Google link in the application browser (noVNC).</p>{auth_link}
<form method="post" action="start"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button{disabled_start}>Start authorization</button></form>
<form method="post" action="clear"><input type="hidden" name="csrf" value="{CSRF_TOKEN}"><button{disabled_clear}>Clear local authorization</button></form>
<p>Clearing removes only the local credential file; it does not revoke Google access.</p>'''.encode()

class ControlHandler(BaseHTTPRequestHandler):
    server_version, sys_version = "AntigravityOAuthControl", ""
    def log_message(self, _format, *_args): pass
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/": self._send_html(HTTPStatus.OK, control_page(FLOW.status()), True)
        elif path == "/status": self._send_json(HTTPStatus.OK, FLOW.status())
        else: self._send_empty(HTTPStatus.NOT_FOUND)
    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/start", "/clear"): self._send_empty(HTTPStatus.NOT_FOUND); return
        if not self._valid_origin() or not self._valid_csrf(): self._send_empty(HTTPStatus.FORBIDDEN); return
        accepted = FLOW.start() if path == "/start" else FLOW.clear()
        if not accepted: self._send_json(HTTPStatus.CONFLICT, FLOW.status()); return
        self.send_response(HTTPStatus.SEE_OTHER); self._security_headers(); self.send_header("Location", CONTROL_EXTERNAL_PATH); self.send_header("Content-Length", "0"); self.end_headers()
    def _valid_origin(self):
        return bool(CONTROL_ORIGIN) and secrets.compare_digest(self.headers.get("Origin", ""), CONTROL_ORIGIN)
    def _valid_csrf(self):
        try: length = int(self.headers.get("Content-Length", "-1"))
        except ValueError: return False
        if length < 1 or length > MAX_REQUEST_BYTES or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded": return False
        cookies = {}
        for item in self.headers.get("Cookie", "").split(";"):
            key, separator, value = item.strip().partition("=")
            if separator: cookies[key] = value
        try: form = parse_qs(self.rfile.read(length).decode("ascii", "strict"), keep_blank_values=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError): return False
        values = form.get("csrf", [])
        return len(values) == 1 and secrets.compare_digest(values[0], CSRF_TOKEN) and secrets.compare_digest(cookies.get(CSRF_COOKIE, ""), CSRF_TOKEN)
    def _security_headers(self):
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Security-Policy", "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer"); self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "DENY")
    def _send_html(self, status, body, set_cookie=False):
        self.send_response(status); self._security_headers()
        if set_cookie: self.send_header("Set-Cookie", f"{CSRF_COOKIE}={CSRF_TOKEN}; Path={CONTROL_EXTERNAL_PATH}; Secure; HttpOnly; SameSite=Strict")
        self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _send_json(self, status, value):
        body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(); self.send_response(status); self._security_headers(); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _send_empty(self, status):
        self.send_response(status); self._security_headers(); self.send_header("Content-Length", "0"); self.end_headers()

def main():
    if not CONTROL_ORIGIN.startswith("https://"): raise SystemExit("ANTIGRAVITY_CONTROL_ORIGIN must be an explicit HTTPS origin")
    server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler); server.daemon_threads = True; server.serve_forever()
if __name__ == "__main__": main()
