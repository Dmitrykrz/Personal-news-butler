"""Handle a prose Telegram message with a full Claude Code session over the project:
taste/rule tweaks, questions, and code changes. Code changes are git commits; the personal
config files are gitignored, so their previous contents are kept in the DB for /undo."""
import json
import subprocess
import sys

from .claude import run_claude
from .config import APP_DIR, RULES_FILE, TASTE_FILE, ConfigMissing, git, load_rules, log

AGENT_TOOLS = ("Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch", "WebSearch")
CODE_PATHS = ("newsagent/", "requirements.txt", "Dockerfile", "docker-compose.yml")
CONFIG_FILES = {"config/rules.yaml": RULES_FILE, "config/taste.md": TASTE_FILE}

AGENT_PROMPT = """You are the maintainer of a personal news digest bot, running inside its own
container. The bot's owner just sent this message from Telegram:

<message>
{text}
</message>
{context}
Start by reading CLAUDE.md in the current directory: it explains the project, where the logs
are, and what you may and may not do.

Decide what the message is:
- Taste / preference feedback ("less of X", "I liked this") -> edit config/taste.md.
- A structural request (sources, schedule times, counts, priorities) -> edit config/rules.yaml.
- A bug report or a request to change how the bot works -> investigate (logs, run records) and
  change the code under newsagent/. Keep changes minimal and in the style of the existing code.
  Test what you can (e.g. `python -m newsagent fetch`, `python -m compileall -q newsagent`).
- A question -> answer it; change nothing.
{setup}
Make the smallest change that does what the owner wants. Keep config files tidy: update or
merge existing lines instead of piling up contradictions. Do not run git commit: the bot
records your changes itself (and can /undo them).

Your final answer is sent back to the owner in Telegram: one to three short sentences, in the
language they wrote in, saying what you changed (or answering the question). No preamble.
"""

SETUP_PROMPT = """
First-time setup: {missing} does not exist yet. The owner is describing what they want.
Create the missing file(s) from their description, using config/rules.example.yaml and
config/taste.example.md as the format (keep the schema comment from the top of the rules
example). Choose real, free sources that fit (RSS feeds, Hacker News, GitHub trending, Google
News searches) and check new feeds with `python -m newsagent fetch <rule-id>`. If something
essential is unclear (schedule times, language), pick a sensible default and say which in
your answer, so the owner can correct it.
"""


def changed_files():
    out = git("status", "--porcelain").stdout
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def read_config():
    return {name: (p.read_text(encoding="utf-8") if p.exists() else None) for name, p in CONFIG_FILES.items()}


def restore_config(snapshot):
    for name, content in snapshot.items():
        path = CONFIG_FILES[name]
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding="utf-8")


def validate():
    """Return an error string if the working tree is broken, else None."""
    try:
        load_rules()
    except ConfigMissing:
        pass
    except ValueError as e:
        return str(e)
    proc = subprocess.run([sys.executable, "-m", "compileall", "-q", "newsagent"],
                          cwd=APP_DIR, capture_output=True, text=True)
    if proc.returncode != 0:
        return f"python does not compile: {(proc.stdout + proc.stderr)[-400:]}"
    return None


def handle_message(text, db, item=None):
    """Returns (reply_text, needs_restart)."""
    context = ""
    if item:
        context = (
            "\nThe owner sent it as a reply to this digest item:\n"
            f"rule: {item['rule_id']}\ntitle: {item['title']}\nsummary: {item['summary']}\nurl: {item['url']}\n"
        )
    before = read_config()
    missing = [name for name, content in before.items() if content is None]
    setup = SETUP_PROMPT.format(missing=" and ".join(missing)) if missing else ""
    reply = run_claude(AGENT_PROMPT.format(text=text, context=context, setup=setup),
                       tools=AGENT_TOOLS, cwd=APP_DIR, timeout=1500).strip()

    files = changed_files()
    after = read_config()
    config_before = {name: before[name] for name in before if after[name] != before[name]}
    if not files and not config_before:
        return reply, False
    error = validate()
    if error:
        git("checkout", "--", ".")
        git("clean", "-fdq")
        restore_config(before)
        log(f"agent: change broke the bot, reverted: {error}")
        return f"⚠️ My change broke something, so I reverted it. ({error[:300]})", False
    sha = None
    if files:
        git("add", "-A")
        git("commit", "-q", "-m", f"telegram: {text[:72]}")
        sha = git("rev-parse", "HEAD").stdout.strip()
    db.record_change(text[:200], sha, json.dumps(config_before, ensure_ascii=False))
    log(f"agent: changed {files + list(config_before)} for {text[:72]!r}")
    restart = any(f.startswith(CODE_PATHS) for f in files)
    return f"✅ {reply}" + ("\n\n♻️ Restarting to load the new code." if restart else ""), restart


def undo_last(db):
    """Revert the agent's last change (code commit and/or config edit). Returns (reply_text, needs_restart)."""
    change = db.last_change()
    if not change:
        return "Nothing to undo.", False
    restart = False
    if change["commit_sha"]:
        files = git("show", "--name-only", "--format=", change["commit_sha"]).stdout.split()
        if git("revert", "--no-edit", change["commit_sha"], check=False).returncode != 0:
            git("revert", "--abort", check=False)
            return "⚠️ Couldn't revert that code change: later changes touch the same lines.", False
        restart = any(f.startswith(CODE_PATHS) for f in files)
    restore_config(json.loads(change["config"] or "{}"))
    db.delete_change(change["id"])
    return f"↩️ Reverted: {change['message'][:72]}" + ("\n\n♻️ Restarting to load the code." if restart else ""), restart
