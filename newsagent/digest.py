"""One digest run: decide which rules are due, fetch, let Claude choose and write, send."""
import json
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import gnews
from .claude import extract_json, run_claude
from .config import APP_DIR, RUNS_DIR, TASTE_FILE, load_rules, log, now_local, timezone_problem
from .db import DB
from .extract import article_text
from .sources import fetch_source

SHORTLIST_THRESHOLD = 25  # above this many candidates, do a cheap title-only shortlist pass first
SHORTLIST_SIZE = 18
DECODE_PAUSE = 6          # seconds between Google News link decodes (Google rate-limits)
DECODE_RETRY_PAUSE = 90   # after a refusal, wait this long and try once more
KEEP_RUN_LOGS_DAYS = 30

SHORTLIST_PROMPT = """You are pre-selecting news for a personal digest. Below is the reader's taste
profile, the rules, and a list of candidate articles (titles and snippets only).

Pick up to {size} candidate ids worth reading in full, so that a final editor can choose up to
{cap} items from them. Include the best 1-2 candidates of every DUE rule that has anything
acceptable. Respect the rules' pick limits loosely (allow ~2x pick per rule for choice), favour
high-priority rules, drop anything the taste profile dislikes, and drop stories that duplicate
something already sent recently.

Answer with JSON only: {{"shortlist": ["c1", "c7", ...]}}

## Taste profile
{taste}

## Rules
{rules}

## Recently sent (do not repeat these stories)
{recent}

## Candidates
{candidates}
"""

FINAL_PROMPT = """You are the editor of a personal news digest delivered as Telegram messages.
Read the taste profile, the rules and the candidate articles (with their text), then decide
what this reader should get in this run.

Budget: {budget}

Editorial rules:
- Fewer good items beat more mediocre ones. Learning items (tips, tricks, how-tos):
  {learning_share} of the day's items.
- Rules marked DUE are expected in this run: each should normally contribute up to its `pick`.
  Skip one only if its candidates are genuinely poor for this reader.
- Rules marked OFF-SCHEDULE are not expected now: include them only if the story is genuinely
  important to this reader.
- `pick` is a soft per-rule limit. Respect priorities. Never send two items about the same story,
  and never repeat a story from the "recently sent" list.
- Only link free, non-paywalled articles. If a text looks cut off by a paywall (Le Monde,
  Le Figaro, etc. subscriber articles), use WebSearch to find free coverage of the same story
  and link that instead.
- Items marked NEEDS_LOOKUP have only a Google News redirect link and no text. For every DUE
  rule whose best candidates are NEEDS_LOOKUP, you MUST use WebSearch (title + publisher) to find
  the original or equivalent free coverage, read it, and link its real URL. "Not looked up" is
  not an acceptable reason to skip a due rule.
- If an article's text is missing you may WebFetch it.

For each item write, in {language}:
- title: a clear, factual headline (rewrite clickbait).
- summary: 2-3 sentences, max ~350 characters, based on the actual content. Say what happened /
  what the trick is, concretely. No hype, no "this article discusses".
- url: the link to open.
- kind: "news" or "learning".

Order items from most to least interesting. Answer with JSON only:
{{"items": [{{"candidate": "c3", "title": "...", "summary": "...", "url": "https://...", "kind": "news"}}],
  "notes": "one line: what you skipped and why"}}

## Taste profile
{taste}

## Rules
{rules}

## Recently sent (do not repeat these stories)
{recent}

## Candidates
{candidates}
"""

# passed to `claude --json-schema`: the CLI makes Claude's answer match these
SHORTLIST_SCHEMA = {
    "type": "object",
    "properties": {"shortlist": {"type": "array", "items": {"type": "string"}}},
    "required": ["shortlist"],
    "additionalProperties": False,
}
FINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "candidate": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "url": {"type": "string"},
                "kind": {"type": "string", "enum": ["news", "learning"]},
            },
            "required": ["candidate", "title", "summary", "url", "kind"],
            "additionalProperties": False,
        }},
        "notes": {"type": "string"},
    },
    "required": ["items", "notes"],
    "additionalProperties": False,
}


class RunFailed(RuntimeError):
    """A digest or deeper run failed. The message is short and safe to show in Telegram;
    the traceback is in the run folder's error.txt."""


def fail_run(run_dir, e):
    """Save the traceback of `e` in run_dir/error.txt and return a RunFailed pointing at it."""
    save_run(run_dir, "error.txt", traceback.format_exc())
    path = run_dir / "error.txt"
    where = path.relative_to(APP_DIR).as_posix() if path.is_relative_to(APP_DIR) else str(path)
    reason = (str(e).splitlines() or [""])[0][:150]
    log(f"  failed: {reason} (details in {where})")
    return RunFailed(f"{reason or type(e).__name__}. Details: {where}")


def runs_left_today(settings, now):
    """Scheduled runs later today, after the current one."""
    return sum(1 for at in settings["schedule"].values()
               if (int(at.split(":")[0]), int(at.split(":")[1])) > (now.hour, now.minute))


def describe_rules(rules, plans):
    lines = []
    for rule in rules:
        plan = plans.get(rule["id"])
        if not plan:
            continue
        status = "DUE" if plan["due"] else "OFF-SCHEDULE"
        lines.append(
            f"- {rule['id']} [{status}] kind={rule['kind']} priority={rule['priority']} "
            f"pick={rule['pick']}: {rule.get('description', '')}"
            + (f" Notes: {rule['notes']}" if rule.get("notes") else "")
        )
    return "\n".join(lines)


def describe_candidates(cands, full=False):
    blocks = []
    for c in cands:
        head = f"[{c.uid}] rule={c.rule_id} source={c.source}"
        if c.published:
            head += f" date={c.published:%Y-%m-%d}"
        for k, v in c.extra.items():
            if v:
                head += f" {k}={v}"
        if c.needs_lookup:
            head += " NEEDS_LOOKUP"
        body = f"{head}\ntitle: {c.title}\nurl: {c.url}"
        if full and c.text:
            body += f"\ntext:\n{c.text}"
        elif c.snippet:
            body += f"\nsnippet: {c.snippet}"
        blocks.append(body)
    return "\n\n".join(blocks)


def plan_rules(rules, db, today, run_name=None):
    """Decide which rules are due in this run and which of their sources to fetch.
    A rule with `run: evening` is only due in the evening run (manual runs ignore `run`)."""
    plans = {}
    for rule in rules:
        last_day, rotation = db.rule_state(rule["id"])
        due = last_day is None or today - last_day >= rule["every_days"]
        if run_name and rule["run"] and rule["run"] != run_name:
            due = False
        if not due and not rule["flexible"]:
            continue
        sources = rule["sources"]
        if rule["rotate"]:  # fetch in rotation order; apply_rotation keeps only the first with news
            start = rotation % len(sources)
            sources = sources[start:] + sources[:start]
        plans[rule["id"]] = {"rule": rule, "due": due, "sources": sources, "rotation": rotation}
    return plans


def apply_rotation(plans, cands):
    """For rotating rules keep only the next source (in turn) that has fresh articles.
    Sources with nothing new are skipped, so a quiet blog doesn't waste the day."""
    for p in plans.values():
        if not p["rule"]["rotate"]:
            continue
        p["advance"] = 1
        rid = p["rule"]["id"]
        for offset, src in enumerate(p["sources"]):
            if any(c.rule_id == rid and c.source == src["name"] for c in cands):
                p["advance"] = offset + 1
                cands = [c for c in cands if c.rule_id != rid or c.source == src["name"]]
                break
    return cands


def fetch_all(plans):
    jobs = [(p["rule"], s) for p in plans.values() for s in p["sources"]]
    results = []

    def one(job):
        rule, src = job
        try:
            return fetch_source(rule["id"], src, rule["lookback_days"])
        except Exception as e:
            log(f"  fetch failed: {rule['id']} / {src['name']}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for cands in pool.map(one, jobs):
            results.extend(cands)
    return results


def resolve_google_links(cands):
    """Turn Google News redirects into publisher URLs, slowly. Whatever stays unresolved
    keeps NEEDS_LOOKUP and Claude finds it with WebSearch."""
    retried = False
    for c in [c for c in cands if c.needs_lookup]:
        real = gnews.decode(c.url)
        if not real and not retried:
            retried = True
            log(f"  google news decode refused, waiting {DECODE_RETRY_PAUSE}s")
            time.sleep(DECODE_RETRY_PAUSE)
            real = gnews.decode(c.url)
        if not real:
            log("  google news still refusing, leaving remaining links to Claude")
            return
        c.origin_url, c.url, c.needs_lookup = c.url, real, False
        time.sleep(DECODE_PAUSE)


def prune_run_logs():
    cutoff = time.time() - KEEP_RUN_LOGS_DAYS * 86400
    for d in RUNS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def save_run(run_dir, name, content):
    (run_dir / name).write_text(content, encoding="utf-8")


def run_digest(send_item, run_name=None, dry_run=False):
    """Run one digest. `send_item(item) -> message_id` delivers one message.
    `run_name` is the schedule slot (e.g. "morning"); None for a manual run.
    Raises RunFailed (details in the run folder's error.txt) if anything goes wrong."""
    now = now_local()
    prune_run_logs()
    run_dir = RUNS_DIR / f"{now:%Y%m%d-%H%M%S}-{run_name or 'manual'}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _run_digest(send_item, run_name, dry_run, now, run_dir)
    except Exception as e:
        raise fail_run(run_dir, e) from e


def _run_digest(send_item, run_name, dry_run, now, run_dir):
    if timezone_problem():
        raise ValueError(timezone_problem())
    doc = load_rules()
    rules, settings = doc["rules"], doc["settings"]
    per_day = settings.get("items_per_day", {})
    day_min, day_max = per_day.get("min", 3), per_day.get("max", 8)
    taste = TASTE_FILE.read_text(encoding="utf-8") if TASTE_FILE.exists() else "(no taste profile yet)"

    today = now.date().toordinal()
    db = DB()

    sent_today = db.sent_on_day(today)
    later = runs_left_today(settings, now)
    cap = day_max - sent_today
    log(f"digest: {run_name or 'manual'} run {now:%Y-%m-%d %H:%M}, sent today {sent_today}, cap {cap}")
    if cap <= 0:
        log("digest: daily maximum already reached, nothing to do")
        return []
    budget = (f"the reader wants {day_min}-{day_max} items per day in total. {sent_today} already sent "
              f"today; {later} more scheduled run(s) later today. Send at most {cap} now"
              + (", leaving room for the later runs." if later
                 else ", and reach the daily minimum if the material allows."))

    plans = plan_rules(rules, db, today, run_name)
    log("  rules: " + ", ".join(f"{rid}{'' if p['due'] else '(flex)'}" for rid, p in plans.items()))

    seen = db.seen_urls()
    cands = apply_rotation(plans, [c for c in fetch_all(plans) if c.url and c.url not in seen])
    for i, c in enumerate(cands, 1):
        c.uid = f"c{i}"
    log(f"  candidates: {len(cands)}")

    recent = "\n".join(f"- [{r['rule_id']}] {r['title']}" for r in db.recent_sent()) or "(nothing yet)"
    fmt = dict(taste=taste, rules=describe_rules(rules, plans), recent=recent, cap=cap, budget=budget,
               size=SHORTLIST_SIZE, language=settings.get("language", "English"),
               learning_share=settings.get("learning_share", "about one third"))

    items = []
    if cands:
        if len(cands) > SHORTLIST_THRESHOLD:
            prompt = SHORTLIST_PROMPT.format(candidates=describe_candidates(cands), **fmt)
            save_run(run_dir, "1-shortlist-prompt.md", prompt)
            reply = run_claude(prompt, schema=SHORTLIST_SCHEMA)
            save_run(run_dir, "1-shortlist-reply.md", reply)
            keep = set(extract_json(reply, "shortlist")["shortlist"])
            cands = [c for c in cands if c.uid in keep]
            log(f"  shortlist: {len(cands)}")

        resolve_google_links(cands)
        with ThreadPoolExecutor(max_workers=6) as pool:
            todo = [c for c in cands if not c.text and not c.needs_lookup]
            for c, text in zip(todo, pool.map(lambda c: article_text(c.url), todo), strict=False):
                c.text = text

        prompt = FINAL_PROMPT.format(candidates=describe_candidates(cands, full=True), **fmt)
        save_run(run_dir, "2-final-prompt.md", prompt)
        reply = run_claude(prompt, tools=("WebSearch", "WebFetch"), timeout=1200, schema=FINAL_SCHEMA)
        save_run(run_dir, "2-final-reply.md", reply)
        result = extract_json(reply, "items")
        log(f"  editor: {len(result.get('items', []))} items. {result.get('notes', '')}")

        by_uid = {c.uid: c for c in cands}
        for it in result.get("items", [])[:cap]:
            cand = by_uid.get(it.get("candidate"))
            url = it.get("url") or (cand.url if cand else "")
            if not cand or not url.startswith("http") or not it.get("title"):
                log(f"  dropped malformed item: {json.dumps(it, ensure_ascii=False)[:200]}")
                continue
            items.append({"rule_id": cand.rule_id, "title": it["title"].strip(),
                          "summary": (it.get("summary") or "").strip(), "url": url,
                          "candidate_url": cand.origin_url or cand.url, "kind": it.get("kind", "news")})

    for item in items:
        message_id = send_item(item)
        if not dry_run:
            db.record_sent(item, today, message_id)

    if not dry_run:
        sent_rules = {i["rule_id"] for i in items}
        for rid, p in plans.items():
            if p["due"] or rid in sent_rules:  # an off-schedule item restarts that rule's clock
                rotation = p["rotation"] + p.get("advance", 1) if (p["rule"]["rotate"] and p["due"]) else p["rotation"]
                db.set_rule_state(rid, today, rotation)

    save_run(run_dir, "3-sent.json", json.dumps(items, ensure_ascii=False, indent=2))
    log(f"digest: done, {len(items)} items sent")
    return items
