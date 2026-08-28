"""Viber delivery. Kept, but not the default -- see the cost note below.

Since 5 February 2024 a new Viber bot carries a EUR 100 monthly maintenance fee
per bot plus per-delivery charges for chatbot-initiated messages, which is what
every message this app sends is. Bots registered before that date stay on the
free legacy model, and that is the one case where configuring this channel makes
sense. Set NOTIFY_CHANNEL=viber to use it.

Viber's Bot API is send-only from here: a bot auth token plus the receiver id of
the person to message. To find that id, either read `sender.id` from a Message
callback / `user.id` from a Subscribed callback, or call
`POST https://chatapi.viber.com/pa/get_account_info`, whose `members` array
lists each admin's `id`. A webhook with a CA-signed certificate must be set
before 1-on-1 messaging works at all, and the receiver must have subscribed by
messaging the bot once.

Nothing here is given a class roster: it receives a finished Message from
`watch.render`, which carries counts and never client names.
"""
from __future__ import annotations

import os

import requests

from .channel import NotifyFailed
from .watch import Message

SEND_URL = "https://chatapi.viber.com/pa/send_message"


class Viber:
    name = "viber"

    def __init__(self, token: str, receiver: str, sender: str = "BalanSense"):
        self.token, self.receiver, self.sender = token, receiver, sender

    @classmethod
    def from_env(cls) -> tuple["Viber | None", str]:
        token = os.getenv("VIBER_TOKEN", "").strip()
        receiver = os.getenv("VIBER_RECEIVER", "").strip()
        if not (token and receiver):
            missing = ", ".join(name for name, value in
                                (("VIBER_TOKEN", token),
                                 ("VIBER_RECEIVER", receiver)) if not value)
            return None, f"{missing} not set in .env"
        return cls(token, receiver,
                   os.getenv("VIBER_SENDER_NAME", "").strip() or "BalanSense"), ""

    def send(self, message: Message) -> None:
        # Viber has no subject; the body already opens with the same summary.
        try:
            r = requests.post(SEND_URL, timeout=20,
                              headers={"X-Viber-Auth-Token": self.token},
                              json={"receiver": self.receiver, "type": "text",
                                    "text": message.text,
                                    "sender": {"name": self.sender}})
        except requests.RequestException as exc:
            raise NotifyFailed(f"Viber unreachable: {exc}") from exc
        # Viber answers HTTP 200 even when it refuses the message, so the HTTP
        # status says nothing useful; status 0 in the body is the only success.
        try:
            body = r.json()
        except ValueError:
            raise NotifyFailed(f"Viber sent no JSON (HTTP {r.status_code}): "
                               f"{r.text[:200]}") from None
        if body.get("status") != 0:
            raise NotifyFailed(
                f"Viber refused it: status={body.get('status')} "
                f"{body.get('status_message') or ''}".strip())
