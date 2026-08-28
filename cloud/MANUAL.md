# Deploying to Cloud Run by hand

The same thing `cloud/deploy.sh` does, one command at a time. Run them in
order; each is safe to re-run.

You do **not** need Docker locally — step 4 builds in the cloud.

---

## The short version

Steps 1, 2, 5, 6, 7 and 12 are unavoidable (project, APIs, bucket, service
account, secrets, schedule). The build and deploy are one command, because
`--source` builds from the `Dockerfile` and deploys in a single pass:

```bash
gcloud run jobs deploy balansense-notify --source . --region "$REGION" ...
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
export JOB=balansense-notify
export BUCKET=${PROJECT_ID}-balansense-notify
export SA=balansense-notify
export SA_EMAIL=${SA}@${PROJECT_ID}.iam.gserviceaccount.com

gcloud config set project "$PROJECT_ID"
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

## 6. Create the job's own service account

```bash
gcloud iam service-accounts create "$SA" \
  --display-name="BalanSenseNotify job"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role=roles/storage.objectAdmin
```

That is the only storage grant it gets, and it is scoped to this one bucket.

## 7. Store the four secrets

**Use `printf`, not `echo`.** `echo` appends a newline, and a trailing newline
inside `TELEGRAM_TOKEN` makes every send fail with `401 Unauthorized`.

```bash
for NAME in FITSYS_USER FITSYS_PASS TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do
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
printf '%s' "new-value" | gcloud secrets versions add TELEGRAM_TOKEN --data-file=-
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
  --max-retries=1 \
  --task-timeout=5m \
  --memory=512Mi \
  --cpu=1 \
  --set-env-vars="STATE_URI=gs://${BUCKET}/state.json,NOTIFY_CHANNEL=telegram,NOTIFY_INSTRUCTOR=Фани,NOTIFY_DAYS=60" \
  --set-secrets="FITSYS_USER=FITSYS_USER:latest,FITSYS_PASS=FITSYS_PASS:latest,TELEGRAM_TOKEN=TELEGRAM_TOKEN:latest,TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest"
```

Secrets are mounted with `--set-secrets`, never `--set-env-vars`: env vars set
on a deploy are readable in the job description and in deploy logs, and one of
these is the FitSys account password.

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

Every 3 hours from 06:00 through midnight, Sofia time.

```bash
gcloud scheduler jobs create http "${JOB}-schedule" \
  --location="$REGION" \
  --schedule="0 0,6,9,12,15,18,21 * * *" \
  --time-zone="Europe/Sofia" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$SCHEDULER_SA"
```

## 13. Check on it

```bash
# fire the schedule immediately rather than waiting for the next slot
gcloud scheduler jobs run "${JOB}-schedule" --location="$REGION"

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
for NAME in FITSYS_USER FITSYS_PASS TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do
  gcloud secrets delete "$NAME" --quiet
done
gcloud iam service-accounts delete "$SA_EMAIL" --quiet
```

## If something goes wrong

| Symptom | Cause |
|---|---|
| `401 Unauthorized` from Telegram | trailing newline in the secret — recreate with `printf`, not `echo` |
| Nothing ever notifies, every run says "Baseline saved" | `STATE_URI` is not a `gs://` URI, so the snapshot dies with the container |
| `Nobody in the calendar matches 'Фани'` | `NOTIFY_INSTRUCTOR` misspelled; the log lists the real names |
| `PERMISSION_DENIED` on the bucket | step 6's `objectAdmin` binding missing |
| Scheduler shows the job failing | step 11's `run.invoker` binding missing |
| Execution times out | raise `--task-timeout`; a cold start plus a 60-day read takes a few seconds |
