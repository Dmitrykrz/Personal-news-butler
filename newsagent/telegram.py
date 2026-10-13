"""Minimal Telegram Bot API client (long polling, plain requests)."""
import time

import requests

from .config import log


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = requests.Session()

    def call(self, method, timeout=30, **params):
        for _ in range(5):
            r = self.session.post(self.base + method, json=params, timeout=timeout)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                time.sleep(retry + 1)
                continue
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        raise RuntimeError(f"telegram {method}: rate limited")

    def get_updates(self, offset):
        """Long poll: blocks up to 50s waiting for new messages."""
        r = self.session.post(self.base + "getUpdates", timeout=60, json={
            "offset": offset, "timeout": 50, "allowed_updates": ["message"]})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getUpdates: {data.get('description')}")
        return data["result"]

    def send(self, chat_id, text, html=False, preview=True, reply_to=None):
        params = {"chat_id": chat_id, "text": text,
                  "link_preview_options": {"is_disabled": not preview}}
        if html:
            params["parse_mode"] = "HTML"
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.call("sendMessage", **params)["message_id"]
        except Exception as e:
            log(f"telegram send failed: {e}")
            raise

    def typing(self, chat_id):
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
