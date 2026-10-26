# Deploying to Cloud Run by hand

The same thing `cloud/deploy.sh` does, one command at a time. Run them in
order; each is safe to re-run.

You do **not** need Docker locally — step 4 builds in the cloud.

> **Already deployed?** Then you almost certainly want
> [Redeploying just the code](#redeploying-just-the-code) instead. The steps
> below create a deployment; they are not the way to ship a code change to one
> that exists. The live names are `balansense-job` (job),
> `balansense-scheduler` (schedule) and `balansense-state-bucket` (snapshot),
> in `europe-central2`.

---

## Redeploying just the code

For a code change — a fix to `notifier/`, say, after FitSys alters a route —
build an image and point the existing job at it. Two commands, and neither can
disturb the env vars, the secrets or the state volume, because neither mentions
them:

```bash
export PROJECT_ID=your-project-id
export REGION=europe-central2
export JOB=balansense-job
export IMG=$REGION-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/$JOB:$(date +%Y%m%d-%H%M)

gcloud builds submit --tag="$IMG" --project="$PROJECT_ID" --region="$REGION" .
gcloud run jobs update "$JOB" --image="$IMG" --region="$REGION" --project="$PROJECT_ID"
```

Then prove it, and read what it said:

```bash
gcloud run jobs execute "$JOB" --region="$REGION" --project="$PROJECT_ID" --wait
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="'"$JOB"'"' \
  --project="$PROJECT_ID" --limit=20 --format='value(textPayload)'
```

An immutable tag rather than `:latest` is what makes this reversible: the
previous image is still in Artifact Registry, so a bad deploy is undone by
`gcloud run jobs update --image=` with the old tag.

Do **not** re-run `deploy.sh` or the steps below for this. `deploy.sh` is
idempotent about the job, but its baseline step and its own defaults are set up
for a first install.

---

## The short version

Steps 1, 2, 5, 6, 7 and 12 are unavoidable (project, APIs, bucket, service
account, secrets, schedule). The build and deploy are one command, because
`--source` builds from the `Dockerfile` and deploys in a single pass:

```bash
gcloud run jobs deploy balansense-job --source . --region "$REGION" ...
```

There is **no web server and no port to expose**: a Cloud Run *job* runs to
completion and exits. The `PORT`/8080 requirement applies to Cloud Run
*services*, which this is not.

## 0. Prerequisites

* `gcloud` installed, and `gcloud auth login` done.
* A project with **billing enabled** (the free tier still requires it).
* The Telegram bot already working locally (`--test-send` arrives).

## 1. Set the values you will reuse

```bash
export PROJECT_ID=your-project-id
export REGION=europe-central2            # Warsaw: closest EU region to Bulgaria
export JOB=balansense-job
export SCHEDULER=balansense-scheduler
export BUCKET=balansense-state-bucket

gcloud config set project "$PROJECT_ID"

# The job runs as the default compute service account. It needs no account of
# its own: its only privileges are the state bucket and its own secrets, both
# granted in steps 3 and 7.
export SA_EMAIL=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')-compute@developer.gserviceaccount.com
```

## 2. Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com
```

## 3–4. Nothing to build separately

`--source` in step 8 builds from the `Dockerfile` and deploys in one pass, and
Cloud Build creates the Artifact Registry repository it needs on first use.
`.dockerignore` keeps the venv, `var/`, `.env`, `tests/` and `cloud/` out of the
upload.

## 5. Create the bucket the snapshot lives in

Cloud Run containers are discarded between executions, so the snapshot cannot
be a file in the container — without this, every run looks like a first run and
nothing is ever reported.

```bash
gcloud storage buckets create "gs://${BUCKET}" \
  --location="$REGION" --uniform-bucket-level-access
```

## 6. Grant the service account its bucket access

```bash
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role=roles/storage.objectAdmin
```

That is the only storage grant it gets, and it is scoped to this one bucket.

## 7. Store the five secrets

**Use `printf`, not `echo`.** `echo` appends a newline, and a trailing newline
inside `telegram-token` makes every send fail with `401 Unauthorized`.

Secret Manager holds them under lower-case-hyphenated names; the container
reads them as UPPER_SNAKE env vars, and step 8 does the mapping.
`NOTIFY_INSTRUCTOR` is a secret rather than a plain env var because it is a
real person's name.

```bash
for NAME in fitsys-user fitsys-pass telegram-token telegram-chat-id notify-instructor; do
  read -rsp "$NAME: " VALUE && echo
  printf '%s' "$VALUE" | gcloud secrets create "$NAME" \
    --data-file=- --replication-policy=automatic
  gcloud secrets add-iam-policy-binding "$NAME" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor
done
unset VALUE
```

`read -rsp` keeps the values out of your shell history. To change one later:

```bash
printf '%s' "new-value" | gcloud secrets versions add telegram-token --data-file=-
```

## 8. Deploy the job

A **job**, not a service: it runs to completion and exits, so there is no HTTP
server and no `PORT`.

```bash
cd /path/to/BalanSenseNotify

gcloud run jobs deploy "$JOB" \
  --source=. \
  --region="$REGION" \
  --service-account="$SA_EMAIL" \
  --max-retries=3 \
  --task-timeout=10m \
  --memory=512Mi \
  --cpu=1 \
  --add-volume="name=state-vol,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount="volume=state-vol,mount-path=/app/var" \
  --set-env-vars="STATE_URI=gs://${BUCKET}/watch_state.json" \
  --set-secrets="FITSYS_USER=fitsys-user:latest,FITSYS_PASS=fitsys-pass:latest,TELEGRAM_TOKEN=telegram-token:latest,TELEGRAM_CHAT_ID=telegram-chat-id:latest,NOTIFY_INSTRUCTOR=notify-instructor:latest"
```

Secrets are mounted with `--set-secrets`, never `--set-env-vars`: env vars set
on a deploy are readable in the job description and in deploy logs, and one of
these is the FitSys account password.

**The snapshot is saved through `STATE_URI`, a `gs://` URI.** That makes every
save conditional on the object's generation, so two overlapping executions
cannot overwrite each other: the one that loses writes nothing and exits 0. It
points at the same object the bucket mount at `/app/var` exposes, so there is
one snapshot, not two; the mount is left over from before and the app no longer
writes through it. The live job has run this way since the September
`authfix-20260907` deploy.

`NOTIFY_CHANNEL` is unset on purpose: with the Telegram secrets present,
`channel.from_env()` selects Telegram by itself. `NOTIFY_DAYS` is unset too, so
the job watches the **14-day default** — note that `.env` uses 60 locally, so a
local `--dry-run` looks further ahead than the deployed job does.

## 9. Prove it works from inside Cloud Run

```bash
gcloud run jobs execute "$JOB" --region="$REGION" --args=--test-send --wait
```

A Telegram message should arrive. This confirms the secrets are mounted, the
FitSys login works from Google's network, and the image runs.

## 10. Lay down the baseline

Do this **before** scheduling. The first run has no snapshot, so it stores one
and sends nothing; without it the first scheduled run is wasted.

```bash
gcloud run jobs execute "$JOB" --region="$REGION" --args=--reset --wait
gcloud storage cat "gs://${BUCKET}/state.json" | head -5
```

## 11. Let Cloud Scheduler invoke the job

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')
export SCHEDULER_SA=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com

gcloud run jobs add-iam-policy-binding "$JOB" \
  --region="$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role=roles/run.invoker
```

## 12. Create the schedule

Every 10 minutes from 06:00 through midnight, Sofia time — the last runs are
00:00–00:50, then nothing until 6am. 114 checks a day.

```bash
gcloud scheduler jobs create http "$SCHEDULER" \
  --location="$REGION" \
  --schedule="*/10 0,6-23 * * *" \
  --time-zone="Europe/Sofia" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$SCHEDULER_SA"
```

## 13. Check on it

```bash
# fire the schedule immediately rather than waiting for the next slot
gcloud scheduler jobs run "$SCHEDULER" --location="$REGION"

# did the executions succeed?
gcloud run jobs executions list --job="$JOB" --region="$REGION" --limit=10

# what did they say?
gcloud logging read \
  "resource.type=cloud_run_job AND resource.labels.job_name=${JOB}" \
  --limit=30 --format='value(textPayload)'

# the current snapshot
gcloud storage cat "gs://${BUCKET}/state.json"
```

---

## Afterwards

**Changing a setting** (the instructor, the window) does not need a rebuild:

```bash
gcloud run jobs update "$JOB" --region="$REGION" \
  --update-env-vars="NOTIFY_DAYS=90"
```

**Changing the schedule:**

```bash
gcloud scheduler jobs update http "${JOB}-schedule" --location="$REGION" \
  --schedule="0 6,12,18 * * *"
```

**Deploying a code change:** just step 8 again — `--source` rebuilds.

**Pausing it** without deleting anything:

```bash
gcloud scheduler jobs pause "${JOB}-schedule" --location="$REGION"
gcloud scheduler jobs resume "${JOB}-schedule" --location="$REGION"
```

**Tearing it all down:**

```bash
gcloud scheduler jobs delete "${JOB}-schedule" --location="$REGION"
gcloud run jobs delete "$JOB" --region="$REGION"
gcloud storage rm -r "gs://${BUCKET}"
gcloud artifacts repositories delete "$REPO" --location="$REGION"
for NAME in fitsys-user fitsys-pass telegram-token telegram-chat-id notify-instructor; do
  gcloud secrets delete "$NAME" --quiet
done
gcloud iam service-accounts delete "$SA_EMAIL" --quiet
```

## If something goes wrong

| Symptom | Cause |
|---|---|
| `401 Unauthorized` from Telegram | trailing newline in the secret — recreate with `printf`, not `echo` |
| Nothing ever notifies, every run says "Baseline saved" | The snapshot is not surviving the container: check the `state-vol` volume is still mounted at `/app/var`, since without it `var/` is container-local and lost on exit |
| Every run dies with `No _token on the login page` | An old image. FitSys moved sign-in to `/api/login`; rebuild and redeploy the code |
| `Nobody in the calendar matches 'Фани'` | `NOTIFY_INSTRUCTOR` misspelled; the log lists the real names |
| `PERMISSION_DENIED` on the bucket | step 6's `objectAdmin` binding missing |
| Scheduler shows the job failing | step 11's `run.invoker` binding missing |
| Execution times out | raise `--task-timeout`; a cold start plus a 60-day read takes a few seconds |
