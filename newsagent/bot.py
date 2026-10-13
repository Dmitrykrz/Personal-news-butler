"""Telegram listener + scheduler. Scheduled runs come from settings.schedule in rules.yaml;
/fire_once runs a digest now; any other text goes to the Claude agent (feedback.py)."""
import os
import threading
import time
import traceback
from datetime import timedelta
from html import escape

from .claude import run_claude
from .config import (
    RULES_FILE,
    TASTE_FILE,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
    TIMEZONE,
    ConfigMissing,
    ensure_dirs,
    load_rules,
    log,
    now_local,
    timezone_problem,
)
from .db import DB
from .digest import plan_rules, run_digest
from .feedback import handle_message, undo_last
from .telegram import Telegram

SCHEDULE_GRACE = timedelta(hours=2)  # a run missed by more than this (bot was down) is skipped

SETUP = """👋 I don't have {missing} yet, so I don't know what news to send you.

Describe what you want in a message, for example:
• who you are and what you're interested in (and what you're not)
• sites, topics or places to follow, and how often
• how many items a day, at what times, in which language

I'll create the configuration from that. You can refine it any time later by just writing to me."""

HELP = """Hi! I'm your news digest bot.

/fire_once — run a digest now
/status — schedule and what's due
/undo — revert the last change I made
/dev <request> — bug fixes and code changes (gives Claude a shell)

Anything else you write goes to Claude: taste feedback ("less of X"), rule changes ("France news daily") or questions.
Reply to a specific news message to talk about that item."""


def format_item(item):
    return f"<b>{escape(item['title'])}</b>\n\n{escape(item['summary'])}\n\n{escape(item['url'])}"


def missing_config():
    """The setup message if rules or taste don't exist yet, else None."""
    missing = [name for name, path in (("any rules", RULES_FILE), ("a taste profile", TASTE_FILE))
               if not path.exists()]
    return SETUP.format(missing=" or ".join(missing)) if missing else None


def claude_problem():
    """One tiny Claude call to check that the token works."""
    try:
        run_claude("Reply with the single word OK.", model="haiku", timeout=180)
    except Exception as e:
        return f"Claude doesn't work: {e}"
    return None


class Bot:
    def __init__(self):
        self.tg = Telegram(TELEGRAM_TOKEN)
        self.chat_id = int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else None
        self.db = DB()
        self.digest_lock = threading.Lock()
        self.agent_lock = threading.Lock()

    def say(self, text, reply_to=None):
        return self.tg.send(self.chat_id, text, preview=False, reply_to=reply_to)

    def background(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def restart(self):
        """Exit; docker's restart policy starts the container again with the new code."""
        with self.digest_lock:
            log("bot: restarting to load new code")
            os._exit(0)

    # --- jobs -------------------------------------------------------------

    def check_setup(self):
        """At startup, tell the owner in Telegram what is still missing or broken."""
        problems = [p for p in (timezone_problem(), claude_problem()) if p]
        if problems:
            self.say("\n\n".join(f"⚠️ {p}" for p in problems))
        setup = missing_config()
        if setup:
            self.say(setup)
            return
        try:
            load_rules()
        except ValueError as e:
            self.say(f"⚠️ config/rules.yaml is broken: {e}")

    def digest(self, run_name=None):
        if run_name is None and missing_config():
            self.say(missing_config())
            return
        if not self.digest_lock.acquire(blocking=False):
            if run_name is None:
                self.say("A digest is already running, hang on.")
            return
        try:
            if run_name is None:
                self.say("🔎 Collecting news… (can take up to ~10 minutes)")
            items = run_digest(lambda item: self.tg.send(self.chat_id, format_item(item), html=True),
                               run_name=run_name)
            if not items and run_name is None:
                self.say("Nothing worth sending right now.")
        except Exception as e:
            log(traceback.format_exc())
            self.say(f"⚠️ Digest failed: {str(e)[:500]}")
        finally:
            self.digest_lock.release()

    def agent(self, text, reply_to, dev=False):
        with self.agent_lock:
            item = self.db.sent_by_message(reply_to) if reply_to else None
            self.tg.typing(self.chat_id)
            restart = False
            try:
                answer, restart = handle_message(text, self.db, dict(item) if item else None, dev=dev)
            except Exception as e:
                log(traceback.format_exc())
                answer = f"⚠️ Couldn't process that: {str(e)[:500]}"
            self.db.log_feedback(text, reply_to, answer)
            self.say(answer)
            if restart:
                self.restart()

    def undo(self):
        with self.agent_lock:
            answer, restart = undo_last(self.db)
            self.say(answer)
            if restart:
                self.restart()

    def status(self):
        setup = missing_config()
        if setup:
            self.say(setup)
            return
        try:
            doc = load_rules()
        except ValueError as e:
            self.say(f"⚠️ {e}")
            return
        settings, rules = doc["settings"], doc["rules"]
        now = now_local()
        today = now.date().toordinal()
        lines = [f"Now: {now:%a %H:%M} ({TIMEZONE})",
                 f"Sent today: {self.db.sent_on_day(today)}"]
        for name, at in settings["schedule"].items():
            due = [rid for rid, p in plan_rules(rules, self.db, today, name).items() if p["due"]]
            lines.append(f"{name} {at}: {', '.join(due) or 'nothing due'}")
        self.say("\n".join(lines))

    # --- scheduler --------------------------------------------------------

    def scheduler(self):
        db = DB()
        while True:
            try:
                settings = load_rules()["settings"]
                now = now_local()
                for name, at in settings["schedule"].items():
                    hour, minute = map(int, at.split(":"))
                    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    key = f"{now:%Y-%m-%d}"
                    if slot <= now < slot + SCHEDULE_GRACE and db.get_meta(f"slot:{name}") != key:
                        db.set_meta(f"slot:{name}", key)
                        log(f"scheduler: starting {name} run")
                        self.digest(name)
            except ConfigMissing:
                pass  # nothing to schedule until the owner describes what they want
            except Exception:
                log(traceback.format_exc())
            time.sleep(30)

    # --- dispatch ---------------------------------------------------------

    def handle(self, msg):
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if self.chat_id is None:
            self.tg.send(chat, f"Your chat id is {chat}. Put TELEGRAM_CHAT_ID={chat} into .env and restart the bot.")
            log(f"bot: unconfigured, told chat {chat} its id")
            return
        if chat != self.chat_id:
            log(f"bot: ignoring message from foreign chat {chat}")
            return
        if not text:
            return
        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            self.say(HELP)
        elif command == "/fire_once" or text.lower() == "fire once":
            self.background(self.digest)
        elif command == "/status":
            self.status()
        elif command == "/undo":
            self.background(self.undo)
        else:
            reply_to = (msg.get("reply_to_message") or {}).get("message_id")
            dev = command == "/dev"
            if dev:
                text = text[len(text.split()[0]):].strip()
                if not text:
                    self.say("Usage: /dev <what to fix or change in the bot's code>")
                    return
            self.background(self.agent, text, reply_to, dev)

    def run(self):
        self.tg.call("setMyCommands", commands=[
            {"command": "fire_once", "description": "Run a digest now"},
            {"command": "status", "description": "Schedule and what's due"},
            {"command": "undo", "description": "Revert the last change"},
            {"command": "dev", "description": "Change the bot's code (gives Claude a shell)"},
        ])
        if self.chat_id is not None:
            self.background(self.check_setup)
        if timezone_problem():
            log(f"bot: scheduler disabled: {timezone_problem()}")
        else:
            self.background(self.scheduler)
        log("bot: polling Telegram")
        offset = None
        while True:
            try:
                updates = self.tg.get_updates(offset)
            except Exception as e:
                log(f"bot: polling error: {e}")
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        self.handle(u["message"])
                    except Exception:
                        log(traceback.format_exc())


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    ensure_dirs()
    Bot().run()
