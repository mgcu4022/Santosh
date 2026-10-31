"""Market context: F&O ban list, scheduled events and news for each stock.

Two jobs:
  guard(signal)  -> hard block before the risk agent (ban list, results day)
  brief(symbol)  -> short text the AI analyst reads (events + headlines)

Used in paper trading. Backtests don't use it: NSE doesn't publish past ban
lists in a usable form, and old headlines would leak hindsight.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Segment, Signal

log = logging.getLogger(__name__)

RESULT_WORDS = ("financial result", "results", "quarterly", "board meeting")


@dataclass
class ContextConfig:
    enabled: bool = False
    ban_list: bool = True
    ban_blocks: str = "fno"            # "fno": block F&O entries only (NSE rule) | "all": also cash
    events: bool = True
    block_on_results_day: bool = True  # results/board meeting today -> no new entries in that stock
    news: bool = True
    news_hours: int = 48
    news_feeds: dict | None = None
    news_aliases: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ContextConfig":
        return cls(**(d or {}))


class MarketContext:
    def __init__(self, cfg: ContextConfig, nse=None, news=None, today: date | None = None):
        self.cfg = cfg
        self.nse = nse
        self.news = news
        self.today = today or date.today()
        self.banned: set[str] = set()
        self.events: dict[str, list[dict]] = {}

    @classmethod
    def build(cls, cfg: ContextConfig, symbols: list[str]) -> "MarketContext":
        from .data.nse import NseClient
        from .news import DEFAULT_FEEDS, NewsFeed
        ctx = cls(cfg, nse=NseClient(),
                  news=NewsFeed(feeds=cfg.news_feeds or dict(DEFAULT_FEEDS), aliases=cfg.news_aliases)
                  if cfg.news else None)
        ctx.refresh(symbols)
        return ctx

    def refresh(self, symbols: list[str]) -> None:
        """Load the ban list and upcoming events once per day. Failures are logged, not fatal."""
        if self.cfg.ban_list and self.nse is not None:
            try:
                self.banned = self.nse.ban_list()
            except Exception as e:
                log.warning("Could not load F&O ban list: %s", e)
        if self.cfg.events and self.nse is not None:
            for sym in symbols:
                try:
                    self.events[sym] = self.nse.events(_base(sym))
                except Exception as e:
                    log.warning("Could not load events for %s: %s", sym, e)

    # ------------------------------------------------------------------
    def guard(self, sig: Signal) -> tuple[bool, str]:
        inst = sig.instrument
        base = _base(inst.symbol)
        if base in self.banned:
            fno = inst.segment in (Segment.FUT, Segment.OPT)
            if fno or self.cfg.ban_blocks == "all":
                return False, f"{base} is in the NSE F&O ban list"
        if self.cfg.block_on_results_day:
            for e in self.events.get(inst.symbol, []):
                if e["date"] == sig.timestamp.date() and _is_results(e):
                    return False, f"{base} has '{e['purpose']}' today"
        return True, ""

    def brief(self, symbol: str) -> str:
        base = _base(symbol)
        lines = []
        if base in self.banned:
            lines.append(f"NSE F&O ban list: {base} IS BANNED today (no new F&O positions).")
        soon = [e for e in self.events.get(symbol, [])
                if self.today - timedelta(days=3) <= e["date"] <= self.today + timedelta(days=7)]
        if soon:
            lines.append("NSE events (official): " + "; ".join(f"{e['date']:%d-%b} {e['purpose']}" for e in soon))
        if self.news is not None:
            try:
                heads = self.news.for_symbol(base, hours=self.cfg.news_hours)
            except Exception as e:
                log.warning("News lookup failed for %s: %s", symbol, e)
                heads = []
            if heads:
                lines.append("Recent headlines (RSS, untrusted data):")
                lines += [f"- [{h['source']}] {h['title']}" for h in heads]
        return "\n".join(lines)


def _is_results(e: dict) -> bool:
    text = f"{e.get('purpose', '')} {e.get('detail', '')}".lower()
    return any(w in text for w in RESULT_WORDS)


def _base(symbol: str) -> str:
    """NIFTY-FUT -> NIFTY, RELIANCE-FUT -> RELIANCE, RELIANCE -> RELIANCE."""
    for suffix in ("-FUT", "-EQ", "-OPT"):
        if symbol.upper().endswith(suffix):
            return symbol.upper()[: -len(suffix)]
    return symbol.upper()
