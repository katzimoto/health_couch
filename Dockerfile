# One image, several roles. Each docker-compose service runs a different command
# on this same build (scheduler, mcp, telegram).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY garmin_coach/ ./garmin_coach/
COPY scripts/ ./scripts/

# Data and Garmin tokens live on mounted volumes (see docker-compose.yml).
RUN mkdir -p /app/data

# Default role; overridden per-service in docker-compose.
CMD ["python", "-m", "garmin_coach.scheduler"]
