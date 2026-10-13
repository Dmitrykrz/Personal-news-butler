"""Paths, environment settings, and loading/validating rules.yaml."""
import logging
import os
import re
import subprocess
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# The whole project folder is mounted at APP_DIR and is one git repo for the code.
# config/rules.yaml and config/taste.md are personal and gitignored.
# Claude (via Telegram) may edit both; /undo reverts the last change (see feedback.py).
APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_DIR / "config"
RULES_FILE = CONFIG_DIR / "rules.yaml"
TASTE_FILE = CONFIG_DIR / "taste.md"
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
DB_FILE = DATA_DIR / "newsbot.db"
RUNS_DIR = DATA_DIR / "runs"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
TIMEZONE = os.environ.get("TZ", "")  # schedule times in rules.yaml are in this timezone

SOURCE_TYPES = {"rss", "hn", "github_trending", "gnews"}
KINDS = {"news", "learning", "mixed"}
PRIORITIES = {"high", "normal", "low"}


_logger = None


def log(*args):
    """Print to stdout (docker logs) and append to data/bot.log (readable by the Telegram agent)."""
    global _logger
    msg = " ".join(str(a) for a in args)
    print(msg, flush=True)
    if _logger is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _logger = logging.getLogger("newsbot")
        handler = RotatingFileHandler(DATA_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
    _logger.info(msg)


def git(*args, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=newsbot", "-c", "user.email=newsbot@localhost", *args],
        cwd=APP_DIR, capture_output=True, text=True, check=check,
    )


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    if not (APP_DIR / ".git").exists():
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "initial version")
        log("git: initialised repository")


def timezone_problem():
    """Return a readable error if TZ is missing or unknown, else None."""
    if not TIMEZONE:
        return "TZ is not set in .env. Add your timezone (e.g. TZ=Europe/Berlin) and restart the container."
    try:
        ZoneInfo(TIMEZONE)
    except Exception:
        return f"TZ={TIMEZONE} in .env is not a known timezone. Use a name like Europe/Berlin or America/New_York."
    return None


def now_local():
    return datetime.now(ZoneInfo(TIMEZONE))


class ConfigMissing(ValueError):
    """rules.yaml doesn't exist yet: the owner hasn't described what they want."""


def load_rules(path: Path = RULES_FILE) -> dict:
    """Parse and validate rules.yaml. Raises ValueError with a readable message."""
    if not path.exists():
        raise ConfigMissing("config/rules.yaml doesn't exist yet")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"rules.yaml is not valid YAML: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise ValueError("rules.yaml must have a top-level 'rules' list")
    settings = doc.setdefault("settings", {})
    schedule = settings.setdefault("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("settings.schedule must map a run name to a time, e.g. morning: \"09:00\"")
    for name, at in schedule.items():
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(at)):
            raise ValueError(f"settings.schedule.{name}: time must look like \"09:00\"")
    seen = set()
    for r in doc["rules"]:
        rid = r.get("id") if isinstance(r, dict) else None
        if not rid or not isinstance(rid, str):
            raise ValueError(f"every rule needs a string 'id': {r!r}")
        if rid in seen:
            raise ValueError(f"duplicate rule id '{rid}'")
        seen.add(rid)
        r.setdefault("kind", "news")
        r.setdefault("priority", "normal")
        r.setdefault("every_days", 1)
        r.setdefault("pick", 1)
        r.setdefault("lookback_days", max(2, r["every_days"]))
        r.setdefault("flexible", False)
        r.setdefault("rotate", False)
        r.setdefault("run", None)
        if r["kind"] not in KINDS:
            raise ValueError(f"rule '{rid}': kind must be one of {sorted(KINDS)}")
        if r["priority"] not in PRIORITIES:
            raise ValueError(f"rule '{rid}': priority must be one of {sorted(PRIORITIES)}")
        if r["run"] is not None and r["run"] not in schedule:
            raise ValueError(f"rule '{rid}': run '{r['run']}' is not in settings.schedule")
        for key in ("every_days", "pick", "lookback_days"):
            if not isinstance(r[key], int) or r[key] < 1:
                raise ValueError(f"rule '{rid}': {key} must be a positive integer")
        sources = r.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"rule '{rid}': needs a non-empty 'sources' list")
        for s in sources:
            if not isinstance(s, dict) or s.get("type") not in SOURCE_TYPES:
                raise ValueError(f"rule '{rid}': source type must be one of {sorted(SOURCE_TYPES)}")
            if s["type"] in ("rss", "gnews") and not s.get("url"):
                raise ValueError(f"rule '{rid}': {s['type']} source needs a 'url'")
            s.setdefault("name", s.get("url") or s["type"])
    return doc
