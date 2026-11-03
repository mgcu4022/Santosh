"""Upstox access for market data.

Recommended: an **Analytics Token** (Upstox developer portal -> My Apps -> Analytics ->
Generate Token). It lasts one year, is read-only (it cannot place orders) and covers
market data. Put it in .env as UPSTOX_ACCESS_TOKEN=... and no daily login is needed.

Alternative: daily Upstox login (OAuth). Those tokens expire every night (~3:30 AM IST).

Upstox requires you to log in yourself (PIN/OTP) once a day; that can't be
automated. This helper makes it a 30-second step:

  1. Opens the Upstox login page in your browser.
  2. You log in; Upstox redirects to a small local web page run by this helper.
  3. The helper swaps the one-time code for today's access token.

The token is kept in memory (or printed for you to export). It is never written
to a file. Optional: --keyring stores it in your operating system's password
store (Windows Credential Manager / macOS Keychain) if the `keyring` package is installed.

One-time setup in the Upstox developer console (https://account.upstox.com/developer/apps):
  - Create an app, set Redirect URL to  http://127.0.0.1:5055/callback
  - export UPSTOX_API_KEY=...  UPSTOX_API_SECRET=...
"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"
DEFAULT_REDIRECT = "http://127.0.0.1:5055/callback"
KEYRING_SERVICE = "tradebot-upstox"


class UpstoxAuthError(RuntimeError):
    pass


def login_url(api_key: str, redirect_uri: str, state: str = "tradebot") -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": api_key,
                                       "redirect_uri": redirect_uri, "state": state})


def exchange_code(code: str, api_key: str, api_secret: str, redirect_uri: str, post=requests.post) -> str:
    r = post(TOKEN_URL, data={"code": code, "client_id": api_key, "client_secret": api_secret,
                              "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
             headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
             timeout=20)
    body = r.json() if r.content else {}
    token = body.get("access_token")
    if r.status_code != 200 or not token:
        raise UpstoxAuthError(f"Token exchange failed ({r.status_code}): {str(body)[:300]}")
    return token


def check_token(token: str, get=requests.get) -> str:
    """Returns the account's user name if the token works."""
    r = get(PROFILE_URL, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=15)
    if r.status_code != 200:
        raise UpstoxAuthError(f"Token check failed ({r.status_code})")
    return r.json().get("data", {}).get("user_name", "OK")


def wait_for_code(redirect_uri: str, timeout_s: float) -> str | None:
    """Run a one-shot local web server on the redirect URL and return the ?code= value."""
    u = urlparse(redirect_uri)
    if u.hostname not in ("127.0.0.1", "localhost"):
        raise UpstoxAuthError("Redirect URL must be on 127.0.0.1/localhost for automatic capture")
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if "code" in q:
                result["code"] = q["code"][0]
                msg = "Upstox login done. You can close this tab; paper trading is starting."
            else:
                msg = "No login code received. Please try again."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer((u.hostname, u.port or 80), Handler)
    srv.timeout = 1.0
    stop = threading.Event()
    timer = threading.Timer(timeout_s, stop.set)
    timer.daemon = True
    timer.start()
    try:
        while not stop.is_set() and "code" not in result:
            srv.handle_request()
    finally:
        timer.cancel()
        srv.server_close()
    return result.get("code")


def interactive_login(timeout_min: float = 10, open_browser: bool = True, notify=None) -> str | None:
    """Full browser login. Returns the token, or None if nobody logged in before the timeout."""
    key, secret = os.environ.get("UPSTOX_API_KEY", ""), os.environ.get("UPSTOX_API_SECRET", "")
    if not key or not secret:
        raise UpstoxAuthError("Set UPSTOX_API_KEY and UPSTOX_API_SECRET (from the Upstox developer console)")
    redirect = os.environ.get("UPSTOX_REDIRECT_URI", DEFAULT_REDIRECT)
    url = login_url(key, redirect)
    print(f"\nLog in to Upstox (waiting {timeout_min:g} min):\n  {url}\n", flush=True)
    if notify:
        notify("[PAPER] Upstox login needed on the trading PC to start today's session.")
    if open_browser:
        webbrowser.open(url)
    code = wait_for_code(redirect, timeout_min * 60)
    if not code:
        log.warning("No Upstox login within %s min", timeout_min)
        return None
    token = exchange_code(code, key, secret, redirect)
    log.info("Upstox login OK (%s)", check_token(token))
    return token


def saved_token() -> str | None:
    """A token that needs no login: UPSTOX_ACCESS_TOKEN (e.g. the 1-year Analytics Token) or the OS keyring."""
    return os.environ.get("UPSTOX_ACCESS_TOKEN") or token_from_keyring()


def get_token(login: bool, timeout_min: float = 10, notify=None) -> str | None:
    """Use a saved token if there is one; otherwise do the browser login when asked."""
    tok = saved_token()
    if tok:
        log.info("Using saved Upstox token (no login needed)")
        return tok
    if login:
        return interactive_login(timeout_min, notify=notify)
    return None


def token_from_keyring() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, "access_token")
    except Exception:
        return None


def save_to_keyring(token: str) -> None:
    import keyring   # pip install keyring
    keyring.set_password(KEYRING_SERVICE, "access_token", token)
