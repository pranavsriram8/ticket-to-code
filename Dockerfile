# ──────────────────────────────────────────────
# ticket-to-code — Production Docker Image
# ──────────────────────────────────────────────
# Multi-stage build for a lean production image.
#
# Build:
#   docker build -t ticket-to-code .
#
# Run:
#   docker run --env-file .env -p 8000:8000 ticket-to-code

# ── Stage 1: Build dependencies ──────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed for some Python packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir --prefix=/install --require-hashes -r requirements.lock

# ── Stage 2: Runtime image ───────────────────────
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ ./app/

# Don't copy .env into the image — pass it at runtime via --env-file or
# environment variables from your secret manager (AWS Secrets Manager, etc.)

# Switch to non-root user
USER appuser

# Expose the FastAPI port
EXPOSE 8000

# Note: No HEALTHCHECK directive — Cloud Run uses its own startup/liveness
# probes (TCP on port 8000). Docker HEALTHCHECK spawns child processes that
# clutter logs with "Child process died" messages.

# Run with uvicorn
# - Workers: 1 — Cloud Run scales horizontally via instances, not workers.
#   Multiple workers cause OOM on 512Mi (dspy + litellm are heavy).
# - Host 0.0.0.0: listen on all interfaces (required in containers)
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
