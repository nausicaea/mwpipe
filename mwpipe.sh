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

# The container user must be your host user. With rootful Docker, this is your UID and GID. With a
# rootless runtime (rootless Docker, Podman), container UID 0 is your host user, and all other UIDs
# are subordinate IDs (/etc/subuid). Thus your own UID would become a subuid on the host. Container
# root keeps no capabilities (cap_drop, no-new-privileges). Set PUID and PGID to override.
rootless() {
    docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q 'rootless' && return 0
    docker info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -q 'true'   # Podman
}
if [ -z "${PUID:-}" ] || [ -z "${PGID:-}" ]; then
    if rootless; then PUID=0 PGID=0; else PUID="$(id -u)" PGID="$(id -g)"; fi
fi
export PUID PGID
tty=""; [ -t 0 ] || tty="-T"   # stdin is a pipe or file (for example, `build-collection - < list.txt`)
if [ -n "${MWPIPE_IMAGE:-}" ]; then   # published image: pull it, do not build it
    docker compose pull --quiet mwpipe
    exec docker compose run --rm $tty mwpipe "$@"
fi
exec docker compose run --rm $tty --build mwpipe "$@"
