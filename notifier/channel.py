"""How a notification leaves the machine, and which way the environment picks.

Channels are peers behind one small protocol, so adding a third is a module and
a branch in `from_env` -- nothing else in the app changes.

Telegram is the least trouble: free, self-serve, no webhook and no hosting.
E-mail is the fallback that needs no new account at all. Viber's own bot terms put a EUR 100 monthly
maintenance fee on every bot created after 5 February 2024, plus per-delivery
charges for the kind of unprompted message this app sends; bots registered
before that date are grandfathered onto the free legacy model, which is the only
case where the Viber channel is worth configuring. SMS to a Bulgarian number
needs a pre-registered domestic sender ID (business only, ~4 weeks) or arrives
from an unpredictable foreign number, so it is not implemented.
"""
from __future__ import annotations

import os
from typing import Protocol

from .watch import Message


class NotifyFailed(RuntimeError):
    """The message did not go out. The caller must not advance its snapshot."""


class Sender(Protocol):
    """All the CLI needs of a channel, and what the tests substitute."""

    name: str

    def send(self, message: Message) -> None:
        ...


class Console:
    """Prints instead of sending. What --dry-run uses, and the fallback when
    nothing is configured -- so a half-set-up cron job shows in the log rather
    than silently doing nothing."""

    name = "console"

    def send(self, message: Message) -> None:
        print("---- would send ----")
        print(f"Subject: {message.subject}")
        print()
        print(message.text)
        print("--------------------")


def from_env() -> tuple[Sender, str]:
    """The channel the environment asks for and -- when it falls back to the
    console -- what was missing. NOTIFY_CHANNEL pins one explicitly; otherwise
    whichever is configured wins, e-mail first."""
    want = os.getenv("NOTIFY_CHANNEL", "").strip().lower()

    if want in ("", "telegram"):
        from .telegram import Telegram
        sender_t, missing_t = Telegram.from_env()
        if sender_t is not None:
            return sender_t, ""
        if want:
            return Console(), missing_t

    if want in ("", "email", "mail"):
        from .mail import Email
        sender, missing = Email.from_env()
        if sender is not None:
            return sender, ""
        if want:
            return Console(), missing

    if want in ("", "viber"):
        from .viber import Viber
        sender_v, missing_v = Viber.from_env()
        if sender_v is not None:
            return sender_v, ""
        if want:
            return Console(), missing_v

    if want and want not in ("telegram", "email", "mail", "viber", "console"):
        return Console(), f"NOTIFY_CHANNEL={want!r} is not a channel"
    return Console(), "no channel configured (see .env.example)"
