#!/usr/bin/env bash
# Deploy as a Cloud Run job on a schedule, in one pass: it builds from source,
# so nothing needs building first and there is no image name to manage.
#
# A *job*, not a service: it runs to completion and exits. Jobs have no HTTP
# server and no PORT/8080 requirement -- that applies to Cloud Run services.
#
# Secrets go in Secret Manager, not in --set-env-vars: env vars set on a deploy
# are visible in the service description and in build logs, and one of these is
# the FitSys account password.
#
# The names below are the ones actually deployed, read back off the live project
# on 2026-09-07. They are not decorative: an earlier version of this script
# defaulted to `balansense-notify`, which does not exist, so running it would
# have built a second job and a second Cloud Scheduler entry pointing at it --
# two notifiers racing on one snapshot, not a repair. Override with the
# environment if you really want a second deployment.
#
# To ship a code change to the existing job, this whole script is more than you
# need -- see "Redeploying just the code" in MANUAL.md for the two-command
# image swap, which cannot disturb the env vars, secrets or the state volume.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-europe-central2}"       # Warsaw: closest EU region to Bulgaria
JOB="${JOB:-balansense-job}"
SCHEDULER="${SCHEDULER:-balansense-scheduler}"
BUCKET="${BUCKET:-balansense-state-bucket}"
# Every 10 minutes from 06:00 through midnight, Europe/Sofia: hours 06..23 and
# 00. 114 checks a day. Nothing runs between 01:00 and 06:00, when nobody books.
SCHEDULE="${SCHEDULE:-*/10 0,6-23 * * *}"
# The default compute service account, which is what this job runs as. There is
# no dedicated account: the job's only privileges are the state bucket and its
# own secrets, and both are granted below.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA_EMAIL="${SA_EMAIL:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
# The snapshot is saved with STATE_URI=gs://BUCKET/watch_state.json, so writes
# only succeed if no other run saved in the meantime (generation match, see
# notifier/state.py). Overlapping executions can then no longer overwrite each
# other. The bucket mount at /app/var is left over from before and unused.
MOUNT_PATH="/app/var"
VOLUME="state-vol"

# Secret Manager names are lower-case-hyphenated; the container wants
# UPPER_SNAKE env vars. This is the mapping, and NOTIFY_INSTRUCTOR is in here
# rather than in --set-env-vars because it is a real person's name.
declare -A SECRETS=(
  [FITSYS_USER]=fitsys-user
  [FITSYS_PASS]=fitsys-pass
  [TELEGRAM_TOKEN]=telegram-token
  [TELEGRAM_CHAT_ID]=telegram-chat-id
  [NOTIFY_INSTRUCTOR]=notify-instructor
)

step() { printf '\n=== %s\n' "$1"; }

step "APIs"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com --project "$PROJECT_ID"

step "Bucket for the snapshot"
# A lifecycle rule is pointless: the object is overwritten in place and is a few
# hundred bytes.
gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
  --project "$PROJECT_ID" 2>/dev/null || true

step "Service account access"
# Only what it needs: read+write the one snapshot object, read its own secrets.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role=roles/storage.objectAdmin \
  --project "$PROJECT_ID" >/dev/null

step "Secrets"
for env_name in "${!SECRETS[@]}"; do
  secret="${SECRETS[$env_name]}"
  value="${!env_name:-}"
  if [[ -z "$value" ]]; then
    echo "  $secret not in the environment as $env_name -- leaving it alone"
  elif gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$secret" --data-file=- \
      --project "$PROJECT_ID" >/dev/null
    echo "  $secret: new version"
  else
    printf '%s' "$value" | gcloud secrets create "$secret" --data-file=- \
      --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
    echo "  $secret: created"
  fi
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID" >/dev/null
done

# Build the --set-secrets argument from the same mapping, so the two cannot drift.
secret_args=""
for env_name in "${!SECRETS[@]}"; do
  secret_args+="${secret_args:+,}${env_name}=${SECRETS[$env_name]}:latest"
done

# --source builds and deploys in one step. Because a Dockerfile is present it
# is used (rather than buildpacks), and Cloud Build creates the Artifact
# Registry repo it needs on first use.
step "Build and deploy the Cloud Run job"
# NOTIFY_CHANNEL is deliberately unset: with TELEGRAM_TOKEN and
# TELEGRAM_CHAT_ID present, channel.from_env() picks Telegram on its own.
# NOTIFY_DAYS is unset too, so the job watches the 14-day default.
gcloud run jobs deploy "$JOB" \
  --source="$(dirname "${BASH_SOURCE[0]}")/.." \
  --region="$REGION" --project "$PROJECT_ID" \
  --service-account="$SA_EMAIL" \
  --max-retries=3 --task-timeout=10m --memory=512Mi --cpu=1 \
  --add-volume="name=${VOLUME},type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount="volume=${VOLUME},mount-path=${MOUNT_PATH}" \
  --set-env-vars="STATE_URI=gs://${BUCKET}/watch_state.json" \
  --set-secrets="$secret_args"

step "Baseline (first run must not notify)"
# Only when there is no snapshot yet. Re-baselining an existing deployment would
# throw away the counts every sign-up is measured against, so every booking made
# since the last run would go unreported -- silently, which is the one failure a
# notifier must not have.
if gcloud storage ls "gs://${BUCKET}/watch_state.json" \
     --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "  snapshot already exists -- keeping it, so nothing is missed"
else
  gcloud run jobs execute "$JOB" --region="$REGION" --project "$PROJECT_ID" \
    --args=--reset --wait
fi

step "Schedule"
gcloud scheduler jobs create http "$SCHEDULER" \
  --location="$REGION" --project "$PROJECT_ID" \
  --schedule="$SCHEDULE" --time-zone="Europe/Sofia" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA_EMAIL" \
  2>/dev/null || gcloud scheduler jobs update http "$SCHEDULER" \
  --location="$REGION" --project "$PROJECT_ID" --schedule="$SCHEDULE"

printf '\nDone. Watch it with:\n'
printf '  gcloud run jobs executions list --job=%s --region=%s\n' "$JOB" "$REGION"
printf '  gcloud run jobs execute %s --region=%s --args=--test-send --wait\n' "$JOB" "$REGION"
