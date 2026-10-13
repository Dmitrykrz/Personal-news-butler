"""Download the readable text of an article (or a repo README)."""
import re

import requests
import trafilatura

from .sources import UA, TIMEOUT

MAX_CHARS = 6000


def github_readme(url):
    m = re.match(r"https://github\.com/([^/]+/[^/#?]+)/?$", url)
    if not m:
        return None
    r = requests.get(
        f"https://api.github.com/repos/{m.group(1)}/readme",
        headers={"Accept": "application/vnd.github.raw", "User-Agent": UA}, timeout=TIMEOUT,
    )
    return r.text if r.ok else None


def article_text(url) -> str:
    try:
        text = github_readme(url)
        if text is None:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            text = trafilatura.extract(r.text, url=url, include_comments=False) or ""
        return text[:MAX_CHARS]
    except Exception as e:
        return f"[could not fetch article text: {e.__class__.__name__}]"
