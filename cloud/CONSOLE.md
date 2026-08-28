# Deploying to Cloud Run from the Cloud Console

Click-by-click, in order. Nothing needs installing on your computer.

**One step cannot be clicked.** Cloud Run jobs can only be created from a
container image — the Console offers no "deploy from source" for jobs — and the
Console has no way to upload a folder and build it. So step 1 uses **Cloud
Shell**, which is part of the Console: the `>_` terminal icon in the top-right
toolbar. Everything after step 1 is pure UI.

Have these to hand:

| | |
|---|---|
| Project ID | from the project picker at the top of the Console |
| Region | **europe-central2** (Warsaw) — used everywhere below |
| FitSys username / password | the studio login |
| Telegram token | from @BotFather |
| Telegram chat id | what `--telegram-chat-id` printed |

---

## Step 1 — Build the image (Cloud Shell)

The app is self-contained, so this is just "get the folder into Cloud Shell and
build it".

**1a.** On your own computer, zip up the app:

```bash
cd /path/to/BalanSenseNotify
zip -r ~/balansense-notify.zip Dockerfile requirements.txt \
  requirements-cloud.txt notifier
```

**1b.** In the Console, click the **`>_`** (Activate Cloud Shell) icon, top
right. Wait for the prompt.

**1c.** In the Cloud Shell toolbar click **⋮** (three dots) → **Upload** →
**Choose Files** → pick `balansense-notify.zip`.

**1d.** Paste this in, replacing `YOUR-PROJECT-ID` on the first line:

```bash
export PROJECT_ID=YOUR-PROJECT-ID
export REGION=europe-central2
gcloud config set project $PROJECT_ID

gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com storage.googleapis.com

gcloud artifacts repositories create notify \
  --repository-format=docker --location=$REGION

mkdir -p ~/app && unzip -o ~/balansense-notify.zip -d ~/app
gcloud builds submit ~/app \
  --tag $REGION-docker.pkg.dev/$PROJECT_ID/notify/balansense-notify:latest
```

It takes 2–3 minutes and should end with `SUCCESS`. Copy the image path — you
need it in step 5:

```
europe-central2-docker.pkg.dev/YOUR-PROJECT-ID/notify/balansense-notify:latest
```

## Step 2 — A bucket for the snapshot

Cloud Run throws the container's filesystem away after every execution. The
snapshot of "how many were booked last time" must therefore live outside it, or
every run looks like a first run and you are never told anything.

1. Navigation menu (☰) → **Cloud Storage** → **Buckets**
2. **+ CREATE**
3. **Name**: `YOUR-PROJECT-ID-balansense-notify` (must be globally unique)
4. **Where to store your data**: *Region* → **europe-central2**
5. Leave storage class **Standard**
6. **Prevent public access**: leave the checkbox **ticked**
7. **Access control**: *Uniform*
8. **CREATE**

---

## Step 3 — A service account for the job

1. ☰ → **IAM & Admin** → **Service Accounts**
2. **+ CREATE SERVICE ACCOUNT**
3. **Service account name**: `balansense-notify`
4. **CREATE AND CONTINUE**
5. **Grant this service account access to project** — skip it, click
   **CONTINUE**, then **DONE**. (Project-wide roles are not needed; the two
   grants it does need are attached to the bucket and the secrets instead.)
6. Copy its email; it looks like
   `balansense-notify@YOUR-PROJECT-ID.iam.gserviceaccount.com`

### Let it read and write the snapshot

1. ☰ → **Cloud Storage** → **Buckets** → click your bucket
2. **PERMISSIONS** tab → **+ GRANT ACCESS**
3. **New principals**: paste the service account email
4. **Role**: *Cloud Storage* → **Storage Object Admin**
5. **SAVE**

---

## Step 4 — The four secrets

1. ☰ → **Security** → **Secret Manager**
2. **+ CREATE SECRET**
3. **Name**: `FITSYS_USER`
4. **Secret value**: type the value. **Do not press Enter** and do not paste a
   trailing blank line — a stray newline inside `TELEGRAM_TOKEN` makes every
   send fail with `401 Unauthorized`, which looks exactly like a wrong token.
5. **CREATE SECRET**
6. Repeat for **`FITSYS_PASS`**, **`TELEGRAM_TOKEN`**, **`TELEGRAM_CHAT_ID`**.

### Let the job read them

For **each** of the four secrets:

1. Click the secret → **PERMISSIONS** tab → **+ GRANT ACCESS**
2. **New principals**: the service account email
3. **Role**: *Secret Manager* → **Secret Manager Secret Accessor**
4. **SAVE**

---

## Step 5 — Create the Cloud Run job

1. ☰ → **Cloud Run**
2. Click the **Jobs** tab
3. **DEPLOY CONTAINER**
4. **Container image URL**: paste the image path from step 1
5. **Job name**: `balansense-notify`
6. **Region**: **europe-central2**
7. **Number of tasks**: `1`
8. Expand **Container, Variables & Secrets, Connections, Security**

**Container tab**
* **Memory**: `512 MiB`
* **CPU**: `1`
* **Task timeout**: `300` seconds
* **Number of retries per failed task**: `1`
* **Container arguments**: leave empty

**Variables & Secrets tab** — under *Environment variables*, **+ ADD VARIABLE**
five times:

| Name | Value |
|---|---|
| `STATE_URI` | `gs://YOUR-BUCKET-NAME/state.json` |
| `NOTIFY_CHANNEL` | `telegram` |
| `NOTIFY_INSTRUCTOR` | `Фани` |
| `NOTIFY_DAYS` | `60` |
| `NOTIFY_MODE` | `test` |

`NOTIFY_MODE=test` is deliberate: the first execution will send one clearly
marked test message instead of watching. You change it to `watch` in step 8.

Still on the same tab, under *Secrets*, **+ REFERENCE A SECRET** four times:

| Secret | Reference method | Name |
|---|---|---|
| `FITSYS_USER` | Exposed as environment variable | `FITSYS_USER` |
| `FITSYS_PASS` | Exposed as environment variable | `FITSYS_PASS` |
| `TELEGRAM_TOKEN` | Exposed as environment variable | `TELEGRAM_TOKEN` |
| `TELEGRAM_CHAT_ID` | Exposed as environment variable | `TELEGRAM_CHAT_ID` |

Set **Version** to `latest` for each. Put these in *Secrets*, **not** in
*Environment variables* — a value typed into an environment variable is readable
by anyone who can view the job, and one of these is the FitSys password.

**Security tab**
* **Service account**: `balansense-notify@…`

9. **CREATE**

---

## Step 6 — Send the test

1. On the job's page, click **EXECUTE**
2. Wait for the execution to go green (about 10–20 seconds)
3. **A Telegram message should arrive**, beginning *"This is a test, not a
   booking."*

If nothing arrives, click the execution → **LOGS** and read the last lines. The
message names the cause: a wrong instructor name lists the real ones, a bad
token says to check it with @BotFather, a missing bucket grant says
`PERMISSION_DENIED`.

---

## Step 7 — Lay the baseline

The watch reports counts that *changed* since the last look, so it needs a
starting point. Without this, your first scheduled run is spent creating one.

1. On the job's page, **EDIT**
2. **Variables & Secrets** → change `NOTIFY_MODE` from `test` to `reset`
3. **UPDATE**  (this creates a new revision and does not run it)
4. Click **EXECUTE**, wait for green
5. Check it landed: ☰ → **Cloud Storage** → your bucket → you should now see
   **`state.json`**, a few hundred bytes

---

## Step 8 — Switch it to watching

1. **EDIT** → **Variables & Secrets** → change `NOTIFY_MODE` from `reset` to
   `watch`
2. **UPDATE**

Do not click Execute — the schedule takes over next.

---

## Step 9 — The schedule

1. On the job's page, click the **TRIGGERS** tab
2. **ADD SCHEDULER TRIGGER**
3. Enable the Cloud Scheduler API if prompted
4. Fill in:
   * **Name**: `balansense-notify-schedule`
   * **Region**: **europe-central2**
   * **Frequency**: `0 0,6,9,12,15,18,21 * * *`
   * **Timezone**: **Central European Time** / *Europe/Sofia*
5. **CONTINUE**
6. **Service account**: pick one that can invoke the job. The default compute
   service account (`…-compute@developer.gserviceaccount.com`) is offered and is
   fine; the Console grants it the invoker role for you.
7. **CREATE**

That frequency is every 3 hours at 00:00, 06:00, 09:00, 12:00, 15:00, 18:00 and
21:00 Sofia time — seven checks a day, with no checks between midnight and 6am.

---

## Step 10 — Confirm it is live

1. **TRIGGERS** tab → click the scheduler job → **FORCE RUN** (to avoid waiting
   for the next slot)
2. Back on the Cloud Run job → **EXECUTIONS** — a new one should appear
3. Click it → **LOGS**. With nothing newly booked it will say
   *"No change since the last look."* That is success.

You are done. You will get a Telegram message within 3 hours of anyone booking
or cancelling one of your classes.

---

## Afterwards

| To do this | Go here |
|---|---|
| Watch a different instructor, or further ahead | Job → **EDIT** → change `NOTIFY_INSTRUCTOR` / `NOTIFY_DAYS` → **UPDATE** |
| Send another test | **EDIT** → `NOTIFY_MODE=test` → **UPDATE** → **EXECUTE** → set it back to `watch` |
| Change the schedule | **TRIGGERS** → click the scheduler job → **EDIT** |
| Pause it | **TRIGGERS** → scheduler job → **PAUSE** |
| Change the Telegram token | **Secret Manager** → `TELEGRAM_TOKEN` → **+ NEW VERSION**. No redeploy needed: the job asks for `latest` each run |
| Deploy a code change | repeat step 1, then Job → **EDIT** → **UPDATE** |
| See what it has been doing | Job → **EXECUTIONS**, or **LOGS** |
| Read the current snapshot | **Cloud Storage** → bucket → `state.json` |

## Costs

Seven executions a day of a few seconds each — about 210 a month. That sits
inside Cloud Run's free monthly allowance, Cloud Scheduler is free up to three
jobs, and the snapshot is a few hundred bytes. Expect approximately nothing, but
check **Billing → Reports** after the first month rather than trusting this.
