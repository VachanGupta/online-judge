# Application image for the API and the workers (same image, different command).
#
# The worker shells out to the Docker CLI to launch *sandbox* containers, so the
# CLI must be present here. In docker-compose the host's Docker socket is mounted
# in, so those sandbox containers run as siblings on the host daemon (see
# docker-compose.yml and ARCHITECTURE.md §4 for the security note on this).
FROM python:3.12-slim

# Install ONLY the Docker client (docker-ce-cli) from Docker's official apt repo
# — arch-aware (arm64/amd64) and reliable. The daemon is the host's, reached via
# the mounted socket; we never run a daemon in this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
COPY scripts ./scripts
COPY sandbox ./sandbox
RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Default to the API; the worker service overrides this command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
