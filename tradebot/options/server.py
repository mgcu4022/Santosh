"""Local web server for the option terminal: http://127.0.0.1:8050

Listens on 127.0.0.1 only (your PC), so nobody else on the network can place
paper orders. Market data refreshes in a background thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import OptionTerminal, TerminalError

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("terminal.html")


def make_handler(term: OptionTerminal):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode())

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(term.state())
            else:
                self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                routes = {
                    "/api/order": lambda b: term.place(b["side"], b["opt"], b["strike"], int(b["lots"])),
                    "/api/exit": lambda b: term.exit_fraction(b["symbol"], float(b["fraction"])),
                    "/api/add": lambda b: term.add_lots(b["symbol"], int(b["lots"])),
                    "/api/move": lambda b: term.move(b["symbol"], float(b["strike"])),
                    "/api/exit_all": lambda b: term.exit_all(),
                    "/api/rules": lambda b: term.set_rules(b.get("mtm_stop_loss"), b.get("mtm_target")),
                    "/api/view": lambda b: term.set_view(b.get("underlying"), b.get("expiry")),
                }
                if self.path not in routes:
                    return self._json({"ok": False, "error": "not found"}, 404)
                self._json(routes[self.path](body))
            except TerminalError as e:
                self._json({"ok": False, "error": str(e)}, 400)
            except (KeyError, ValueError, TypeError) as e:
                self._json({"ok": False, "error": f"bad request: {e}"}, 400)
            except Exception as e:   # keep the server alive
                log.exception("request failed")
                self._json({"ok": False, "error": str(e)}, 500)

        def log_message(self, *a):
            pass

    return Handler


def ticker(term: OptionTerminal, every: float, stop: threading.Event):
    while not stop.is_set():
        try:
            term.tick()
        except Exception as e:
            log.warning("market data refresh failed: %s", e)
            term._note("error", f"Data refresh failed: {str(e)[:120]}")
        stop.wait(every)


def serve(term: OptionTerminal, port: int = 8050, refresh_s: float = 2.0, open_browser: bool = True):
    term.tick()
    stop = threading.Event()
    threading.Thread(target=ticker, args=(term, refresh_s, stop), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(term))
    url = f"http://127.0.0.1:{port}"
    print(f"Option terminal ({term.state()['mode']} mode, PAPER orders only): {url}   Ctrl+C to stop")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        term.save()
        print(f"Saved to {term.run_dir}")
