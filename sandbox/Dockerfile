# The sandbox image: the toolchains a submission might need (g++, python3) plus
# the in-container supervisor. It is intentionally minimal. The hard isolation
# (network off, memory/CPU/PID caps, read-only root, dropped capabilities, etc.)
# is applied by the host at `docker run` time — see app/runner/sandbox.py — not
# baked here, so the same image serves every language and limit profile.
FROM debian:bookworm-slim

# g++ for C++, python3 for Python submissions AND for the supervisor itself.
# Clean apt lists to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A real non-root user (uid 1000) with a home directory. The host passes
# --user 1000:1000, but baking the user means tools that consult /etc/passwd or
# $HOME behave correctly. HOME points at the writable tmpfs the host mounts at
# run time, so compilers/interpreters can write their caches.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser
ENV HOME=/tmp

# The supervisor is owned by root and world-readable; the unprivileged user only
# needs to execute it, never modify it.
COPY supervisor.py /opt/judge/supervisor.py
RUN chmod 0755 /opt/judge/supervisor.py

USER 1000:1000

# A harmless default; the host always overrides CMD with the real job spec.
ENTRYPOINT ["python3", "/opt/judge/supervisor.py"]
