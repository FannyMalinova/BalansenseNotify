"""Offline checks. No network, no FitSys account -- the feed rows are synthetic.

Run:  .venv/bin/python -m tests.test_notifier

This app is standalone -- it does not import the FitSys scheduler -- so the
parser is checked against rows shaped like the live feed, and its own site.json
is compared with the scheduler's map whenever the scheduler happens to be on
the same machine (skipped otherwise, e.g. inside the container).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from notifier import channel, mail, state, viber, watch

NOW = datetime(2026, 9, 2, 8, 0)
MSG = watch.Message(subject="+1 booked · test", text="+1 booked\ntest")
TMP = Path("/tmp/balansensenotify_test_state.json")


class FakeRequests:
    """Stands in for the `requests` module inside a channel, so the exact call
    it makes can be inspected without a token or a network."""

    RequestException = __import__("requests").RequestException

    def __init__(self, *, body=None, status_code=200, raises=None, text=""):
        self.body, self.status_code = body, status_code
        self.raises, self.text = raises, text
        self.calls: list[dict] = []

    def post(self, url, timeout=None, headers=None, json=None):
        self.calls.append({"url": url, "timeout": timeout,
                           "headers": headers or {}, "json": json or {}})
        if self.raises:
            raise self.raises
        outer = self

        class Resp:
            status_code = outer.status_code
            text = outer.text

            def json(self):
                if outer.body is None:
                    raise ValueError("not json")
                return outer.body
        return Resp()


class FakeClient:
    """Stands in for fitsys_api.Client so the CLI can be driven with no network."""

    source = "stub"

    def __init__(self, reading):
        self._reading = reading

    def read(self, start, end, *, instructor="", since=None):
        return self._reading


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok   {msg}")


def occ(rid, day, hhmm, kind, who, booked, cap=12, room="зала 1") -> watch.Occupancy:
    h, m = (int(x) for x in hhmm.split(":"))
    return watch.Occupancy(remote_id=rid, when=datetime(2026, 9, day, h, m),
                           class_type=kind, instructor=who, room=room,
                           booked=booked, capacity=cap)


LIVE = {
    "11": occ("11", 2, "09:00", "Реформър група", "Фани М", 8),
    "12": occ("12", 3, "18:30", "Хатха йога", "Снежана", 5),
    "13": occ("13", 4, "09:00", "Реформър група", "Фани М", 3),
}


def test_diff():
    print("watch.diff")
    changes = watch.diff({"11": 7, "12": 5, "99": 4}, LIVE)
    check([(c.occ.remote_id, c.kind, c.delta) for c in changes]
          == [("11", "signup", 1)],
          "only a count that moved against a seen baseline is reported")
    check(watch.diff({"11": 9, "12": 5}, LIVE)[0].kind == "signout",
          "a falling count reads as a sign-out")
    check(watch.diff({}, LIVE) == [],
          "a first look reports nothing (no false flood of sign-ups)")
    check(watch.diff({"13": 1}, LIVE) and not watch.diff({"13": 3}, LIVE),
          "an unchanged count is not a change")
    check([c.occ.remote_id for c in watch.diff({"12": 1, "13": 1}, LIVE)]
          == ["12", "13"], "several changes come back oldest class first")
    check(watch.total_booked(LIVE) == 16, "total booked sums the window")


def test_message():
    print("watch.render")
    msg = watch.render(watch.diff({"11": 7}, LIVE), total=16, classes=3)
    check("+1 booked" in msg.text, "the change is stated first")
    check("Реформър група · Wednesday 02.09 09:00 · зала 1" in msg.text,
          f"class name and room stay as FitSys spells them, the weekday is "
          f"English, got {msg.text.splitlines()[1]!r}")
    check("8/12 · Фани М" in msg.text, "how full it is now, and who teaches it")
    check("Total booked: 16 in 3 classes" in msg.text,
          "the running total is included")
    check("-2 cancelled" in watch.render(watch.diff({"11": 10}, LIVE),
                                         total=16, classes=3).text,
          "a sign-out is reported as a cancellation, with the count")
    check("in 1 class" in watch.render(watch.diff({"11": 7}, LIVE), total=8,
                                     classes=1).text,
          "singular 'class' when only one is watched")

    print("watch.render subject line")
    check(msg.subject == "+1 booked · Реформър група · Wednesday 02.09 09:00 · "
                         "зала 1 · 8/12",
          f"one change puts the whole thing in the subject, got {msg.subject!r}")
    many = watch.render(watch.diff({"11": 7, "12": 7}, LIVE), total=16, classes=3)
    check(many.subject == "+1 -2 · 2 classes · 16 booked total",
          f"several changes summarise, got {many.subject!r}")
    check(len(many.subject) < 80,
          "the subject stays short enough for a lock screen")


def test_state():
    print("state: the local file")
    TMP.unlink(missing_ok=True)
    store = state.FileStore(TMP)
    check(store.load() == {}, "a missing snapshot is an empty baseline")
    store.save({rid: o.booked for rid, o in LIVE.items()})
    check(store.load() == {"11": 8, "12": 5, "13": 3},
          "the snapshot round-trips as ids and counts only")
    text = TMP.read_text(encoding="utf-8")
    check("customer" not in text and "name" not in text,
          "the snapshot on disk holds no client fields")
    store.save({"11": 8})
    check(store.load() == {"11": 8},
          "saving the current reading prunes ids that left the window")
    TMP.unlink(missing_ok=True)


def test_state_backend_selection():
    """Cloud Run throws the container filesystem away between executions, so
    STATE_URI has to be able to point somewhere that survives."""
    print("state.from_env")
    import os

    before = os.environ.get("STATE_URI")
    try:
        os.environ.pop("STATE_URI", None)
        check(state.from_env().name == "file",
              "with nothing set the snapshot is a local file")
        check(state.from_env(str(TMP)).path == TMP,
              "an explicit --state path wins")

        os.environ["STATE_URI"] = "gs://my-bucket/notify/state.json"
        store = state.from_env()
        check(store.name == "gcs", f"a gs:// URI selects GCS, got {store.name}")
        check(store.bucket_name == "my-bucket"
              and store.blob_name == "notify/state.json",
              f"bucket and object are split correctly, got {store}")
        check(str(store) == "gs://my-bucket/notify/state.json",
              "and it prints back as the URI it came from")
        check(state.from_env(str(TMP)).name == "file",
              "--state still overrides STATE_URI")

        for bad in ("gs://", "gs://bucket-only"):
            try:
                state.from_env(bad)
                raise AssertionError(f"{bad!r} should be rejected")
            except RuntimeError as exc:
                check("needs both a bucket and an object name" in str(exc),
                      f"{bad!r} is refused with an example, not silently "
                      f"treated as a path")
    finally:
        if before is None:
            os.environ.pop("STATE_URI", None)
        else:
            os.environ["STATE_URI"] = before


def test_gcs_store_guards_against_overlap():
    """Two Cloud Run executions can overlap. The loser must not clobber the
    winner's snapshot -- the sign-ups recorded in it would go with it."""
    print("state.GcsStore")
    import sys
    import types

    calls = []

    class FakeBlob:
        generation = 1729

        def __init__(self, exists=True):
            self.exists = exists

        def download_as_text(self):
            if not self.exists:
                raise FakeExceptions.NotFound("nope")
            return '{"booked": {"11": 8}}'

        def upload_from_string(self, data, content_type=None,
                              if_generation_match=None):
            calls.append({"data": data, "content_type": content_type,
                          "if_generation_match": if_generation_match})
            if if_generation_match == 999:
                raise FakeExceptions.PreconditionFailed("changed")

    class FakeExceptions:
        class NotFound(Exception):
            pass

        class PreconditionFailed(Exception):
            pass

    # Stand in for google.cloud.storage / google.api_core.exceptions.
    fake_storage = types.ModuleType("google.cloud.storage")
    saved = {k: sys.modules.get(k) for k in
             ("google", "google.cloud", "google.cloud.storage",
              "google.api_core", "google.api_core.exceptions")}
    try:
        google = types.ModuleType("google")
        cloud = types.ModuleType("google.cloud")
        api_core = types.ModuleType("google.api_core")
        exc_mod = types.ModuleType("google.api_core.exceptions")
        exc_mod.NotFound = FakeExceptions.NotFound
        exc_mod.PreconditionFailed = FakeExceptions.PreconditionFailed
        api_core.exceptions = exc_mod
        google.cloud, google.api_core = cloud, api_core
        cloud.storage = fake_storage
        sys.modules.update({"google": google, "google.cloud": cloud,
                            "google.cloud.storage": fake_storage,
                            "google.api_core": api_core,
                            "google.api_core.exceptions": exc_mod})

        store = state.GcsStore("b", "state.json")
        blob = FakeBlob()
        store._blob = lambda: blob                          # type: ignore[method-assign]

        check(store.load() == {"11": 8}, "an existing object loads")
        store.save({"11": 9})
        check(calls[-1]["if_generation_match"] == 1729,
              f"the write is conditional on the generation just read, got "
              f"{calls[-1]['if_generation_match']}")
        check(calls[-1]["content_type"] == "application/json",
              "and is stored as JSON")

        missing = state.GcsStore("b", "state.json")
        missing._blob = lambda: FakeBlob(exists=False)       # type: ignore[method-assign]
        check(missing.load() == {},
              "a first-ever run finds no object and starts empty")
        missing.save({"11": 1})
        check(calls[-1]["if_generation_match"] == 0,
              "the first write requires the object not to exist yet (gen 0), so "
              "two first runs cannot both create it")

        losing = state.GcsStore("b", "state.json")
        losing._generation = 999
        losing._blob = lambda: blob                          # type: ignore[method-assign]
        try:
            losing.save({"11": 3})
            raise AssertionError("a clobbering write must be refused")
        except state.StateConflict as exc:
            check("another execution overlapped" in str(exc),
                  f"the loser aborts with an explanation, got {exc}")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_instructor_names():
    """FitSys abbreviates surnames, so the name a human configures is never the
    name in the feed. These are the real spellings read from the live calendar."""
    print("watch.instructor_matches")
    m = watch.instructor_matches
    check(m("Фани Малинова", "Фани М"),
          "'Фани Малинова' matches the calendar's 'Фани М'")
    check(m("Фани Малинова", "Фани Малинова"), "the full name still matches")
    check(m("Фани", "Фани М"), "a given name on its own still matches")
    check(not m("Фани Малинова", "Ани М"),
          "'Ани М' is not 'Фани М' -- a substring test would have matched it")
    check(not m("Фани Малинова", "Фани П"),
          "a different surname initial is a different person")
    check(m("Здравка Димитрова", "Здравка  Д")
          and not m("Здравка Димитрова", "Здравка М"),
          "the studio's two Здравкas are told apart, double space and all")
    check(m("Михаела Петрова", "Михаела"),
          "a feed name with no surname at all matches on the given name")
    check(not m("", "Фани М") and not m("Фани Малинова", ""),
          "an empty name on either side matches nothing")
    check(not m("Ели Александрова", "Фани М"), "unrelated names do not match")


def test_telegram():
    """What goes to Telegram, and the errors that actually happen in setup."""
    print("telegram: the outbound request")
    from notifier import telegram

    real = telegram.requests
    try:
        fake = FakeRequests(body={"ok": True, "result": {"message_id": 7}})
        telegram.requests = fake
        telegram.Telegram("123:AAH-tok", "456789").send(MSG)
        call = fake.calls[0]
        check(call["url"] == "https://api.telegram.org/bot123:AAH-tok/sendMessage",
              f"the token sits in the URL path, as Telegram wants, got "
              f"{call['url']}")
        check(call["json"] == {"chat_id": "456789", "text": MSG.text,
                              "disable_notification": False},
              f"chat_id and plain text, nothing else, got {call['json']!r}")
        check("parse_mode" not in call["json"],
              "no parse_mode: class names contain _ and * and would need escaping")

        # A real class name that Markdown mode would have broken.
        awkward = watch.Message(subject="s",
                                text="+1 booked\nBody_Ballet *за* начинаещи")
        telegram.requests = fake = FakeRequests(body={"ok": True})
        telegram.Telegram("t", "c").send(awkward)
        check(fake.calls[0]["json"]["text"] == awkward.text,
              "underscores and asterisks travel untouched")

        long_message = watch.Message(subject="s", text="я" * 5000)
        telegram.requests = fake = FakeRequests(body={"ok": True})
        telegram.Telegram("t", "c").send(long_message)
        sent_text = fake.calls[0]["json"]["text"]
        check(len(sent_text) == telegram.TEXT_LIMIT and sent_text.endswith("…"),
              f"an over-long message is trimmed to Telegram's limit, not "
              f"rejected, got {len(sent_text)}")

        telegram.requests = FakeRequests(
            body={"ok": False, "error_code": 401, "description": "Unauthorized"})
        try:
            telegram.Telegram("bad", "c").send(MSG)
            raise AssertionError("a bad token must raise")
        except channel.NotifyFailed as exc:
            check("@BotFather" in str(exc),
                  f"401 points at the token, got {exc}")

        telegram.requests = FakeRequests(
            body={"ok": False, "error_code": 400,
                  "description": "Bad Request: chat not found"})
        try:
            telegram.Telegram("t", "nope").send(MSG)
            raise AssertionError("an unknown chat must raise")
        except channel.NotifyFailed as exc:
            check("send your bot a message first" in str(exc),
                  f"'chat not found' explains the real cause, got {exc}")

        # getUpdates, the setup helper
        telegram.requests = FakeRequests(body={"ok": True, "result": [
            {"message": {"chat": {"id": 456789, "type": "private",
                                  "first_name": "Фани", "username": "fanny"}}},
            {"message": {"chat": {"id": 456789, "type": "private",
                                  "first_name": "Фани"}}},
            {"channel_post": {"chat": {"id": -100123, "type": "channel",
                                       "title": "BalanSense"}}},
            {"my_chat_member": {"chat": {"id": 999}}},          # no message
        ]})
        found = telegram.chat_ids("t")
        check([c["id"] for c in found] == ["456789", "-100123"],
              f"each chat appears once, non-message updates ignored, got {found}")
        check(found[0]["who"] == "Фани fanny" and found[0]["type"] == "private",
              f"the chat is described so the right one can be picked, got {found[0]}")

        telegram.requests = FakeRequests(body={"ok": True, "result": []})
        check(telegram.chat_ids("t") == [],
              "no chats yet is an empty list, not an error")
    finally:
        telegram.requests = real


def test_email():
    """The message that would leave over SMTP, and how the channel is chosen.
    No connection is made: smtplib is substituted."""
    print("mail: the message and the transport")
    import os
    import smtplib

    msg = watch.render(watch.diff({"11": 7}, LIVE), total=16, classes=3)
    built = mail.Email("smtp.example.com", 465, "me@example.com", "pw",
                       "me@example.com")._build(msg)
    check(built["Subject"] == msg.subject,
          "the subject is the one-line summary, which is all a phone shows")
    check(built["To"] == "me@example.com" and built["From"] == "me@example.com",
          "From falls back to the SMTP user when MAIL_FROM is unset")
    body = built.get_content()
    check("Реформър група" in body and "Total booked" in body,
          "the body is the full rendered text")
    check("utf-8" in str(built.get_charsets()),
          f"Cyrillic is sent as UTF-8, got {built.get_charsets()}")

    sent: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            sent.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, m):
            sent.append(("send", m["Subject"], m["To"]))

        def starttls(self, context=None):
            sent.append(("starttls",))

    real_ssl, real_plain = smtplib.SMTP_SSL, smtplib.SMTP
    try:
        smtplib.SMTP_SSL = FakeSMTP                                # type: ignore[misc]
        mail.Email("smtp.mail.yahoo.com", 465, "u", "p", "to@x.com").send(msg)
        check([s[0] for s in sent] == ["connect", "login", "send"],
              f"port 465 connects with implicit TLS then logs in, got {sent}")
        check(sent[0][1:] == ("smtp.mail.yahoo.com", 465, 20),
              f"host, port and a timeout are all passed, got {sent[0]}")

        sent.clear()
        smtplib.SMTP = FakeSMTP                                    # type: ignore[misc]
        mail.Email("smtp.example.com", 587, "u", "p", "to@x.com").send(msg)
        check([s[0] for s in sent] == ["connect", "starttls", "login", "send"],
              f"port 587 upgrades with STARTTLS before logging in, got {sent}")

        class RefusingSMTP(FakeSMTP):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")

        smtplib.SMTP_SSL = RefusingSMTP                            # type: ignore[misc]
        try:
            mail.Email("smtp.mail.yahoo.com", 465, "u", "p", "to@x.com").send(msg)
            raise AssertionError("a rejected login must raise")
        except channel.NotifyFailed as exc:
            check("app-specific password" in str(exc) and "535" in str(exc),
                  f"a rejected login names the likely cause, got {exc}")
    finally:
        smtplib.SMTP_SSL, smtplib.SMTP = real_ssl, real_plain

    print("channel.from_env")
    keys = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO", "SMTP_PORT",
            "MAIL_FROM", "VIBER_TOKEN", "VIBER_RECEIVER", "NOTIFY_CHANNEL",
            "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
    before = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        sender, missing = channel.from_env()
        check(sender.name == "console" and "no channel configured" in missing,
              f"nothing configured falls back to the console, got {missing!r}")

        os.environ.update(SMTP_HOST="smtp.x", SMTP_USER="u", SMTP_PASS="p",
                          MAIL_TO="to@x.com")
        sender, missing = channel.from_env()
        check(sender.name == "email" and not missing,
              "SMTP settings alone select e-mail")
        check(getattr(sender, "port") == 465,
              "465 is the default port when SMTP_PORT is unset")

        os.environ["SMTP_PORT"] = "25"
        os.environ["NOTIFY_CHANNEL"] = "email"
        sender, missing = channel.from_env()
        check(sender.name == "console" and "25 is unauthenticated" in missing,
              f"port 25 is refused rather than sent in the clear, got {missing!r}")

        os.environ["SMTP_PORT"] = "587"
        os.environ["NOTIFY_CHANNEL"] = "viber"
        sender, missing = channel.from_env()
        check(sender.name == "console" and "VIBER_TOKEN" in missing,
              "asking for viber without its credentials says which are missing")
        os.environ.update(VIBER_TOKEN="t", VIBER_RECEIVER="r")
        check(channel.from_env()[0].name == "viber",
              "NOTIFY_CHANNEL=viber picks Viber even with e-mail configured")

        os.environ["NOTIFY_CHANNEL"] = "telegram"
        check("TELEGRAM_TOKEN" in channel.from_env()[1],
              "asking for telegram without its credentials says which are missing")
        os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
        check(channel.from_env()[0].name == "telegram",
              "NOTIFY_CHANNEL=telegram picks Telegram")
        del os.environ["NOTIFY_CHANNEL"]
        check(channel.from_env()[0].name == "telegram",
              "with several configured and no NOTIFY_CHANNEL, Telegram wins")
        os.environ["NOTIFY_CHANNEL"] = "signal"
        check("not a channel" in channel.from_env()[1],
              "an unknown channel name is reported, not ignored")
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_test_send_leaves_the_snapshot_alone():
    """--test-send must not create or consume the baseline the watch relies on,
    or running a test would swallow the next real sign-up."""
    print("cli: --test-send does not disturb the watch")
    import argparse

    from notifier import cli, fitsys_api

    TMP.unlink(missing_ok=True)
    state.FileStore(TMP).save({"11": 7})                 # a baseline mid-flight
    reading = watch.Reading(classes=LIVE, unreadable=0, studio_classes=3,
                            studio_instructors=("Фани М",))
    real_client, real_from_env = fitsys_api.client_from_env, channel.from_env
    sent: list = []

    class Capture:
        name = "stub"

        def send(self, message):
            sent.append(message)

    try:
        fitsys_api.client_from_env = lambda **kw: FakeClient(reading)
        channel.from_env = lambda: (Capture(), "")
        args = argparse.Namespace(days=60, instructor="Фани", dry_run=False,
                                  reset=False, state=str(TMP), all=False,
                                  test_send=True, telegram_chat_id=False)
        check(cli.test_send(args) == 0, "a test send returns cleanly")
        check(state.FileStore(TMP).load() == {"11": 7},
              f"the snapshot is untouched by a test, got {state.FileStore(TMP).load()}")
        check(sent and sent[0].subject.startswith("TEST · "),
              f"the message is unmistakably a test, got {sent[0].subject!r}")
        check("not a booking" in sent[0].text,
              "the body says outright that nothing was booked")
        # The bug this test exists for: the first version fabricated a
        # "+1 booked" from a real class, and it was read as a real sign-up.
        first_line = sent[0].text.splitlines()[0]
        check(not first_line.startswith(("+", "-")),
              f"the message does not open by claiming a change, got {first_line!r}")
        check("Currently booked: 16" in sent[0].text,
              "it reports the true current total, not a change")
    finally:
        fitsys_api.client_from_env = real_client
        channel.from_env = real_from_env
        TMP.unlink(missing_ok=True)


def test_self_test_message_claims_nothing_false():
    """Regression: --test-send used to fabricate a `+1 booked` line from a real
    class to demonstrate the format, which read as an actual sign-up."""
    print("watch.self_test_message")
    from datetime import date

    m = watch.self_test_message(LIVE["11"], classes=16, total=3,
                                until=date(2026, 10, 26))
    check("not a booking" in m.text.splitlines()[0],
          f"it opens by saying it is not a booking, got {m.text.splitlines()[0]!r}")
    check("channel works" in m.subject and "booked ·" not in m.subject,
          f"the subject reports the channel, not a change, got {m.subject!r}")
    check("Currently booked: 3" in m.text,
          "the real current total is stated as current, not as a delta")
    check("Watching 16 classes until 26.10.2026" in m.text,
          f"it says what is being watched and how far, got {m.text!r}")

    # The example block is allowed to show the format -- under a heading, and
    # with a count that is plainly hypothetical (booked + 1, not booked).
    check("A real notification would look like:" in m.text,
          "any sample is introduced as an example")
    sample_lines = m.text.split("would look like:")[1].splitlines()[1:]
    check(all(line.startswith("  ") for line in sample_lines if line),
          f"the example is indented away from the real statements, got {sample_lines}")
    check("  9/12 · Фани М" in m.text,
          "the example shows booked+1 (9), never the real 8 as if it were new")

    empty = watch.self_test_message(None, classes=0, total=0,
                                    until=date(2026, 10, 26))
    check("Watching 0 classes" in empty.text
          and "would look like" not in empty.text,
          "with nothing to watch it says so and shows no example")
    check("1 class until" in watch.self_test_message(None, classes=1, total=0,
                                                    until=date(2026, 10, 26)).text,
          "singular 'class' for one")


def test_notify_mode_env_var():
    """The Cloud Run console cannot override a container's arguments per
    execution, so the three modes must also be reachable from a variable."""
    print("cli: NOTIFY_MODE")
    import os

    from notifier import cli

    before = os.environ.get("NOTIFY_MODE")
    calls = []
    real_run, real_test = cli.run, cli.test_send
    try:
        cli.run = lambda a: calls.append(("watch", a.reset)) or 0
        cli.test_send = lambda a: calls.append(("test", None)) or 0

        for value, expected in (("", ("watch", False)), ("watch", ("watch", False)),
                                ("test", ("test", None)), ("reset", ("watch", True))):
            calls.clear()
            os.environ["NOTIFY_MODE"] = value
            check(cli.main(["--instructor", "Фани"]) == 0
                  and calls == [expected],
                  f"NOTIFY_MODE={value!r} -> {expected[0]}"
                  + (" with --reset" if expected[1] else "")
                  + f", got {calls}")

        os.environ["NOTIFY_MODE"] = "nonsense"
        try:
            cli.main(["--instructor", "Фани"])
            raise AssertionError("an unknown mode must be refused")
        except SystemExit as exc:
            check("not one of: watch, test, reset" in str(exc),
                  f"an unknown mode lists the valid ones, got {exc}")
    finally:
        cli.run, cli.test_send = real_run, real_test
        if before is None:
            os.environ.pop("NOTIFY_MODE", None)
        else:
            os.environ["NOTIFY_MODE"] = before


def test_failed_send_keeps_the_snapshot():
    """The claim that a failed send is retried next run, actually exercised:
    cli.run must not advance the snapshot when the message did not go out."""
    print("cli: a failed send does not lose the change")
    import argparse

    from notifier import channel, cli, fitsys_api

    TMP.unlink(missing_ok=True)
    state.FileStore(TMP).save({"11": 7, "12": 5, "13": 3})      # 11 is about to become 8

    reading = watch.Reading(classes=LIVE, unreadable=0, studio_classes=3,
                            studio_instructors=("Фани М",))
    real_client, real_from_env = fitsys_api.client_from_env, channel.from_env

    class Failing:
        name = "viber"

        def send(self, text):
            raise channel.NotifyFailed("Viber refused it: status=3")

    try:
        fitsys_api.client_from_env = lambda **kw: FakeClient(reading)
        channel.from_env = lambda: (Failing(), "")
        args = argparse.Namespace(days=14, instructor="Фани", dry_run=False,
                                  reset=False, state=str(TMP), all=False)
        try:
            cli.run(args)
            raise AssertionError("a failed send should stop the run")
        except SystemExit as exc:
            check("Snapshot unchanged" in str(exc),
                  f"it says the change will be reported again, got {exc}")
        check(state.FileStore(TMP).load() == {"11": 7, "12": 5, "13": 3},
              f"the snapshot is untouched, got {state.FileStore(TMP).load()}")

        # ...and a successful send does advance it.
        sent: list[str] = []

        class Working:
            name = "viber"

            def send(self, text):
                sent.append(text)

        channel.from_env = lambda: (Working(), "")
        check(cli.run(args) == 0, "a successful send returns cleanly")
        check(sent and "+1 booked" in sent[0].text,
              f"the message that went out is the rendered one, got {sent!r}")
        check(state.FileStore(TMP).load()["11"] == 8,
              "the snapshot advances only after the message is out")
    finally:
        fitsys_api.client_from_env = real_client
        channel.from_env = real_from_env
        TMP.unlink(missing_ok=True)


def test_parsing_real_feed_rows():
    """The parser, against rows shaped exactly like the live feed."""
    from notifier import fitsys_api

    print("fitsys_api.parse_rows")
    site = fitsys_api.load_site()

    def row(eid, day, hhmm, title, who, booked, cap=12, typ="class",
            room="зала 1"):
        return {"id": eid, "title": title, "type": typ, "resource_title": room,
                "instructor_names": who, "people": cap,
                "current_people": booked,
                "start": f"2026-09-{day:02d} {hhmm}:00",
                "end": f"2026-09-{day:02d} {hhmm[:2]}:55:00",
                "customer_names": "SHOULD NOT BE READ",
                "customer_ids": "SHOULD NOT BE READ",
                "last_comment": "SHOULD NOT BE READ"}

    rows = [
        row(11, 2, "09:00", "Реформър група", "2581_Фани М", 8),
        row(12, 3, "18:30", "Хатха йога", "238_Снежана", 5),
        row(13, 4, "09:00", "Реформър група", "2581_Фани М", 3),
        row(14, 1, "09:00", "Реформър група", "2581_Фани М", 9),   # in the past
        row(15, 3, "14:00", "Масаж", "2191_Даря Б", 1, typ="reservation"),
        row(16, 3, "20:00", "Наем", "0_Външен", 0, typ="event"),
        row(17, 5, "10:00", "Пилатес", "2581_Фани М", None),       # no count
        row(18, 4, "11:00", "Body Ballet", "77_Ани М", 4),         # not Фани
    ]

    r = fitsys_api.parse_rows(rows, site, since=NOW)
    now = r.classes
    check(set(now) == {"11", "12", "13", "18"},
          f"reservations, room rentals and past classes excluded, got "
          f"{sorted(now)}")
    check(r.unreadable == 1,
          "a row with no readable booking count is counted, not dropped")
    check(now["11"].fullness() == "8/12",
          "current_people and people map to booked and capacity")
    check(now["11"].instructor == "Фани М",
          f"the instructor id prefix is stripped, got {now['11'].instructor!r}")
    check(now["11"].room == "зала 1" and now["11"].class_type == "Реформър група",
          "room and class type come through")
    check(r.studio_instructors == ("Ани М", "Снежана", "Фани М"),
          f"instructors come from classes only, got {r.studio_instructors}")

    mine = fitsys_api.parse_rows(rows, site, since=NOW,
                                 instructor="Фани Малинова")
    check(set(mine.classes) == {"11", "13"},
          f"the full name filters to her classes, got {sorted(mine.classes)}")
    check("18" not in mine.classes,
          "'Ани М' is not caught by a filter for 'Фани Малинова'")
    check(mine.studio_classes == 6,
          f"the unfiltered count survives filtering, got {mine.studio_classes}")
    check(not fitsys_api.parse_rows(rows, site, since=NOW,
                                    instructor="Мария Иванова").classes,
          "a name nobody uses yields no classes")
    check("14" in fitsys_api.parse_rows(rows, site).classes,
          "without `since`, past classes are kept (pure)")

    for value, hour in (("2026-09-02T09:00:00+03:00", 9),
                        ("2026-09-02 09:00", 9), ("02.09.2026 09:00", 9)):
        parsed = fitsys_api._parse_dt(value)
        check(parsed is not None and parsed.hour == hour,
              f"{value!r} parses to 09:00, got {parsed}")
    check(fitsys_api._parse_dt("not a date") is None,
          "an unparseable start is None, not a crash")

    msg = watch.render(watch.diff({"11": 7}, now), total=16, classes=3)
    check("SHOULD NOT BE READ" not in msg.text + msg.subject,
          "no client field can reach the message even when the feed carries it")


def test_site_json_describes_reads_only():
    """This app's own site record must describe reads only: there is no write
    code here to route, and none should be invited in."""
    from notifier import fitsys_api

    print("site.json")
    site = fitsys_api.load_site()
    check(site["calendar"]["events_endpoint"]["url"]
          == "/calendar/get-events-basic",
          "the feed used is the one that carries no customer data")
    check(site["calendar"]["events_endpoint"]["method"] == "GET",
          "and it is read with GET")

    keys = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                keys.append(k.lower())
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(site)
    for forbidden in ("write", "create", "edit", "cancel", "delete"):
        check(forbidden not in keys,
              f"no {forbidden!r} section exists in the map")


def test_site_json_matches_the_scheduler_when_present():
    """The scheduler keeps its own, richer map. This app is standalone, so the
    two are separate copies -- which is the accepted cost of the separation, and
    means they can drift. There is no runtime dependency, but when both happen
    to be on one machine, compare them. Skipped in the container.
    """
    import json
    import os

    from notifier import fitsys_api

    print("site.json vs the scheduler's map (development only)")
    home = os.getenv("FITSYS_HOME", "")
    root = Path(home) if home else Path(__file__).resolve().parent.parent.parent / "FitSys"
    theirs_file = root / "fitsys" / "site_map.json"
    if not theirs_file.is_file():
        print(f"  skip  no scheduler at {root} -- nothing to compare against")
        return

    ours = fitsys_api.load_site()
    theirs = json.loads(theirs_file.read_text(encoding="utf-8"))
    check(ours["base_url"] == theirs["base_url"],
          f"base_url agrees ({ours['base_url']})")
    check(ours["login"]["url"] == theirs["login"]["url"]
          and ours["login"]["fields"] == theirs["login"]["fields"],
          "the login route and its form fields agree")
    oc, tc = ours["calendar"], theirs["calendar"]
    check(oc["page_url"] == tc["page_url"], "the calendar page agrees")
    check(oc["events_endpoint"]["url"] == tc["events_endpoint"]["url"],
          "the events endpoint agrees")
    check(oc["include_types"] == tc["include_types"],
          "the event types counted as classes agree")
    check(oc["instructor_pattern"]["strip"] == tc["instructor_pattern"]["strip"],
          "the instructor-name pattern agrees")
    differing = {k for k in set(oc["event_fields"]) & set(tc["event_fields"])
                 if k != "status" and oc["event_fields"][k] != tc["event_fields"][k]}
    check(not differing,
          f"every field name both map agrees; differing={sorted(differing)}")


def test_viber_request_shape():
    """What actually goes to Viber. Viber signals refusal in a 200 body rather
    than an HTTP status, which is worth pinning down."""
    print("viber: the outbound request")
    real = viber.requests
    try:
        fake = FakeRequests(body={"status": 0, "message_token": 123})
        viber.requests = fake
        viber.Viber("tok-abc", "user-xyz", "BalanSense").send(MSG)
        call = fake.calls[0]
        check(call["url"] == "https://chatapi.viber.com/pa/send_message",
              "posts to /pa/send_message")
        check(call["headers"].get("X-Viber-Auth-Token") == "tok-abc",
              "the bot token travels in X-Viber-Auth-Token, not the body")
        check(call["json"].get("receiver") == "user-xyz"
              and call["json"].get("type") == "text",
              f"receiver and type are set, got {call['json']!r}")
        check(call["json"].get("sender", {}).get("name") == "BalanSense",
              "the sender name is nested under sender.name as Viber wants")
        check(call["timeout"] == 20, "the call cannot hang a run for ever")

        viber.requests = FakeRequests(body={"status": 3,
                                            "status_message": "invalid auth token"})
        try:
            viber.Viber("bad", "user-xyz").send(MSG)
            raise AssertionError("a refusal in a 200 body must raise")
        except channel.NotifyFailed as exc:
            check("status=3" in str(exc) and "invalid auth token" in str(exc),
                  f"a 200-with-status-3 refusal raises and quotes Viber, got {exc}")

        viber.requests = FakeRequests(body=None, status_code=502, text="<html>bad")
        try:
            viber.Viber("tok", "user").send(MSG)
            raise AssertionError("a non-JSON reply must raise")
        except channel.NotifyFailed as exc:
            check("502" in str(exc), f"a non-JSON reply names the status, got {exc}")

        viber.requests = FakeRequests(
            raises=real.RequestException("connection reset"))
        try:
            viber.Viber("tok", "user").send(MSG)
            raise AssertionError("a network failure must raise NotifyFailed")
        except channel.NotifyFailed as exc:
            check("unreachable" in str(exc),
                  f"a network failure becomes NotifyFailed, got {exc}")
    finally:
        viber.requests = real


def test_no_write_path():
    """This app must have no way to change the studio calendar. Checked against
    the parsed syntax tree, not the text, so prose in a docstring does not read
    as a call."""
    print("read-only by construction")
    import ast
    import inspect

    from notifier import fitsys_api

    WRITES = {"apply", "_post_form", "_create", "_edit", "_cancel", "put",
              "patch", "delete"}
    for mod in (fitsys_api, watch, state, viber, mail, channel, telegram_mod()):
        called = {node.func.attr
                  for node in ast.walk(ast.parse(inspect.getsource(mod)))
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        clash = sorted(called & WRITES)
        name = mod.__name__.split(".")[-1]
        check(not clash, f"{name}.py calls nothing that writes to FitSys"
                         + (f" -- found {clash}" if clash else ""))

    # The only POST anywhere is the login form. There is no write endpoint.
    src = inspect.getsource(fitsys_api)
    posts = src.count(".post(")
    check(posts == 1, f"exactly one POST exists in the client, got {posts}")
    login_line = [l for l in src.splitlines() if ".post(" in l][0]
    check("cfg[\"url\"]" in login_line,
          f"and it is the login form, got {login_line.strip()!r}")
    check("get-events-basic" in fitsys_api.SITE_FILE.read_text(encoding="utf-8"),
          "the one data route is the read-only feed")


def telegram_mod():
    from notifier import telegram
    return telegram


def main() -> int:
    test_diff()
    test_message()
    test_state()
    test_state_backend_selection()
    test_gcs_store_guards_against_overlap()
    test_instructor_names()
    test_parsing_real_feed_rows()
    test_site_json_describes_reads_only()
    test_site_json_matches_the_scheduler_when_present()
    test_viber_request_shape()
    test_telegram()
    test_email()
    test_self_test_message_claims_nothing_false()
    test_test_send_leaves_the_snapshot_alone()
    test_notify_mode_env_var()
    test_failed_send_keeps_the_snapshot()
    test_no_write_path()
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
