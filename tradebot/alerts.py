"""Alerts: console and Telegram.

Telegram setup (free):
  1. Message @BotFather on Telegram, send /newbot, copy the token.
  2. Send any message to your new bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}.
  3. export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...

Messages are sent from a background thread so a slow network never delays trading.
"""
from __future__ import annotations

import logging
import os
import queue
import threading

import requests

log = logging.getLogger(__name__)

DEFAULT_EVENTS = ("ENTRY_SIG", "CLOSED", "AI_VETO", "HALT")


class Notifier:
    def __init__(self, events=DEFAULT_EVENTS):
        self.events = set(events)

    def wants(self, event: str) -> bool:
        return event in self.events

    def send(self, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ConsoleNotifier(Notifier):
    def send(self, text: str) -> None:
        print(f"[ALERT] {text}", flush=True)


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None, events=DEFAULT_EVENTS,
                 timeout: float = 5.0, _post=None):
        super().__init__(events)
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        self.timeout = timeout
        self._post = _post or requests.post
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._t.start()

    def send(self, text: str) -> None:
        try:
            self._q.put_nowait(text[:4000])
        except queue.Full:
            log.warning("Alert queue full; dropping message")

    def _worker(self):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            text = self._q.get()
            if text is None:
                return
            try:
                self._post(url, data={"chat_id": self.chat_id, "text": text}, timeout=self.timeout)
            except Exception as e:   # never let alerts break trading
                log.warning("Telegram send failed: %s", e)
            finally:
                self._q.task_done()

    def close(self) -> None:
        self._q.join()
        self._q.put(None)


def format_event(event: str, symbol: str, details: dict) -> str:
    icon = {"ENTRY_SIG": "🟢", "CLOSED": "🔵", "AI_VETO": "🟠", "HALT": "🔴"}.get(event, "•")
    keys = ("side", "qty", "ref", "stop", "target", "exit", "net_pnl", "why")
    body = ", ".join(f"{k}={details[k]}" for k in keys if details.get(k) not in (None, ""))
    return f"{icon} [PAPER] {event} {symbol}: {body}"


def build_notifier(cfg: dict | None) -> Notifier | None:
    cfg = cfg or {}
    events = cfg.get("events", DEFAULT_EVENTS)
    if cfg.get("telegram"):
        return TelegramNotifier(events=events)
    if cfg.get("console"):
        return ConsoleNotifier(events=events)
    return None
