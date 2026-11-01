#!/bin/sh
# unseenet-gateway in a container (docs/docker.md).
#
#   (no arguments)        unseenet-gateway run, with /data/gateway.toml
#   keys, ripen, create-groups, import-once, stats ...   that gateway command
#   --version, -h         as unseenet-gateway
#   sh, python3, unseenet ...   run that program instead
#
# Tor runs in the container (SOCKS on 127.0.0.1:9050) unless TOR_SOCKS names one
# outside it, e.g. TOR_SOCKS=tor:9050 for a Tor service beside it in compose.
set -eu
DATA=${UNSEENET_DATA:-/data}
CONFIG=${UNSEENET_GATEWAY_CONFIG:-$DATA/gateway.toml}

# Started as root (the default): make /data ours, then drop root for good.
if [ "$(id -u)" = 0 ]; then
    if [ "$(stat -c %u "$DATA")" != 10001 ]; then
        chown -R unseenet:unseenet "$DATA"
    fi
    chmod 700 "$DATA"
    # A secret file is mounted as it is on the host (often root's, mode 600), so the
    # gateway's user may not read it: hand a copy on in /tmp (a tmpfs), readable by it only.
    for s in UNSEENET_GATEWAY_PASSPHRASE:unseenet_gateway_passphrase UNSEENET_GATEWAY_GROUP_PASSPHRASE:unseenet_gateway_group_passphrase; do
        var=${s%%:*} name=${s#*:}
        eval "f=\${${var}_FILE:-}"
        for f in "$f" "/run/secrets/$name"; do
            if [ -n "$f" ] && [ -r "$f" ]; then
                (umask 077 && cat "$f" > "/tmp/$name")
                chown unseenet:unseenet "/tmp/$name"
                export "${var}_FILE=/tmp/$name"
                break
            fi
        done
    done
    exec su-exec unseenet "$0" "$@"
fi

case "${1:-}" in
    sh|ash|python3|unseenet|unseenet-*) exec "$@" ;;
esac
case "${1:-}" in --version|-h|--help) exec unseenet-gateway "$@" ;; esac

if [ ! -f "$CONFIG" ]; then
    cp /opt/unseenet-gateway/gateway.example.toml "$CONFIG"
    echo "unseenet-gateway: wrote an example $CONFIG - edit it (docs/docker.md), then start again." >&2
    exit 64
fi

# The keys' passphrases: Docker secrets, files, or the environment (as unseenet-gateway reads them).
secret() {   # secret VAR NAME: VAR from VAR_FILE or /run/secrets/NAME when not set
    eval "v=\${$1:-}"
    [ -n "$v" ] && return 0
    eval "f=\${$1_FILE:-}"
    for f in "$f" "/run/secrets/$2"; do
        if [ -n "$f" ] && [ -r "$f" ]; then
            export "$1=$(cat "$f")"
            return 0
        fi
    done
}
secret UNSEENET_GATEWAY_PASSPHRASE unseenet_gateway_passphrase
secret UNSEENET_GATEWAY_GROUP_PASSPHRASE unseenet_gateway_group_passphrase

if [ "${1:-run}" != keys ] && [ -z "${TOR_SOCKS:-}" ] && [ -z "${UNSEENET_NO_TOR:-}" ]; then
    mkdir -p "$DATA/tor"
    chmod 700 "$DATA/tor"
    tor --quiet --SocksPort 127.0.0.1:9050 --DataDirectory "$DATA/tor" --RunAsDaemon 0 &
fi

cd "$(dirname "$CONFIG")"
exec unseenet-gateway -c "$CONFIG" "${@:-run}"
