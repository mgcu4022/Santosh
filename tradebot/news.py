"""Free market news from RSS feeds, matched to stocks.

Headlines feed the AI analyst as context (treated as data, never as
instructions). Feeds can be changed in the config under `context.news_feeds`;
a feed that fails is skipped.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger(__name__)

DEFAULT_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
}

# Common short names. Add your own under context.news_aliases in the config.
DEFAULT_ALIASES = {
    "RELIANCE": ["Reliance Industries", "RIL", "Reliance Jio", "Reliance Retail"],
    "HDFCBANK": ["HDFC Bank"], "ICICIBANK": ["ICICI Bank"], "SBIN": ["SBI", "State Bank of India"],
    "INFY": ["Infosys"], "TCS": ["Tata Consultancy", "TCS"], "LT": ["Larsen", "L&T"],
    "KOTAKBANK": ["Kotak Mahindra Bank", "Kotak Bank"], "AXISBANK": ["Axis Bank"],
    "BHARTIARTL": ["Bharti Airtel", "Airtel"], "M&M": ["Mahindra & Mahindra", "M&M"],
    "MARUTI": ["Maruti Suzuki", "Maruti"], "HINDUNILVR": ["Hindustan Unilever", "HUL"],
    "BAJFINANCE": ["Bajaj Finance"], "SUNPHARMA": ["Sun Pharma"], "TATASTEEL": ["Tata Steel"],
    "ADANIENT": ["Adani Enterprises"], "ADANIPORTS": ["Adani Ports"], "WIPRO": ["Wipro"],
    "HCLTECH": ["HCL Tech", "HCLTech"], "TECHM": ["Tech Mahindra"], "ITC": ["ITC"],
    "NIFTY": ["Nifty", "Sensex", "RBI", "FII"], "BANKNIFTY": ["Bank Nifty", "RBI"],
}


def _text(el, *tags):
    for t in tags:
        x = el.find(t)
        if x is not None:
            return (x.text or x.get("href") or "").strip()
    return ""


def _parse_time(s: str):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)                 # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))   # Atom: ISO 8601
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str, source: str = "") -> list[dict]:
    """RSS 2.0 or Atom -> [{title, link, published, summary, source}]."""
    xml_text = re.sub(r"^[^<]+", "", xml_text)       # strip BOM/whitespace before the XML
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out = []
    for it in items:
        title = _text(it, "title", f"{atom}title")
        if not title:
            continue
        summary = re.sub(r"<[^>]+>", " ", _text(it, "description", f"{atom}summary"))
        out.append({"title": re.sub(r"\s+", " ", title), "link": _text(it, "link", f"{atom}link"),
                    "published": _parse_time(_text(it, "pubDate", f"{atom}updated", f"{atom}published")),
                    "summary": re.sub(r"\s+", " ", summary).strip()[:300], "source": source})
    return out


def match(headline: dict, symbol: str, aliases: list[str]) -> bool:
    text = f"{headline['title']} {headline.get('summary', '')}"
    for term in {symbol, *aliases}:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text,
                     flags=0 if term.isupper() else re.IGNORECASE):
            return True
    return False


@dataclass
class NewsFeed:
    feeds: dict = field(default_factory=lambda: dict(DEFAULT_FEEDS))
    aliases: dict = field(default_factory=dict)
    ttl_seconds: int = 600
    timeout: float = 10.0
    _cache: list = field(default_factory=list)
    _fetched_at: float = 0.0

    def all(self) -> list[dict]:
        if self._cache and time.time() - self._fetched_at < self.ttl_seconds:
            return self._cache
        items = []
        for name, url in self.feeds.items():
            try:
                r = requests.get(url, timeout=self.timeout, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                items += parse_feed(r.text, name)
            except Exception as e:   # one bad feed must not stop the others
                log.warning("News feed %s failed: %s", name, e)
        seen, uniq = set(), []
        for h in items:
            key = h["title"].lower()
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        self._cache, self._fetched_at = uniq, time.time()
        return uniq

    def for_symbol(self, symbol: str, hours: int = 48, limit: int = 8, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        al = list(self.aliases.get(symbol, [])) + DEFAULT_ALIASES.get(symbol, [])
        hits = [h for h in self.all()
                if match(h, symbol, al) and (h["published"] is None or now - h["published"] <= timedelta(hours=hours))]
        hits.sort(key=lambda h: h["published"] or now, reverse=True)
        return hits[:limit]
