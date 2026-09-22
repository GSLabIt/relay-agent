#!/bin/sh
# Grant the unprivileged relay process access to the host Docker socket when
# it is mounted. Docker preserves the host socket's numeric GID in the
# container, but that GID varies by host and cannot be baked into the image.
set -eu

socket_path=/var/run/docker.sock

if [ -S "$socket_path" ]; then
    socket_gid=$(stat -c '%g' "$socket_path")
    socket_group=$(getent group "$socket_gid" | cut -d: -f1 || true)

    if [ -z "$socket_group" ]; then
        socket_group="docker-host-$socket_gid"
        groupadd --gid "$socket_gid" "$socket_group"
    fi

    usermod -aG "$socket_group" agent
fi

exec gosu agent "$@"
