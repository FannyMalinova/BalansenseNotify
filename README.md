# BalanSenseNotify

A message when someone signs up for a class at BalanSense, or drops out — with
the running total of bookings. Telegram, e-mail, or Viber.

Completely separate from the **FitSys scheduler**. That program builds the
weekly schedule and writes to the calendar; this one only watches and tells you.
They share no code, no configuration and no deployment: this app has its own
read-only client (`notifier/fitsys_api.py`) and its own record of the site's
routes (`notifier/site.json`), and does not know or care where the scheduler
lives.

The accepted cost: site knowledge exists in two places, so a route change has to
be fixed twice. A test compares the two maps whenever both happen to be on the
same machine, so drift shows up in development — but there is no runtime
dependency, and the container carries this app alone.

## What a message looks like

Subject — which is all a phone shows on the lock screen:

```
+2 · 2 класа · общо записани 3
```

Body:

```
+1 записан
Реформър индивидуален · петък 28.08 10:00 · Зала 2
1/1 · Ели Александрова

-2 отписани
Body Ballet за начинаещи · петък 28.08 12:00 · зала 1
3/10 · Тамара Ф

Общо записани: 53 в 60 класа
```

Counts, never names. The feed this reads (`calendar/get-events-basic`) carries
`current_people` and `people` but no customer names or ids at all, so a sign-up
is reported without the program ever holding client data. Open FitSys itself to
see who.

That is also the one limitation worth knowing: a count shows the **net** change.
If one client signs out while another signs up inside the same interval, the
number is unchanged and nothing is reported. Naming the client would mean the
scheduler's other feed and a client's name being sent to Viber's servers.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Just `requests` and `python-dotenv` — no browser, no model, no API key, and
nothing else on disk. Put the studio login in `.env`:

```
FITSYS_USER=...
FITSYS_PASS=...
```

It signs in over plain HTTP each run. At seven runs a day that is seven logins,
which is why no session is persisted.

## Run

```bash
.venv/bin/python -m tests.test_notifier      # 143 offline checks, no account
.venv/bin/python -m notifier.cli --test-send # send one real message now
.venv/bin/python -m notifier.cli --dry-run   # print, never send
.venv/bin/python -m notifier.cli --reset     # re-baseline, send nothing
.venv/bin/python -m notifier.cli --all       # the whole studio, deliberately
```

## Whose classes

Set once, in `.env`:

```
NOTIFY_INSTRUCTOR=Фани
```

A given name on its own is enough, and is what this studio uses — no other
instructor is called Фани. A full name works too: the calendar abbreviates
surnames (it stores `Фани М`) and the app matches the two for you, requiring the
given names to be equal and the feed's initial to start the configured surname. That is what keeps the
studio's `Здравка Д` and `Здравка М` apart, and what stops a plain substring
match on `Ани М` from also catching `Фани М`.

Two deliberate refusals here, because the alternative to both is a notifier that
looks fine and says nothing for weeks:

- **No name set → it will not run.** Watching the whole studio is ~300 classes a
  month; it has to be asked for with `--all`, not happen because a setting was
  forgotten.
- **A name nobody uses → it exits and lists who actually teaches.**

```
$ .venv/bin/python -m notifier.cli --instructor "Фанни Малинова"
Nobody in the calendar matches 'Фанни Малинова'.
FitSys abbreviates surnames, so check the given name's spelling. Teaching in the next 14 day(s):
  Ани М
  Ася П
  Ели Александрова
  ...
```

One limit worth knowing: a typo in the *surname* cannot be caught, because the
feed only carries the initial — `Фани Малиноваа` still matches `Фани М`. A wrong
given name or a wrong initial is caught.

One shot per invocation; cron sets the cadence:

```cron
# every 3 hours from 06:00 through midnight (Europe/Sofia)
0 0,6,9,12,15,18,21 * * * cd /path/to/BalanSenseNotify && \
              .venv/bin/python -m notifier.cli >> var/notify.log 2>&1
```

`NOTIFY_INSTRUCTOR` from `.env` scopes it, so the cron line needs no arguments.

The first run has no snapshot, so it saves a baseline and sends nothing —
otherwise it would fire one message per class in the window.

## Choosing a channel

`NOTIFY_CHANNEL` pins one; left empty, whichever is configured wins, Telegram
first, then e-mail. Nothing configured falls back to printing, so a half-set-up
cron job shows in the log rather than silently doing nothing.

### Telegram (recommended)

Free, self-serve, nothing hosted, about two minutes:

1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a username
   → copy the token into `TELEGRAM_TOKEN`.
2. Find your new bot and send it any message (`/start` will do). This matters:
   **a bot may only write to someone who has written to it first.**
3. Run the setup helper, which reads the ids off Telegram for you:

```bash
.venv/bin/python -m notifier.cli --telegram-chat-id
```

```
Chats that have messaged this bot:

  TELEGRAM_CHAT_ID=456789          private    Фани fanny
```

4. Paste that into `.env` with `NOTIFY_CHANNEL=telegram`.
5. Prove delivery before a booking depends on it:

```bash
.venv/bin/python -m notifier.cli --test-send
```

It states only what is true right now — how many classes are being watched and
the current booking total — and shows the shape of a real notice separately,
indented under an explicit heading. It never fabricates a sign-up: an earlier
version did, to demonstrate the format, and it read as a real booking. It also
does not touch the snapshot, since a test must not consume the baseline the
watch relies on.

Messages go as **plain text with no `parse_mode`** on purpose: Telegram's
Markdown and HTML modes need escaping, and FitSys class names really do contain
`_`, `*` and `-` (`Body Ballet за начинаещи`, `Реформър - индивидуален`) — an
unescaped one is an API error or a mangled message. Plain text cannot fail that
way. Messages over Telegram's 4096-character limit are trimmed rather than
rejected.

### E-mail

Free, no provider approval, no regulator. Four settings in `.env`:

```
SMTP_HOST=smtp.mail.yahoo.com     # Gmail: smtp.gmail.com; Outlook: smtp-mail.outlook.com:587
SMTP_USER=you@yahoo.com
SMTP_PASS=<app-specific password>
MAIL_TO=you@yahoo.com
```

`SMTP_PASS` almost always has to be an **app-specific password**, not your
account password — Yahoo: *Account Security → Generate app password*; Gmail:
*App passwords*, which needs 2-step verification first. A rejected login says so
explicitly rather than just failing. `SMTP_PORT` defaults to 465 (implicit TLS);
587 uses STARTTLS; 25 is refused rather than sent unauthenticated.

Turn on push for the mail account on your phone and it behaves much like an SMS.

### SMS — not implemented, and why

Cheap per message (~$0.008 to Bulgaria) but the delivery path isn't viable.
Domestic long codes and short codes are unsupported in Bulgaria; a domestic
alphanumeric sender ID needs business-only pre-registration with roughly four
weeks' provisioning and forbids P2P traffic. The one route that works now is an
international long code, where the sender ID isn't preserved — messages arrive
from an unpredictable foreign number, which is also what spam filters look for.

### Viber — only for a bot registered before 5 Feb 2024

Since 5 February 2024 a new Viber bot carries a **EUR 100 monthly maintenance
fee per bot** plus per-delivery charges for chatbot-initiated messages — which is
what every message this app sends. New bots also can't be self-registered:
business clients apply through forbusiness.viber.com or a verified partner, with
manual review and a signed contract. Bots registered *before* that date stay on
the free legacy model, and that is the only case worth configuring.

If you have such a bot:

1. Token: Viber → **More → Settings → Bots → Edit Info → "Your app key"**.
2. Receiver id: either read `sender.id` from a Message callback / `user.id` from
   a Subscribed callback, or `POST https://chatapi.viber.com/pa/get_account_info`
   and take your own `id` from the `members` array.
3. A webhook with a CA-signed certificate must be set via
   `POST /pa/set_webhook` before 1-on-1 messaging works at all, and you must
   have messaged the bot once to subscribe.
4. Set `VIBER_TOKEN`, `VIBER_RECEIVER` and `NOTIFY_CHANNEL=viber`.

Without those two values the app prints the message instead of sending it, so a
half-configured cron job shows up in the log rather than silently doing nothing.
A failed send leaves the snapshot untouched, so the next run reports the same
change rather than losing it — both of those are covered by tests.

All three channels are pinned down offline. Telegram: the token in the URL path,
the plain-text body, the 4096-character trim, and the two errors that actually
happen in setup — `401 Unauthorized` (bad token) and `chat not found` (you have
not messaged the bot). E-mail: the subject and UTF-8 body, port
465 vs 587 (implicit TLS vs STARTTLS), the refusal of port 25, and a rejected
login naming app-specific passwords as the likely cause. Viber: the
URL, the `X-Viber-Auth-Token` header, the `receiver`/`type`/`text`/`sender.name`
body, the 20-second timeout, and the three failure modes — a refusal arriving as
`status != 0` inside an **HTTP 200** body, a non-JSON reply, and a dropped
connection. What is *not* verified either way is delivery to a real device —
that needs your own credentials.

## Running it on Cloud Run

A Cloud Run **job**, not a service: it runs to completion and exits, so there is
no HTTP server and no `PORT`. Cloud Scheduler triggers it.

```bash
PROJECT_ID=my-project \
  FITSYS_USER=... FITSYS_PASS=... \
  TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... \
  bash cloud/deploy.sh     # APIs, bucket, secrets, build, deploy, schedule
```

`--source` builds from the `Dockerfile` and deploys in one pass, so there is no
image to build first and no Artifact Registry repo to create.

**No web server, no port.** A Cloud Run *job* runs to completion and exits; the
`PORT`/8080 requirement applies to Cloud Run *services*, which this is not. The
`Dockerfile` has no `EXPOSE`, the app has no loop, and it returns an exit code.

Two alternatives to the script:

* **[cloud/CONSOLE.md](cloud/CONSOLE.md)** — click-by-click in the Cloud
  Console, nothing to install. One step uses Cloud Shell, because Cloud Run jobs
  can only be created from an image and the Console cannot build one.
* **[cloud/MANUAL.md](cloud/MANUAL.md)** — the same steps as individual
  `gcloud` commands, with a troubleshooting table.

`cloud/deploy.sh` is readable top to bottom and idempotent — re-run it after a
code change. It ends by running the job once with `--reset` to lay down the
baseline *before* the schedule starts, because the first run never notifies.

### The snapshot cannot live in the container

This is the thing that makes Cloud Run different from cron. Container
filesystems are thrown away between executions, so `var/watch_state.json` would
be gone every time — every run would look like a first run, save a baseline, and
report nothing. Silently, for ever.

So the snapshot is pluggable. `STATE_URI` takes either a path or a
`gs://bucket/object.json` URI; the deploy script points it at a bucket it
creates. Writes are conditional on the object's generation, so two overlapping
executions cannot clobber each other — the loser aborts, exits 0 so the
scheduler does not flag it, and the next run reads the winner and carries on.

### Why the build stages a context

`gcloud run jobs deploy --source .` builds it; `docker build .` works too.
The image is `notifier/` plus `requests`,
`python-dotenv` and `google-cloud-storage`; `.dockerignore` keeps out the venv,
`var/`, `.env`, tests and `cloud/`. It runs as a non-root user and has no
`FITSYS_HOME`, because there is nothing outside the app to point at.

**Read-only is structural here, not configured.** There is no POST to the
calendar anywhere in the image: `fitsys_api.py` has exactly one `.post(`, the
login form, and the one data route is `GET /calendar/get-events-basic`, which is
GET-only server-side. `site.json` describes no write routes because there is no
write code to route. A test asserts all of that.

### Secrets, and what it can reach

`FITSYS_USER`, `FITSYS_PASS`, `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` go into
Secret Manager and are mounted as env vars — not `--set-env-vars`, which would
put the FitSys password into the job description and the deploy logs. The job
gets its own service account with exactly two grants: `objectAdmin` on the one
bucket, and `secretAccessor` on those four secrets.

### Cost

Seven executions a day — about 210 a month, a few seconds each. Far inside Cloud
Run's free monthly vCPU-second allowance, with Cloud Scheduler free up to 3 jobs
and a few hundred bytes in GCS. Effectively free, but check current allowances
against your own billing rather than taking my word for it.

### Checking on it

```bash
gcloud run jobs executions list --job=balansense-notify --region=europe-central2
gcloud run jobs execute balansense-notify --region=europe-central2 \
  --args=--test-send --wait
```

## How it is put together

| file | role | needs the network |
|---|---|---|
| `notifier/watch.py` | booking counts → what changed; name matching; the message | no |
| `notifier/channel.py` | which channel the environment picks; the console fallback | no |
| `notifier/telegram.py` | Telegram — the recommended channel | yes |
| `notifier/mail.py` | e-mail over SMTP | yes |
| `notifier/state.py` | the snapshot: local file or GCS object; ids and integers only | for GCS |
| `notifier/fitsys_link.py` | the one seam onto the scheduler's package | no |
| `notifier/reader.py` | read-only calendar reads via `fitsys.http_client` | yes |
| `notifier/viber.py` | Viber, for a pre-2024 bot | yes |
| `notifier/cli.py` | the command line cron runs | yes |

Design choices worth knowing:

- **Completely separate from the scheduler.** No shared code, no shared config,
  no shared deployment — the price being a second copy of the site knowledge in
  `site.json`, checked against the scheduler's map by a test when both are
  present. Chosen because this app is deployed on its own as a container job,
  and an image that carries the scheduler's web app and write path is the wrong
  thing to ship for something that only reads.
- **Read-only by construction, not by configuration.** There is no write code to
  gate: one POST (the login form), one GET data route, no write routes in
  `site.json`. A test parses each module's syntax tree and fails on any call
  that could write.
- **Field names come from `site.json`, never hardcoded**, so a route or field
  rename is one edit in one file.
- **A class new to the watch is never a sign-up.** Only a count that moved
  against a baseline we actually saw counts, or every newly created class would
  fire a false notice.
- **The reporting logic is pure.** `watch.py` has no clock, no network and no
  filesystem, and does not import the scheduler — so what counts as a sign-up
  and what gets said is testable offline with no FitSys account.

## Status

Verified against the live BalanSense calendar, read-only. Scoped to
`Фани`: 12 classes and 3 bookings over the next four weeks, out of 299
classes and 15 instructors in the studio. Sign-ups and sign-outs detected and
rendered correctly; a mistyped given name exits with the list of real names. Viber delivery is **not** yet
verified — it needs the bot token and receiver id above, and the setup steps
were written from documentation, not from a completed run.
