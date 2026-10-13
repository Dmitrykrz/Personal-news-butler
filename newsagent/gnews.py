"""Resolve Google News redirect links (news.google.com/rss/articles/...) to the publisher URL."""
import json
import re
from urllib.parse import quote

import requests

from .sources import TIMEOUT, UA

# EU visitors are redirected to a cookie-consent page without this
CONSENT = {"SOCS": "CAI", "CONSENT": "YES+cb"}


def decode(url) -> str | None:
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return None
    article_id = m.group(1)
    try:
        page = requests.get(f"https://news.google.com/articles/{article_id}", headers={"User-Agent": UA},
                            cookies=CONSENT, timeout=TIMEOUT).text
        sig = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sig and ts):
            return None
        inner = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                 f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",{ts.group(1)},"{sig.group(1)}"]')
        payload = json.dumps([[["Fbv4je", inner, None, "generic"]]])
        r = requests.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data=f"f.req={quote(payload)}", cookies=CONSENT, timeout=TIMEOUT,
        )
        body = json.loads(r.text.split("\n\n", 1)[1])
        decoded = json.loads(body[0][2])[1]
        return decoded if decoded.startswith("http") else None
    except Exception:
        return None
