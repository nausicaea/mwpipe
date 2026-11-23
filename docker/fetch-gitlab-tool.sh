#!/bin/sh
# fetch-gitlab-tool NAME VERSION [SHA256]
# fetch-gitlab-tool --print-sha256 NAME VERSION
# Download the Linux build of a modding-openmw Go tool (GOCoverify, Lumenaire) from the GitLab CI
# artifacts of the git tag VERSION. Verify it and install it as /out/NAME.
# If you give no SHA256, the script uses the checksum file that is published next to the zip. This finds
# corrupted downloads. To prevent tampering, give a SHA256 to pin the exact build (the Dockerfile does).
# With --print-sha256, the script installs nothing. It downloads the zip for each architecture and
# writes the SHA-256 values for the Dockerfile. It also compares them with the published checksum files.
set -eu
if [ "${1:-}" = --print-sha256 ]; then
    name="$2"
    version="$3"
    base="https://gitlab.com/api/v4/projects/modding-openmw%2F$name/jobs/artifacts/$version/raw/dist"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    upper="$(echo "$name" | tr '[:lower:]' '[:upper:]')"
    for arch in amd64 arm64; do
        asset="$name-linux-$arch"
        curl -fsSL -o "$tmp/$asset.zip" "$base/$asset.zip?job=package" ||
            { echo "No CI artifact for $name $version ($arch)" >&2; exit 1; }
        sum="$(sha256sum "$tmp/$asset.zip" | cut -d' ' -f1)"
        published="$(curl -fsSL "$base/$asset.zip.sha256sum.txt?job=package" 2>/dev/null ||
                     curl -fsSL "$base/$asset.sha256sum.txt?job=package" 2>/dev/null || true)"
        published="$(echo "$published" | cut -d' ' -f1)"
        if [ -n "$published" ] && [ "$published" != "$sum" ]; then
            echo "$asset: the published checksum $published is not the same as $sum" >&2
            exit 1
        fi
        arch_upper="$(echo "$arch" | tr '[:lower:]' '[:upper:]')"
        echo "ARG ${upper}_SHA256_${arch_upper}=$sum"
    done
    exit 0
fi
name="$1"
version="$2"
want="${3:-}"
asset="$name-linux-${TARGETARCH:?TARGETARCH not set (build with BuildKit)}"
base="https://gitlab.com/api/v4/projects/modding-openmw%2F$name/jobs/artifacts/$version/raw/dist"

curl -fsSL -o "/tmp/$asset.zip" "$base/$asset.zip?job=package" ||
    { echo "No CI artifact for $name $version (tag missing, or artifact expired)" >&2; exit 1; }

if [ -z "$want" ]; then
    for sums in "$asset.zip.sha256sum.txt" "$asset.sha256sum.txt"; do
        if want="$(curl -fsSL "$base/$sums?job=package" | cut -d' ' -f1)" && [ -n "$want" ]; then
            break
        fi
        want=""
    done
fi
[ -n "$want" ] || { echo "No checksum for $asset; pass SHA256 as a build arg" >&2; exit 1; }
echo "$want  /tmp/$asset.zip" | sha256sum -c -

mkdir -p "/tmp/$name" /out
unzip -q -o -d "/tmp/$name" "/tmp/$asset.zip"
install -m 0755 "$(find "/tmp/$name" -type f -name "$asset" | head -n 1)" "/out/$name"
"/out/$name" --version
