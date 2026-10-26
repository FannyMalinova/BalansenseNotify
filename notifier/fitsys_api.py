"""A minimal, read-only client for balansense.fitsys.com.

This app is standalone: it does not import the FitSys scheduler. That means the
site knowledge here is a second copy of what the scheduler also knows, which is
a real cost -- a route change has to be fixed in two places. It buys complete
separation, which is what the deployment needs: the container carries this app
and nothing else, and there is no code path anywhere in the image that can write
to the studio calendar.

Read-only is structural, not configured. There is no POST helper, no create,
edit or cancel, and the one endpoint used is GET-only. `site.json` records no
write routes because there is nothing here to route.

What it does:
  * signs in with a username and password (unattended runs), through the
    Sanctum SPA flow FitSys now uses: ask `/sanctum/csrf-cookie`, POST JSON to
    `/api/login`, then read `/api/context` for the siteId the feed needs;
  * reads `/calendar/get-events-basic` in weekly slices -- a four-week request
    died mid-response against the live site, weekly ones did not;
  * parses one feed row into an Occupancy.

The login used to be an HTML form: GET `/users/login`, scrape a hidden `_token`,
POST the three fields back. That page is now a Vue mount point with no form and
no inputs in it at all, so the scrape found nothing and every run failed with
"No _token on the login page". The feed itself did not change -- same route,
same field names.

`/calendar/get-events-basic` was chosen over the sibling `/calendar/get-events`
because it carried counts and no identities. It no longer does: it now returns
`customer_names` and `customer_ids`, populated. The parser never read them, but
"the endpoint does not send identities" has stopped being true, so `_slice`
deletes them from every row as it reads it. Counts-only is now something this
file enforces, not something it inherits.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .watch import Occupancy, Reading, instructor_matches

SITE_FILE = Path(__file__).resolve().parent / "site.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class NotSignedIn(RuntimeError):
    """No usable session: wrong credentials, or the session expired."""


class ReadFailed(RuntimeError):
    """The calendar could not be read. Carries what was tried."""


def load_site(path: Path | None = None) -> dict:
    return json.loads((path or SITE_FILE).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Parsing one feed row. Pure -- no session needed, so it is testable offline.

def _first(row: dict, names: list[str]):
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        for source, key in ((row, name), (lowered, name.lower())):
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _as_int(value) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().replace("T", " ").replace("Z", "")
    text = re.sub(r"[+-]\d\d:?\d\d$", "", text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def clean_instructor(value, site: dict) -> str:
    """FitSys returns instructors as '2581_Фани М' -- id, underscore, name."""
    strip = site["calendar"].get("instructor_pattern", {}).get("strip")
    parts = [p.strip() for p in re.split(r"[,;|]", str(value or "")) if p.strip()]
    out = []
    for part in parts:
        m = re.match(strip, part) if strip else None
        out.append((m.group(1) if m else part).strip())
    return ", ".join(out)


def parse_rows(rows: list, site: dict, *, instructor: str = "",
               since: datetime | None = None) -> Reading:
    """Feed rows -> booking counts by event id.

    Also reports what was seen *before* the instructor filter, so a caller can
    tell "nobody is called that" from "she has nothing on this fortnight".
    """
    fields = site["calendar"]["event_fields"]
    include = [t.lower() for t in (site["calendar"].get("include_types") or [])]
    want = instructor.strip()
    out: dict[str, Occupancy] = {}
    unreadable, studio_classes = 0, 0
    everyone: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(_first(row, fields.get("type", [])) or "").lower()
        if include and kind not in include:
            continue                          # room rentals, single bookings
        start = _parse_dt(_first(row, fields["start"]))
        rid = _first(row, fields["id"])
        if start is None or rid is None:
            continue                          # nothing to key or place it on
        who = clean_instructor(_first(row, fields["instructor"]) or "", site)
        studio_classes += 1
        if who:
            everyone.add(who)
        if want and not instructor_matches(want, who):
            continue
        if since is not None and start < since:
            continue                          # a past class cannot be acted on
        booked = _as_int(_first(row, fields["booked"]))
        if booked is None:
            unreadable += 1                   # a mapping problem, not silence
            continue
        out[str(rid)] = Occupancy(
            remote_id=str(rid), when=start,
            class_type=str(_first(row, fields["class_type"])
                           or _first(row, fields["title"]) or "class").strip(),
            instructor=who,
            room=str(_first(row, fields["room"]) or "").strip(),
            booked=booked,
            capacity=_as_int(_first(row, fields["capacity"])))
    return Reading(classes=out, unreadable=unreadable,
                   studio_classes=studio_classes,
                   studio_instructors=tuple(sorted(everyone)))


# --------------------------------------------------------------------------- #

class Client:
    """A signed-in, read-only session. Reads; there is nothing else it can do."""

    def __init__(self, site: dict | None = None, timeout: int = 30) -> None:
        self.site = site or load_site()
        self.base = self.site["base_url"].rstrip("/")
        self.timeout = timeout
        self._site_id: str | None = None
        self.s = requests.Session()
        # Every route this app touches now answers JSON, and Laravel decides
        # between a JSON body and an HTML redirect on these two headers.
        self.s.headers.update({"User-Agent": UA,
                               "Accept": "application/json",
                               "X-Requested-With": "XMLHttpRequest",
                               "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8"})
        # A long read once died with IncompleteRead; retry the transport too.
        self.s.mount("https://", HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=0.6, status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]))))

    def url(self, path: str) -> str:
        return self.base + "/" + path.lstrip("/")

    # -- auth --------------------------------------------------------------- #
    def _context(self) -> dict:
        """`/api/context` -- who the session is, and which site it is looking
        at. Raises rather than returning a default: a wrong siteId would read
        somebody else's calendar and report it as this studio's.
        """
        cfg = self.site["login"]
        try:
            r = self.s.get(self.url(cfg["context_url"]), timeout=self.timeout)
        except requests.RequestException as exc:
            raise ReadFailed(f'GET {cfg["context_url"]} failed: '
                             f'{type(exc).__name__}: {exc}') from exc
        if r.status_code in (401, 403, 419):
            raise NotSignedIn("FitSys says this session is not signed in.")
        if not r.ok:
            raise ReadFailed(f'GET {cfg["context_url"]} -> HTTP {r.status_code}')
        try:
            ctx = r.json()
        except ValueError:
            raise NotSignedIn(f'GET {cfg["context_url"]} returned HTTP 200 but '
                              f'not JSON -- the session has probably expired.'
                              ) from None
        if not isinstance(ctx, dict):
            raise ReadFailed(f'GET {cfg["context_url"]} returned '
                             f'{type(ctx).__name__}, not an object.')
        return ctx

    def is_signed_in(self) -> bool:
        """One cheap request: `/api/context` is 200 JSON with a session and 401
        without one."""
        try:
            return bool(self._context().get("user_id"))
        except (NotSignedIn, ReadFailed):
            return False

    def sign_in(self, user: str, password: str) -> None:
        """Sanctum's SPA flow, which is what the FitSys front end does.

        The old HTML form is gone, so there is no hidden `_token` to scrape and
        no form fields to post; credentials go as JSON instead.
        """
        cfg = self.site["login"]
        f = cfg["fields"]
        # The SPA asks for the XSRF cookie first, so do the same. On the live
        # site this answers 401 and sets no cookie, and the login below still
        # succeeds on the session cookie alone -- so it must not be fatal.
        try:
            self.s.get(self.url(cfg["csrf_cookie_url"]), timeout=self.timeout)
        except requests.RequestException:
            pass
        headers = {}
        xsrf = urllib.parse.unquote(self.s.cookies.get("XSRF-TOKEN") or "")
        if xsrf:                     # only when Sanctum actually issued one
            headers["X-XSRF-TOKEN"] = xsrf
        r = self.s.post(self.url(cfg["url"]), timeout=self.timeout,
                        headers=headers,
                        json={f["email"]: user, f["password"]: password,
                              f["user_id"]: None})
        if not r.ok:
            # A rejection is 422 {"message": "Invalid credentials"} -- quote it
            # rather than guessing at Bulgarian error text in an HTML page.
            said = ""
            try:
                body = r.json()
                if isinstance(body, dict) and body.get("message"):
                    said = f': {body["message"]}'
            except ValueError:
                pass
            raise NotSignedIn(f"FitSys refused the sign-in (HTTP "
                              f"{r.status_code}{said}). Check FITSYS_USER and "
                              f"FITSYS_PASS.")
        self._site_id = None
        if not self.is_signed_in():
            raise NotSignedIn("FitSys accepted the sign-in but the session does "
                              "not read as signed in. Check "
                              "login.context_url in notifier/site.json.")

    def _resolve_site_id(self) -> str:
        """Which site the feed is asked about. Pinned in site.json, or taken
        from the context. The calendar page's bootstrap JSON used to carry it
        and no longer does."""
        if self._site_id:
            return self._site_id
        pinned = self.site["calendar"].get("site_id")
        if pinned:
            self._site_id = str(pinned)
            return self._site_id
        sid = self._context().get("siteId")
        if sid in (None, ""):
            raise ReadFailed(
                "No siteId in the context response -- cannot tell which site "
                "to read. Pin calendar.site_id in notifier/site.json.")
        self._site_id = str(sid)
        return self._site_id

    # -- read --------------------------------------------------------------- #
    @staticmethod
    def _rows(payload) -> list | None:
        """None means 'no event list here' (a failure); [] means a quiet week."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("events", "data", "result", "items", "rows"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return None

    def _slice(self, start: date, end: date) -> list:
        ep = self.site["calendar"]["events_endpoint"]
        site_id = self._resolve_site_id()
        fmt = ep["date_format"]
        params = {k: (v.replace("{start}", start.strftime(fmt))
                       .replace("{end}", end.strftime(fmt))
                       .replace("{site_id}", site_id))
                  for k, v in ep["params"].items()}
        try:
            r = self.s.get(self.url(ep["url"]), params=params,
                           timeout=self.timeout,
                           headers={"Referer": self.url(
                               self.site["calendar"]["page_url"])})
        except requests.RequestException as exc:
            raise ReadFailed(f"GET {ep['url']} [{start}..{end}] failed: "
                             f"{type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403, 419):
            raise NotSignedIn("FitSys rejected the read -- the session expired.")
        if not r.ok:
            raise ReadFailed(f"GET {ep['url']} [{start}..{end}] -> HTTP "
                             f"{r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise ReadFailed(f"GET {ep['url']} returned HTTP 200 but not JSON "
                             f"-- probably a login page.") from None
        rows = self._rows(payload)
        if rows is None:
            raise ReadFailed(
                f"GET {ep['url']} returned JSON with no event list; keys were "
                f"{sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}. "
                f"Check calendar.events_endpoint in notifier/site.json.")
        # The identities the feed now sends stop here, at the edge, before any
        # row is stored, parsed or logged. Nothing downstream has to be trusted
        # to leave them alone.
        for row in rows:
            if isinstance(row, dict):
                for name in ep.get("drop_fields") or ():
                    row.pop(name, None)
        return rows

    def read(self, start: date, end: date, *, instructor: str = "",
             since: datetime | None = None) -> Reading:
        """The window, in weekly slices, parsed and filtered."""
        cal = self.site["calendar"]
        chunk = int(cal.get("chunk_days") or 0) or (end - start).days + 1
        rows: list = []
        a = start
        while a <= end:
            b = min(end, a + timedelta(days=chunk - 1))
            rows += self._slice(a, b)
            a = b + timedelta(days=1)
        return parse_rows(rows, self.site, instructor=instructor, since=since)

    @property
    def source(self) -> str:
        return f"GET {self.site['calendar']['events_endpoint']['url']}"


def client_from_env(timeout: int = 30) -> Client:
    """A signed-in client, or a clear instruction to fix the configuration.

    There is no cookie file and no browser: this app runs unattended, so it
    signs in with FITSYS_USER / FITSYS_PASS every time. At nineteen runs a day
    that is nineteen logins, which is still cheaper than owning session state.
    """
    import os

    user = os.getenv("FITSYS_USER", "").strip()
    password = os.getenv("FITSYS_PASS", "").strip()
    if not (user and password):
        missing = ", ".join(n for n, v in (("FITSYS_USER", user),
                                           ("FITSYS_PASS", password)) if not v)
        raise NotSignedIn(f"{missing} not set. This app signs in to FitSys by "
                          f"itself; put the studio login in .env (locally) or "
                          f"in Secret Manager (on Cloud Run).")
    client = Client(timeout=timeout)
    client.sign_in(user, password)
    return client
