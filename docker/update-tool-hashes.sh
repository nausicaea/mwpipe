#!/bin/sh
# update-tool-hashes BASE_REF [DOCKERFILE]
# Set the pinned SHA-256 values of GOCoverify and Lumenaire in the Dockerfile again. The script changes only
# the tools whose *_VERSION is different from the version in BASE_REF (a git commit, for example the base
# of a pull request). For a version that did not change, the script keeps the pinned values: a CI artifact
# that changes under the same tag must stop the build, not get a new pin without notice.
# The workflow .github/workflows/renovate-hashes.yml runs this script on Renovate branches.
set -eu
base="$1"
dockerfile="${2:-Dockerfile}"
here="$(cd "$(dirname "$0")" && pwd)"
old="$(git show "$base:./$dockerfile")"
tmp="$(mktemp)"
trap 'rm -f "$tmp" "$tmp.new"' EXIT

for tool in gocoverify lumenaire; do
    var="$(echo "$tool" | tr '[:lower:]' '[:upper:]')"
    new_version="$(sed -n "s/^ARG ${var}_VERSION=//p" "$dockerfile")"
    old_version="$(printf '%s\n' "$old" | sed -n "s/^ARG ${var}_VERSION=//p")"
    [ -n "$new_version" ] || { echo "$dockerfile has no ARG ${var}_VERSION" >&2; exit 1; }
    if [ "$new_version" = "$old_version" ]; then
        echo "$tool: version $new_version did not change, the SHA-256 values stay"
        continue
    fi
    echo "$tool: $old_version -> $new_version"
    sh "$here/fetch-gitlab-tool.sh" --print-sha256 "$tool" "$new_version" > "$tmp"
    while read -r line; do
        key="${line%%=*}"   # "ARG NAME_SHA256_ARCH"
        grep -q "^$key=" "$dockerfile" || { echo "$dockerfile has no $key" >&2; exit 1; }
        sed "s|^$key=.*|$line|" "$dockerfile" > "$tmp.new"
        cat "$tmp.new" > "$dockerfile"   # keep the mode and the inode of the file
        echo "  $line"
    done < "$tmp"
done
