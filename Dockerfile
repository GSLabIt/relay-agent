FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "docker>=7.1.0" "websockets>=12.0"

COPY agent/ ./agent/

# Docker socket access requires group membership or root. Callers should pass
# --group-add $(stat -c '%g' /var/run/docker.sock) to avoid running as root.
# Odoo tenant images run as UID 1000 and write to the host-mounted tenant
# directories. Keep the relay user aligned so newly-created directories are
# writable by the Odoo process without world-writable permissions.
RUN groupadd -r -g 1000 agent && useradd -r -u 1000 -g agent agent
USER agent

ENTRYPOINT ["python", "-m", "agent"]
