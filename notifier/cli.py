"""BalanSenseNotify -- a message when someone signs up for a class.

    .venv/bin/python -m notifier.cli --telegram-chat-id  # setup, once
    .venv/bin/python -m notifier.cli --test-send     # prove delivery works
    .venv/bin/python -m notifier.cli --dry-run       # print, never send
    .venv/bin/python -m notifier.cli --reset         # re-baseline, send nothing
    .venv/bin/python -m notifier.cli                 # what cron runs

One shot per invocation; cron or Cloud Scheduler sets the cadence. Reads the
FitSys calendar only -- there is no write code anywhere in this app.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import channel, state, watch

APP_ROOT = Path(__file__).resolve().parent.parent


def show_telegram_chat_id() -> int:
    """Setup helper: print the chat ids that have written to the bot."""
    from .telegram import chat_ids

    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_TOKEN in .env first -- @BotFather gives it to "
                 "you when you run /newbot.")
    found = chat_ids(token)
    if not found:
        sys.exit("No chats yet. Open Telegram, find your bot, and send it any "
                 "message (/start will do) -- that is also what allows the bot "
                 "to write back to you. Then run this again.\n"
                 "(Telegram only keeps unread updates for 24 hours.)")
    print("Chats that have messaged this bot:\n")
    for chat in found:
        print(f"  TELEGRAM_CHAT_ID={chat['id']:<16} {chat['type']:<10} "
              f"{chat['who']}")
    print("\nPut the right one in .env, with NOTIFY_CHANNEL=telegram.")
    return 0


def test_send(args) -> int:
    """Send one real message now, built from a real upcoming class, so delivery
    is proved before a booking ever depends on it.

    Deliberately does not touch the snapshot: a test must not consume or create
    the baseline that the watch itself relies on.
    """
    from . import fitsys_api

    sender, missing = channel.from_env()
    if missing:
        sys.exit(f"No channel configured ({missing}) -- nothing to test. "
                 f"See .env.example.")

    start = date.today()
    end = start + timedelta(days=max(1, args.days) - 1)
    client = fitsys_api.client_from_env()
    reading = client.read(start, end, instructor=args.instructor,
                          since=datetime.now())
    if not reading.classes:
        # Still worth sending: it proves the channel, and "watching 0 classes"
        # is itself something worth knowing.
        sender.send(watch.self_test_message(None, classes=0, total=0, until=end))
        print(f"Sent, but there is no upcoming class for "
              f"{args.instructor or 'the studio'} in the next {args.days} days.")
        return 0

    soonest = min(reading.classes.values(), key=lambda o: o.when)
    # Says only what is true now. It must not invent a sign-up, even a labelled
    # one -- a fabricated "+1 booked" reads as a real booking.
    message = watch.self_test_message(
        soonest, classes=len(reading.classes),
        total=watch.total_booked(reading.classes), until=end)

    print(f"Sending a test via {sender.name}")
    sender.send(message)
    print("Sent. If it did not arrive, the channel is misconfigured -- the "
          "send itself reported success.")
    return 0


def run(args) -> int:
    from . import fitsys_api               # reads .env, so import after loading it

    start = date.today()
    end = start + timedelta(days=max(1, args.days) - 1)
    client = fitsys_api.client_from_env()
    reading = client.read(start, end, instructor=args.instructor,
                          since=datetime.now())
    now = reading.classes

    # A name the calendar does not use would leave this quiet for ever, which is
    # the one failure a notifier must never have. Note the difference between
    # "nobody is called that" and "she has nothing on this fortnight".
    if args.instructor and not any(
            watch.instructor_matches(args.instructor, name)
            for name in reading.studio_instructors):
        sys.exit(f"Nobody in the calendar matches {args.instructor!r}.\n"
                 f"FitSys abbreviates surnames, so check the given name's "
                 f"spelling. Teaching in the next {args.days} day(s):\n  "
                 + "\n  ".join(reading.studio_instructors or ["(nobody)"]))

    total = watch.total_booked(now)
    who = f" for {args.instructor}" if args.instructor else " (whole studio)"
    print(f"{len(now)} upcoming class(es){who} {start}..{end} via "
          f"{client.source}; "
          f"{total} booking(s) in total")
    if reading.unreadable:
        print(f"  ({reading.unreadable} row(s) carried no readable booking "
              f"count -- check calendar.event_fields.booked in "
              f"notifier/site.json)", file=sys.stderr)

    store = state.from_env(args.state)
    previous = store.load()
    counts = {rid: occ.booked for rid, occ in now.items()}

    if args.reset or not previous:
        store.save(counts)
        print(f"Baseline saved ({len(counts)} classes) in {store}. "
              "Nothing sent; the next run reports against it.")
        return 0

    changes = watch.diff(previous, now)
    if not changes:
        store.save(counts)                  # prunes ids that left the window
        print("No change since the last look.")
        return 0

    for c in changes:
        print(f"  {c.kind:<8} {c.was} -> {c.occ.booked}  {c.occ.label()}")

    sender: channel.Sender = channel.Console()
    if not args.dry_run:
        sender, missing = channel.from_env()
        if missing:
            print(f"Not sending ({missing}); printing instead.",
                  file=sys.stderr)
    try:
        sender.send(watch.render(changes, total=total, classes=len(now)))
    except channel.NotifyFailed as exc:
        # Leave the snapshot alone: the next run must report this again rather
        # than a failed send swallowing the only notice of it.
        sys.exit(f"Could not notify: {exc}\nSnapshot unchanged; the next run "
                 f"will report the same change.")
    store.save(counts)
    return 0


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(APP_ROOT / ".env")
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        prog="notifier", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=int(os.getenv("NOTIFY_DAYS", "14")),
                   help="how far ahead to watch (default 14)")
    p.add_argument("--instructor", default=os.getenv("NOTIFY_INSTRUCTOR", ""),
                   help="whose classes to watch, as a full name; FitSys's "
                        "abbreviated spelling is matched for you")
    p.add_argument("--all", action="store_true",
                   help="watch the whole studio instead of one instructor")
    p.add_argument("--dry-run", action="store_true",
                   help="print the message instead of sending it")
    p.add_argument("--reset", action="store_true",
                   help="re-baseline: store today's counts, send nothing "
                        "(or set NOTIFY_MODE=reset)")
    p.add_argument("--state", default=None,
                   help="where the snapshot lives: a path, or a gs:// URI for "
                        "Cloud Run (default STATE_URI, else var/watch_state.json)")
    p.add_argument("--test-send", action="store_true",
                   help="send one real message now, to prove delivery works "
                        "(or set NOTIFY_MODE=test)")
    p.add_argument("--telegram-chat-id", action="store_true",
                   help="setup: print the chat ids that have messaged the bot")
    args = p.parse_args(argv)

    # NOTIFY_MODE is the same three choices as the flags, in the environment.
    # The Cloud Run console has no per-execution argument override, so on that
    # path the only way to run a one-off test is to edit a variable and press
    # Execute -- which this makes possible without touching the flags.
    mode = os.getenv("NOTIFY_MODE", "").strip().lower()
    if mode not in ("", "watch", "test", "reset"):
        sys.exit(f"NOTIFY_MODE={mode!r} is not one of: watch, test, reset.")
    args.test_send = args.test_send or mode == "test"
    args.reset = args.reset or mode == "reset"

    if args.telegram_chat_id:      # a setup step, not a watch run
        try:
            return show_telegram_chat_id()
        except RuntimeError as exc:
            sys.exit(str(exc))

    if args.all:
        args.instructor = ""
    elif not args.instructor:
        # Watching the whole studio is ~300 classes a month, so it has to be
        # asked for rather than happen because a setting was forgotten.
        sys.exit("Whose classes? Set NOTIFY_INSTRUCTOR in .env or pass "
                 "--instructor \"Име Фамилия\" (or --all for the whole studio).")

    try:
        return test_send(args) if args.test_send else run(args)
    except state.StateConflict as exc:
        # Not a failure worth alarming anyone: two executions overlapped and
        # this one lost. Exit 0 so a scheduler does not count it as an error.
        print(str(exc), file=sys.stderr)
        return 0
    except RuntimeError as exc:
        # SchedulerNotFound, NotSignedIn and the scheduler's own read failures
        # all carry a message written to be acted on -- print it, not a stack.
        sys.exit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
