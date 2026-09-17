# Job Hunter Bot — Docker image
#
# Build:  docker build -t job-hunter .
# Run:    docker run --env-file .env -v ./data:/app/data job-hunter
#
# Or use docker-compose.yml for the full setup.

FROM python:3.11-slim-bookworm

# ---- uv package manager ----
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# ---- Dependencies (cached layer) ----
# Copy only the lock files first so Docker caches this layer
# unless pyproject.toml or uv.lock actually change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---- Playwright Chromium + system deps ----
# playwright install --with-deps handles all system libraries
# (libnss3, libcups2, libatk-bridge2.0-0, etc.) automatically.
RUN uv run playwright install --with-deps chromium

# ---- Application source ----
COPY . .

# Database lives in /app/data so it survives container rebuilds
# when that directory is volume-mounted.
RUN mkdir -p /app/data

ENV DATABASE_URL=sqlite+aiosqlite:///./data/jobs.db

# ---- Health check ----
# Verify the Python process is still running.  If the bot's event
# loop hangs, the process won't respond and Docker will restart it.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

CMD ["uv", "run", "python", "main.py"]
