#!/bin/sh
# fetch-gitlab-tool NAME VERSION [SHA256]
# Download the Linux build of a modding-openmw Go tool (GOCoverify, Lumenaire) from the GitLab CI
# artifacts of the git tag VERSION. Verify it and install it as /out/NAME.
# If you give no SHA256, the script uses the checksum file that is published next to the zip. This finds
# corrupted downloads. To prevent tampering, give a SHA256 to pin the exact build.
set -eu
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
