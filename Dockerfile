FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates procps tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# the venv belongs to the unprivileged user so the bot can install new requirements at start
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && chown -R node:node /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1

# the project folder (code + config + data) is mounted here by docker-compose
WORKDIR /app
USER node

CMD ["sh", "-c", "pip install -q --disable-pip-version-check -r requirements.txt; exec python -m newsagent bot"]
