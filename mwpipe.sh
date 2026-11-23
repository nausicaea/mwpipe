#!/bin/sh
# Convenience wrapper: it runs mwpipe in Docker with your UID and GID. The host needs Docker only.
#   ./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
#   ./mwpipe.sh postprocess
#   ./mwpipe.sh build-collection - --name "My list" < modlist.txt
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "First, create .env (cp .env.example .env)" >&2; exit 1; }

# The compose secret must exist. An empty file means "ask for the key".
secret="${NEXUS_API_KEY_SECRET:-./secrets/nexus_api_key}"
if [ ! -e "$secret" ]; then
    mkdir -p "$(dirname "$secret")"
    (umask 077 && : > "$secret")
fi

export PUID="$(id -u)" PGID="$(id -g)"
tty=""; [ -t 0 ] || tty="-T"   # stdin is a pipe or file (for example, `build-collection - < list.txt`)
if [ -n "${MWPIPE_IMAGE:-}" ]; then   # published image: pull it, do not build it
    docker compose pull --quiet mwpipe
    exec docker compose run --rm $tty mwpipe "$@"
fi
exec docker compose run --rm $tty --build mwpipe "$@"
