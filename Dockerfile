FROM python:3.12-slim

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --no-index --no-deps \
        "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install --no-cache-dir "docker>=7.1.0" "websockets>=12.0"

COPY agent/ ./agent/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Odoo tenant images run as UID 1000 and write to the host-mounted tenant
# directories. Keep the relay user aligned so newly-created directories are
# writable by the Odoo process without world-writable permissions. PostgreSQL
# owns the shared pg scratch directory as GID 999; add the relay user to that
# group so fs.write_bytes can upload pg_dump files there without running as
# root or making the directory world-writable.
RUN groupadd -r -g 999 postgres 2>/dev/null || true \
    && groupadd -r -g 1000 agent \
    && useradd -r -u 1000 -g agent \
        -G "$(getent group 999 | cut -d: -f1)" agent \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh

# The entrypoint learns the numeric GID of the mounted host Docker socket,
# then drops privileges to `agent` before starting Python. This avoids a
# deployment-specific `--group-add $(stat …)` requirement and never keeps the
# long-lived agent process running as root.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "python", "-m", "agent"]
