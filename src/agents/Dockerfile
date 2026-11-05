FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scripts ./scripts
COPY config/mvp ./config/mvp
CMD ["python", "-m", "src.agents.follower_runner", "--decision-engine", "src.decision.engine:decide"]
