"""E-mail delivery over plain SMTP.

Free, needs no provider approval and no regulator, which is why it is the
default channel. With push enabled on the phone's mail app a message lands on
the lock screen much like an SMS -- and the subject line is what shows there, so
`watch.render` puts the whole change in it.

Most providers now refuse an account password over SMTP and want an
app-specific password instead (Yahoo: Account Security -> Generate app password;
Gmail: App passwords, needs 2-step verification). That is what SMTP_PASS holds.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from .channel import NotifyFailed
from .watch import Message

# Ports 465 (implicit TLS) and 587 (STARTTLS) are the two anyone uses; 25 is
# unauthenticated relay and is refused here rather than sent in the clear.
SSL_PORT, STARTTLS_PORT = 465, 587


class Email:
    name = "email"

    def __init__(self, host: str, port: int, user: str, password: str,
                 to: str, sender: str = "", timeout: int = 20) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.to, self.sender, self.timeout = to, sender or user, timeout

    @classmethod
    def from_env(cls) -> tuple["Email | None", str]:
        """An Email sender, or None and what was missing."""
        values = {name: os.getenv(name, "").strip()
                  for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return None, ", ".join(missing) + " not set in .env"
        raw_port = os.getenv("SMTP_PORT", "").strip()
        try:
            port = int(raw_port) if raw_port else SSL_PORT
        except ValueError:
            return None, f"SMTP_PORT={raw_port!r} is not a number"
        if port == 25:
            return None, "SMTP_PORT=25 is unauthenticated; use 465 or 587"
        return cls(host=values["SMTP_HOST"], port=port, user=values["SMTP_USER"],
                   password=values["SMTP_PASS"], to=values["MAIL_TO"],
                   sender=os.getenv("MAIL_FROM", "").strip()), ""

    def _build(self, message: Message) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = self.sender
        msg["To"] = self.to
        # Keeps a run of notifications out of one collapsed mail thread, so each
        # sign-up notifies separately instead of quietly joining an old thread.
        msg["X-Entity-Ref-ID"] = "balansense-notify"
        msg.set_content(message.text, subtype="plain", charset="utf-8")
        return msg

    def send(self, message: Message) -> None:
        msg = self._build(message)
        context = ssl.create_default_context()
        try:
            if self.port == SSL_PORT:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                      context=context) as s:
                    s.login(self.user, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port,
                                  timeout=self.timeout) as s:
                    s.starttls(context=context)
                    s.login(self.user, self.password)
                    s.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyFailed(
                f"{self.host} rejected the login for {self.user}. Most "
                f"providers need an app-specific password here, not the "
                f"account password. ({exc.smtp_code} "
                f"{_text(exc.smtp_error)})") from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise NotifyFailed(
                f"could not send via {self.host}:{self.port}: {exc}") from exc


def _text(raw) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
