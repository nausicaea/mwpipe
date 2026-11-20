#!/bin/sh
# Convenience wrapper: runs mwpipe in Docker with your UID/GID. Needs only Docker on the host.
#   ./mwpipe.sh run https://www.nexusmods.com/games/morrowind/collections/8euesh
#   ./mwpipe.sh postprocess
#   ./mwpipe.sh build-collection - --name "My list" < modlist.txt
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "Create .env first (cp .env.example .env)" >&2; exit 1; }

# mwpipe's own directories (not OpenMW's): ~/Library/{Caches,Application Support}/mwpipe on macOS,
# XDG directories elsewhere. Override with MWPIPE_{CACHE,STATE,CONFIG}_HOME in your shell environment (not .env).
if [ "$(uname -s)" = Darwin ]; then
    : "${MWPIPE_CACHE_HOME:=$HOME/Library/Caches/mwpipe}"
    : "${MWPIPE_STATE_HOME:=$HOME/Library/Application Support/mwpipe}"
    : "${MWPIPE_CONFIG_HOME:=$HOME/Library/Application Support/mwpipe}"
else
    : "${MWPIPE_CACHE_HOME:=${XDG_CACHE_HOME:-$HOME/.cache}/mwpipe}"
    : "${MWPIPE_STATE_HOME:=${XDG_STATE_HOME:-$HOME/.local/state}/mwpipe}"
    : "${MWPIPE_CONFIG_HOME:=${XDG_CONFIG_HOME:-$HOME/.config}/mwpipe}"
fi
export MWPIPE_CACHE_HOME MWPIPE_STATE_HOME MWPIPE_CONFIG_HOME
mkdir -p "$MWPIPE_CACHE_HOME" "$MWPIPE_STATE_HOME" "$MWPIPE_CONFIG_HOME"

# The compose secret must exist; an empty file means "prompt me for the key".
secret="${NEXUS_API_KEY_SECRET:-./secrets/nexus_api_key}"
if [ ! -e "$secret" ]; then
    mkdir -p "$(dirname "$secret")"
    (umask 077 && : > "$secret")
fi

export PUID="$(id -u)" PGID="$(id -g)"
tty=""; [ -t 0 ] || tty="-T"   # stdin is a pipe or file (e.g. `build-collection - < list.txt`)
if [ -n "${MWPIPE_IMAGE:-}" ]; then   # published image: pull instead of building
    docker compose pull --quiet mwpipe
    exec docker compose run --rm $tty mwpipe "$@"
fi
exec docker compose run --rm $tty --build mwpipe "$@"
