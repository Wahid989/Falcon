# Minimal Python image shared by the Commander and Follower agent services.
#
# This is a placeholder skeleton: the real agent runtimes and their
# dependencies are added later (Follower runtime, Commander CLI, and the
# Python project scaffolding that pins dependencies). For now the image only
# needs to build and keep its container running so `docker compose up` stays
# healthy end to end.
FROM python:3.12-slim

WORKDIR /app

# Agent source will be copied/installed here once it exists.
COPY . /app

# FALCON_ROLE selects which agent this container runs (set per service in
# docker-compose.yml). Until the real runtimes land, both roles share this
# placeholder loop that keeps the container alive without crashing.
ENV FALCON_ROLE=agent

CMD ["python", "-u", "-c", "import os, time; role = os.environ.get('FALCON_ROLE', 'agent'); print(role + ' service placeholder: idle, waiting for runtime', flush=True); time.sleep(2**31)"]
