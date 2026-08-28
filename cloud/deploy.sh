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
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-europe-central2}"       # Warsaw: closest EU region to Bulgaria
JOB="${JOB:-balansense-notify}"
BUCKET="${BUCKET:-${PROJECT_ID}-balansense-notify}"
# Every 3 hours from 06:00 through midnight, Europe/Sofia: 06, 09, 12, 15, 18,
# 21, 00. Midnight is the interval's closing check -- without it there is a
# 9-hour blind gap from 21:00 to 06:00.
SCHEDULE="${SCHEDULE:-0 0,6,9,12,15,18,21 * * *}"
SA="${SA:-balansense-notify}"
SA_EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"

step() { printf '\n=== %s\n' "$1"; }

step "APIs"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com --project "$PROJECT_ID"

step "Bucket for the snapshot"
# Uniform access, no public access, and a lifecycle rule is pointless: the
# object is overwritten in place and is a few hundred bytes.
gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
  --uniform-bucket-level-access --project "$PROJECT_ID" 2>/dev/null || true

step "Service account"
gcloud iam service-accounts create "$SA" --project "$PROJECT_ID" \
  --display-name="BalanSenseNotify job" 2>/dev/null || true
# Only what it needs: read+write the one snapshot object, read its own secrets.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role=roles/storage.objectAdmin \
  --project "$PROJECT_ID" >/dev/null

step "Secrets"
for name in FITSYS_USER FITSYS_PASS TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do
  value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "  $name not in the environment -- skipping (create it by hand, or"
    echo "    export $name=... and re-run)"
    continue
  fi
  if gcloud secrets describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- \
      --project "$PROJECT_ID" >/dev/null
    echo "  $name: new version"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- \
      --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
    echo "  $name: created"
  fi
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID" >/dev/null
done

# --source builds and deploys in one step. Because a Dockerfile is present it
# is used (rather than buildpacks), and Cloud Build creates the Artifact
# Registry repo it needs on first use.
step "Build and deploy the Cloud Run job"
gcloud run jobs deploy "$JOB" \
  --source="$(dirname "${BASH_SOURCE[0]}")/.." \
  --region="$REGION" --project "$PROJECT_ID" \
  --service-account="$SA_EMAIL" \
  --max-retries=1 --task-timeout=5m --memory=512Mi --cpu=1 \
  --set-env-vars="STATE_URI=gs://${BUCKET}/state.json,NOTIFY_CHANNEL=telegram,NOTIFY_INSTRUCTOR=${NOTIFY_INSTRUCTOR:-Фани},NOTIFY_DAYS=${NOTIFY_DAYS:-60}" \
  --set-secrets="FITSYS_USER=FITSYS_USER:latest,FITSYS_PASS=FITSYS_PASS:latest,TELEGRAM_TOKEN=TELEGRAM_TOKEN:latest,TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest"

step "Baseline (first run must not notify)"
gcloud run jobs execute "$JOB" --region="$REGION" --project "$PROJECT_ID" \
  --args=--reset --wait

step "Schedule"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud scheduler jobs create http "${JOB}-schedule" \
  --location="$REGION" --project "$PROJECT_ID" \
  --schedule="$SCHEDULE" --time-zone="Europe/Sofia" \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  2>/dev/null || gcloud scheduler jobs update http "${JOB}-schedule" \
  --location="$REGION" --project "$PROJECT_ID" --schedule="$SCHEDULE"

printf '\nDone. Watch it with:\n'
printf '  gcloud run jobs executions list --job=%s --region=%s\n' "$JOB" "$REGION"
printf '  gcloud run jobs execute %s --region=%s --args=--test-send --wait\n' "$JOB" "$REGION"
