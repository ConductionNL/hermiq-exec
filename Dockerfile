# hermiq-exec — hardened execution worker for Hermiq (SPECTR-NEXTCLOUD-PLAN.md §6.5)
#
# Security architecture (mirrors concurrentie-analyse/Dockerfile.llm-worker,
# the pattern Specter's jailed containers already run in production):
# - Claude Code CLI runs INSIDE this container, invoked by ex_app/lib/analyze.py
# - Credentials are mounted read-only at RUNTIME by deploy/ — never baked
#   into this image, never committed to this repo
# - Egress is restricted to api.anthropic.com + the Ollama host + Nextcloud
#   (HaRP) by the deploy/ jail (docker-compose.jail.yml + iptables sidecar).
#   This Dockerfile/image cannot enforce that on its own — AppAPI has no
#   per-app egress knob (verified, plan §6.5); the network topology is the
#   control, not anything in this file.
# - Runs as a non-root user
#
# python:3.11 base per the scaffold brief; Node.js is added on top solely to
# install the Claude Code CLI (an npm package) — everything else here is pip.

FROM python:3.11-slim

ARG NODE_MAJOR=22

# Node.js (for the Claude CLI) — install then strip the setup-only tools so
# they don't sit in the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ex_app/ ex_app/
COPY appinfo/ appinfo/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Non-root user. UID 1000 so the persistent-storage volume mount lines up
# with a typical host bind-mount owner in local/dev compose setups.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin hermiq \
    && mkdir -p /home/hermiq/.claude /data \
    && chown -R hermiq:hermiq /app /home/hermiq /data

USER hermiq
ENV HOME=/home/hermiq
ENV APP_PERSISTENT_STORAGE=/data

# Persistent storage (workspace scratch dirs live under here, see
# ex_app/lib/main.py::_workspace_root — wiped per-job, not meant to
# accumulate long-term state).
VOLUME /data

ENTRYPOINT ["./entrypoint.sh"]
