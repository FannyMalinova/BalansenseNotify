"""Telegram delivery. Free, self-serve, and the least trouble of the channels.

Setting it up is two minutes and needs nothing hosted: message @BotFather,
`/newbot`, take the token; message your own bot once so it may write to you;
then `python -m notifier.cli --telegram-chat-id` prints your chat id for you.
No webhook, no certificate, no contract, no monthly fee -- contrast Viber, whose
new bots carry a EUR 100 monthly maintenance fee.

Messages are sent as **plain text with no parse_mode**. Telegram's Markdown and
HTML modes would require escaping, and class names arrive from FitSys with
characters like `_`, `*` and `-` in them (`Body Ballet за начинаещи`,
`Реформър - индивидуален`); an unescaped `_` turns a message into an API error
or silently mangles it. Plain text cannot fail that way.

Nothing here is given a class roster: it receives a finished Message from
`watch.render`, which carries counts and never client names.
"""
from __future__ import annotations

import os

import requests

from .channel import NotifyFailed
from .watch import Message

API = "https://api.telegram.org"
TEXT_LIMIT = 4096          # Telegram rejects a longer `text` outright


class Telegram:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: int = 20) -> None:
        self.token, self.chat_id, self.timeout = token, chat_id, timeout

    @classmethod
    def from_env(cls) -> tuple["Telegram | None", str]:
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not (token and chat_id):
            missing = ", ".join(name for name, value in
                                (("TELEGRAM_TOKEN", token),
                                 ("TELEGRAM_CHAT_ID", chat_id)) if not value)
            return None, f"{missing} not set in .env"
        return cls(token, chat_id), ""

    def _call(self, method: str, payload: dict) -> dict:
        try:
            r = requests.post(f"{API}/bot{self.token}/{method}",
                              timeout=self.timeout, json=payload)
        except requests.RequestException as exc:
            raise NotifyFailed(f"Telegram unreachable: {exc}") from exc
        # Telegram uses real HTTP status codes *and* an `ok` flag; the body
        # carries the useful part, so parse it before trusting the status.
        try:
            body = r.json()
        except ValueError:
            raise NotifyFailed(f"Telegram sent no JSON (HTTP {r.status_code}): "
                               f"{r.text[:200]}") from None
        if not body.get("ok"):
            code = body.get("error_code", r.status_code)
            why = body.get("description") or "(no description)"
            hint = ""
            if code == 401:
                hint = " -- TELEGRAM_TOKEN looks wrong; check it with @BotFather"
            elif code in (400, 403) and "chat" in why.lower():
                hint = (" -- send your bot a message first; a bot may only "
                        "write to someone who has started a chat with it")
            raise NotifyFailed(f"Telegram refused it: {code} {why}{hint}")
        return body

    def send(self, message: Message) -> None:
        # Telegram has no subject; the body already opens with the summary.
        text = message.text
        if len(text) > TEXT_LIMIT:
            text = text[:TEXT_LIMIT - 1] + "…"
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                   "disable_notification": False})


def chat_ids(token: str, timeout: int = 20) -> list[dict]:
    """Every chat that has written to the bot recently, for setup.

    Telegram keeps undelivered updates for 24 hours only, so this finds nothing
    until you have messaged the bot -- which is the same action that grants it
    permission to write back.
    """
    finder = Telegram(token, chat_id="", timeout=timeout)
    body = finder._call("getUpdates", {"limit": 100})
    seen: dict[str, dict] = {}
    for update in body.get("result") or []:
        for key in ("message", "edited_message", "channel_post"):
            chat = (update.get(key) or {}).get("chat") or {}
            if chat.get("id") is None:
                continue
            entry = {"id": str(chat["id"]),
                     "type": chat.get("type", "?"),
                     "who": " ".join(str(chat[f]) for f in
                                     ("first_name", "last_name", "title",
                                      "username") if chat.get(f)) or "?"}
            # Updates for the same chat carry different amounts of detail --
            # keep the fullest, or a sparse later one hides the username.
            best = seen.get(entry["id"])
            if best is None or len(entry["who"]) > len(best["who"]):
                seen[entry["id"]] = entry
    return list(seen.values())
