FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir "docker>=7.1.0" "websockets>=12.0"

COPY agent/ ./agent/

# Docker socket access requires group membership or root. Callers should pass
# --group-add $(stat -c '%g' /var/run/docker.sock) to avoid running as root.
RUN groupadd -r agent && useradd -r -g agent agent
USER agent

ENTRYPOINT ["python", "-m", "agent"]
