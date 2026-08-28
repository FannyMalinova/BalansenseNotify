"""What changed in the bookings since the last look.

Pure: no clock, no network, no filesystem, and no import of the scheduler. The
whole reporting decision -- what counts as a sign-up, what gets said -- is
therefore testable offline with no FitSys account.

The feed this app reads, `calendar/get-events-basic`, carries `current_people`
and `people` but no customer names or ids at all, so a sign-up is reported
without ever holding client data. That is also the limit of what it can say: a
count shows the *net* change, so one client signing out while another signs up
inside the same interval cancels out and goes unreported. Naming who signed up
would mean the scheduler's other feed and a client's name sent to Viber.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# Message text is English. Class types, rooms and instructor names are NOT
# translated -- they are data, and they must read exactly as they do in FitSys
# so a notification can be matched against the calendar at a glance.
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]


@dataclass(frozen=True)
class Occupancy:
    """One class as the feed sees it right now: when it is, and how full."""

    remote_id: str
    when: datetime
    class_type: str
    instructor: str
    room: str = ""
    booked: int = 0
    capacity: int | None = None

    def label(self) -> str:
        return (f"{self.class_type} · {WEEKDAYS[self.when.weekday()]} "
                f"{self.when.day:02d}.{self.when.month:02d} "
                f"{self.when.hour:02d}:{self.when.minute:02d}"
                + (f" · {self.room}" if self.room else ""))

    def fullness(self) -> str:
        return (f"{self.booked}/{self.capacity}" if self.capacity
                else str(self.booked))


@dataclass(frozen=True)
class Reading:
    """One look at the calendar.

    `studio_*` describe everything read *before* the instructor filter, so the
    caller can tell "this name matches nobody" (a configuration mistake worth
    shouting about) from "this person simply has no classes this fortnight".
    """

    classes: dict[str, Occupancy]
    unreadable: int = 0
    studio_classes: int = 0
    studio_instructors: tuple[str, ...] = ()


def _tokens(name: str) -> list[str]:
    return [t for t in str(name or "").replace(" ", " ").split() if t]


def instructor_matches(configured: str, feed_name: str) -> bool:
    """Does `feed_name`, as FitSys spells it, name the person in `configured`?

    FitSys abbreviates surnames: the calendar says `Фани М` where a human says
    `Фани Малинова`. A substring test on the full name therefore matches nothing
    and the watch goes quiet forever, which is the worst way for a notifier to
    fail. Tokens are compared instead -- the given names must be equal, and each
    further token in the feed must be a prefix of the corresponding configured
    one. That is what keeps the studio's `Здравка Д` and `Здравка М` apart, and
    what stops a substring match on `Ани М` from also catching `Фани М`.

    A configured name shorter than the feed's still matches, so `Фани` alone
    works too.
    """
    want, got = _tokens(configured.lower()), _tokens(feed_name.lower())
    if not want or not got or want[0] != got[0]:
        return False
    return all(mine.startswith(theirs)
               for theirs, mine in zip(got[1:], want[1:]))


@dataclass(frozen=True)
class Change:
    """A class whose booking count moved since the previous look."""

    occ: Occupancy
    was: int

    @property
    def delta(self) -> int:
        return self.occ.booked - self.was

    @property
    def kind(self) -> str:
        return "signup" if self.delta > 0 else "signout"


def diff(previous: dict[str, int], now: dict[str, Occupancy]) -> list[Change]:
    """Classes whose count moved, oldest class first.

    A class missing from `previous` is new to the watch -- just created, or it
    has entered the window -- and is not reported as a sign-up: only a change
    against a count we actually saw is a booking event. Without that rule every
    newly created class would fire a false notice.
    """
    return sorted((Change(occ, previous[rid]) for rid, occ in now.items()
                   if rid in previous and previous[rid] != occ.booked),
                  key=lambda c: c.occ.when)


def total_booked(now: dict[str, Occupancy]) -> int:
    return sum(occ.booked for occ in now.values())


def _phrase(delta: int) -> str:
    n = abs(delta)
    if delta > 0:
        return f"+{n} booked"
    return f"-{n} cancelled"


@dataclass(frozen=True)
class Message:
    """What gets delivered. The subject exists because e-mail has one and it is
    all a phone shows on the lock screen -- so the change has to fit in it, not
    just in the body. Channels without subjects ignore it."""

    subject: str
    text: str


def _subject(changes: list[Change], total: int) -> str:
    if len(changes) == 1:
        c = changes[0]
        return f"{_phrase(c.delta)} · {c.occ.label()} · {c.occ.fullness()}"
    ups = sum(c.delta for c in changes if c.delta > 0)
    downs = -sum(c.delta for c in changes if c.delta < 0)
    moved = " ".join(p for p in (f"+{ups}" if ups else "",
                                 f"-{downs}" if downs else "") if p)
    return (f"{moved} · {len(changes)} "
            + ("class" if len(changes) == 1 else "classes")
            + f" · {total} booked total")


def render(changes: list[Change], *, total: int, classes: int) -> Message:
    """The notification."""
    blocks = [f"{_phrase(c.delta)}\n{c.occ.label()}\n"
              + f"{c.occ.fullness()} · {c.occ.instructor}".rstrip(" ·")
              for c in changes]
    text = ("\n\n".join(blocks)
            + f"\n\nTotal booked: {total} in {classes} "
            + ("class" if classes == 1 else "classes"))
    return Message(subject=_subject(changes, total), text=text)


def self_test_message(sample: Occupancy | None, *, classes: int, total: int,
                      until: date) -> Message:
    """A message that proves the channel works without claiming anything false.

    The first version of this fabricated a `+1 booked` line from a real class
    in order to show the format -- and it was read, correctly, as a claim that
    someone had just signed up. Inventing an event is precisely the confusion a
    notifier must never create, however it is labelled. So this states only
    things that are true right now, and shows the shape of a real notice under
    an explicit heading, indented, as an example.
    """
    lines = ["This is a test, not a booking.", "",
             f"The channel works. Watching {classes} "
             + ("class" if classes == 1 else "classes")
             + f" until {until.strftime('%d.%m.%Y')}.",
             f"Currently booked: {total}."]
    if sample is not None:
        would_be = sample.booked + 1
        cap = f"/{sample.capacity}" if sample.capacity else ""
        lines += ["", "A real notification would look like:",
                  "  +1 booked",
                  f"  {sample.label()}",
                  f"  {would_be}{cap} · {sample.instructor}"]
    return Message(subject=f"TEST · channel works · watching {classes} "
                           + ("class" if classes == 1 else "classes"),
                   text="\n".join(lines))
