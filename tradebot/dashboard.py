"""Read-only web dashboard for the strategy bot: http://127.0.0.1:8060

Shows every paper bot running today (e.g. runs/paper_YYYYMMDD and
runs/trial20k/paper_YYYYMMDD): watchlist, positions, MTM, limits and the journal.
It only reads the state.json files the bot writes every few seconds; it cannot
place, change or close anything. Listens on 127.0.0.1 only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PAGE = Path(__file__).with_name("dashboard.html")
log = logging.getLogger(__name__)


def list_runs(runs_dir: str, day: str | None = None) -> list[dict]:
    """Today's bot runs (newest first), each with a short summary."""
    day = day or datetime.now(IST).strftime("%Y%m%d")
    root = Path(runs_dir)
    out = []
    for d in root.glob(f"**/paper_{day}*"):
        f = d / "state.json"
        if not f.is_file():
            continue
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rel = d.relative_to(root).as_posix()
        label = rel.replace(f"paper_{day}", "today").replace("/", " / ")
        out.append({"id": rel, "label": label, "updated": s.get("updated"), "mtm": s.get("mtm"),
                    "capital": s.get("capital"), "mtime": f.stat().st_mtime})
    return sorted(out, key=lambda r: -r["mtime"])


def read_state(runs_dir: str, run_id: str) -> dict | None:
    root = Path(runs_dir).resolve()
    target = (root / run_id / "state.json").resolve()
    if root not in target.parents or not target.is_file():   # no path tricks outside runs/
        return None
    s = json.loads(target.read_text(encoding="utf-8"))
    try:
        age = (datetime.now(IST) - datetime.fromisoformat(s["updated"])).total_seconds()
        s["age_seconds"] = round(age)
    except (KeyError, ValueError):
        s["age_seconds"] = None
    return s


def make_handler(runs_dir: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/runs":
                return self._send(200, json.dumps(list_runs(runs_dir), default=str).encode())
            if u.path == "/api/state":
                run = (parse_qs(u.query).get("run") or [""])[0]
                s = read_state(runs_dir, run) if run else None
                if s is None:
                    return self._send(404, b'{"error": "run not found"}')
                return self._send(200, json.dumps(s, default=str).encode())
            self._send(404, b'{"error": "not found"}')

        def log_message(self, *a):
            pass

    return Handler


def serve(runs_dir: str = "runs", port: int = 8060, open_browser: bool = True) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(runs_dir))
    url = f"http://127.0.0.1:{port}"
    print(f"Bot dashboard (read-only): {url}   Ctrl+C to stop")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
