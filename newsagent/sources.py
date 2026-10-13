"""Fetchers: turn a rule's source definition into a list of candidate articles."""
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape

import feedparser
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 20
MAX_PER_SOURCE = 30


@dataclass
class Candidate:
    rule_id: str
    source: str
    title: str
    url: str
    published: datetime | None = None
    snippet: str = ""
    extra: dict = field(default_factory=dict)
    needs_lookup: bool = False  # Google News redirect link: Claude must find the original article
    uid: str = ""
    text: str = ""
    origin_url: str = ""  # the Google News redirect, once `url` has been resolved


def _get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def strip_html(html, limit=500):
    text = unescape(re.sub(r"<[^>]+>", " ", html or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def fetch_rss(rule_id, src, lookback_days, gnews=False):
    feed = feedparser.parse(_get(src["url"]).content)
    cutoff = time.time() - lookback_days * 86400
    out = []
    for e in feed.entries:
        published = _entry_time(e)
        if published and published.timestamp() < cutoff:
            continue
        title = strip_html(e.get("title", ""), 300)
        snippet = strip_html(e.get("summary", ""))
        # many feeds carry the whole article: use it and skip downloading the page
        content = strip_html((e.get("content") or [{}])[0].get("value", ""), 6000)
        extra = {}
        if gnews:
            publisher = (e.get("source") or {}).get("title", "")
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(" - " + publisher)]
            extra["publisher"] = publisher
        out.append(Candidate(rule_id, src["name"], title, e.get("link", ""), published, snippet, extra,
                             needs_lookup=gnews, text=content if len(content) >= 800 else ""))
    out.sort(key=lambda c: c.published or datetime.now(timezone.utc), reverse=True)
    return out[: src.get("max_items", MAX_PER_SOURCE)]


def fetch_hn(rule_id, src, lookback_days):
    since = int(time.time() - lookback_days * 86400)
    min_points = src.get("min_points", 150)
    data = _get(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": "story", "numericFilters": f"created_at_i>{since},points>{min_points}", "hitsPerPage": 50},
    ).json()
    out = []
    for h in sorted(data["hits"], key=lambda h: h.get("points", 0), reverse=True):
        hn_link = f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(Candidate(
            rule_id, src["name"], h.get("title", ""), h.get("url") or hn_link,
            datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc), "",
            {"points": h.get("points"), "comments": h.get("num_comments"), "discussion": hn_link},
        ))
    return out[: src.get("max_items", MAX_PER_SOURCE)]


def fetch_github_trending(rule_id, src, lookback_days):
    since = src.get("since", "weekly")
    soup = BeautifulSoup(_get(f"https://github.com/trending?since={since}").text, "html.parser")
    out = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        if not link:
            continue
        repo = link["href"].strip("/")
        desc = row.select_one("p")
        lang = row.select_one('[itemprop="programmingLanguage"]')
        gained = row.select_one("span.float-sm-right")
        out.append(Candidate(
            rule_id, src["name"], repo, f"https://github.com/{repo}", None,
            desc.get_text(" ", strip=True) if desc else "",
            {"language": lang.get_text(strip=True) if lang else "",
             "stars_gained": gained.get_text(" ", strip=True) if gained else ""},
        ))
    return out


def fetch_source(rule_id, src, lookback_days):
    kind = src["type"]
    if kind == "rss":
        return fetch_rss(rule_id, src, lookback_days)
    if kind == "gnews":
        return fetch_rss(rule_id, src, lookback_days, gnews=True)
    if kind == "hn":
        return fetch_hn(rule_id, src, lookback_days)
    if kind == "github_trending":
        return fetch_github_trending(rule_id, src, lookback_days)
    raise ValueError(f"unknown source type {kind}")
