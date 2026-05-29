FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir docker>=7.1.0 websockets>=12.0

COPY agent/ ./agent/

# Runs as non-root but needs Docker socket access — add user to docker group at runtime
# via: docker run --group-add $(stat -c '%g' /var/run/docker.sock) ...
# Or mount the socket and run as root in trusted environments.
USER root

ENTRYPOINT ["python", "-m", "agent"]
