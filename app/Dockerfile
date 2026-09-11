FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as an unprivileged user rather than root inside the container.
RUN useradd --create-home --shell /bin/bash samaya \
    && chown -R samaya:samaya /app
USER samaya

# --reload was a dev-only flag left in the production CMD: it spins up a
# filesystem watcher and — combined with the docker-compose bind mount of
# ./app:/app — can restart the process on any host-side file touch (e.g. a
# backup tool updating mtimes).
#
# Deliberately still a single worker: APScheduler starts in-process inside
# main.py's lifespan, so each additional uvicorn worker would run its own
# copy of the daily regeneration and per-minute reminder jobs — duplicate
# Discord posts and reminders, not just wasted CPU. Scaling past one worker
# needs the scheduler moved out of the request-serving process (or gated
# behind a DB advisory lock so only one worker's copy actually runs the
# jobs) — a separate change, not a Dockerfile flag.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
