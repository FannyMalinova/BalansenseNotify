# BalanSenseNotify as a Cloud Run *job* -- it runs to completion and exits.
# There is no HTTP server and no PORT: a job is not a service.
#
# The image contains this app and nothing else. It does not import, vendor or
# know about the FitSys scheduler: notifier/fitsys_api.py is this app's own
# read-only client and notifier/site.json its own record of the routes. So
# `docker build .` here works with no staging and no FITSYS_HOME.
#
# Read-only is structural: there is no POST to the calendar anywhere in the
# image, and the one endpoint used is GET-only.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cloud.txt

COPY notifier /app/notifier

RUN useradd --create-home --uid 10001 notify && chown -R notify /app
USER notify

# No CMD args: the schedule runs a plain watch. NOTIFY_MODE=test or =reset
# covers the one-off runs, which is what the Cloud Run console can change.
ENTRYPOINT ["python", "-m", "notifier.cli"]
