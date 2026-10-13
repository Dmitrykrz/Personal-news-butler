"""SQLite state: per-rule schedule state, sent items, feedback log, scheduler bookkeeping."""
import sqlite3
import time

from .config import DB_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS rule_state (
    rule_id TEXT PRIMARY KEY, last_day INTEGER, rotation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent (
    id INTEGER PRIMARY KEY, url TEXT, title TEXT, summary TEXT, rule_id TEXT,
    day INTEGER, sent_at REAL, message_id INTEGER
);
CREATE INDEX IF NOT EXISTS sent_url ON sent(url);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY, text TEXT, reply_to INTEGER, result TEXT, created_at REAL
);
-- one row per agent change, for /undo: the git commit (code) and the previous config contents (JSON)
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, message TEXT, commit_sha TEXT, config TEXT, created_at REAL
);
"""


class DB:
    """`day` values are calendar days (date.toordinal()) in the configured timezone."""

    def __init__(self, path=DB_FILE):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

    def rule_state(self, rule_id):
        row = self.conn.execute("SELECT last_day, rotation FROM rule_state WHERE rule_id=?", (rule_id,)).fetchone()
        return (row["last_day"], row["rotation"]) if row else (None, 0)

    def set_rule_state(self, rule_id, last_day, rotation):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO rule_state(rule_id, last_day, rotation) VALUES(?,?,?)",
                (rule_id, last_day, rotation),
            )

    def seen_urls(self) -> set:
        return {r["url"] for r in self.conn.execute("SELECT url FROM sent")}

    def recent_sent(self, days=14):
        since = time.time() - days * 86400
        return self.conn.execute(
            "SELECT rule_id, title FROM sent WHERE sent_at > ? AND message_id IS NOT NULL ORDER BY sent_at",
            (since,),
        ).fetchall()

    def sent_on_day(self, day) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sent WHERE day=? AND message_id IS NOT NULL", (day,)
        ).fetchone()[0]

    def record_sent(self, item, day, message_id):
        with self.conn:
            self.conn.execute(
                "INSERT INTO sent(url, title, summary, rule_id, day, sent_at, message_id) VALUES(?,?,?,?,?,?,?)",
                (item["url"], item["title"], item["summary"], item["rule_id"], day, time.time(), message_id),
            )
            # also remember the original candidate URL (e.g. a Google News redirect) so it isn't re-offered
            if item.get("candidate_url") and item["candidate_url"] != item["url"]:
                self.conn.execute(
                    "INSERT INTO sent(url, title, rule_id, day, sent_at) VALUES(?,?,?,?,?)",
                    (item["candidate_url"], item["title"], item["rule_id"], day, time.time()),
                )

    def sent_by_message(self, message_id):
        return self.conn.execute("SELECT * FROM sent WHERE message_id=?", (message_id,)).fetchone()

    def log_feedback(self, text, reply_to, result):
        with self.conn:
            self.conn.execute(
                "INSERT INTO feedback(text, reply_to, result, created_at) VALUES(?,?,?,?)",
                (text, reply_to, result, time.time()),
            )

    def record_change(self, message, commit_sha, config_json):
        with self.conn:
            self.conn.execute(
                "INSERT INTO changes(message, commit_sha, config, created_at) VALUES(?,?,?,?)",
                (message, commit_sha, config_json, time.time()),
            )

    def last_change(self):
        return self.conn.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 1").fetchone()

    def delete_change(self, change_id):
        with self.conn:
            self.conn.execute("DELETE FROM changes WHERE id=?", (change_id,))
