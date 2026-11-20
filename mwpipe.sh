#!/bin/sh
# Convenience wrapper: runs mwpipe in Docker with your UID/GID. Needs only Docker on the host.
#   ./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
#   ./mwpipe.sh postprocess
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "Create .env first (cp .env.example .env)" >&2; exit 1; }

# The compose secret must exist; an empty file means "prompt me for the key".
secret="${NEXUS_API_KEY_SECRET:-./secrets/nexus_api_key}"
if [ ! -e "$secret" ]; then
    mkdir -p "$(dirname "$secret")"
    (umask 077 && : > "$secret")
fi

export PUID="$(id -u)" PGID="$(id -g)"
if [ -n "${MWPIPE_IMAGE:-}" ]; then   # published image: pull instead of building
    docker compose pull --quiet mwpipe
    exec docker compose run --rm mwpipe "$@"
fi
exec docker compose run --rm --build mwpipe "$@"
