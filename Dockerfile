# syntax=docker/dockerfile:1
# mwpipe: Nexus collection -> OpenMW, plus plox, delta_plugin, GOCoverify, Lumenaire and tes3cmd.
#
# Versions are only ever set in the annotated ARG lines below, which Renovate keeps current
# (see renovate.json5). Base images get their @sha256 digest from Renovate too.

# ---- Rust tools: built from source at a pinned tag -------------------------------------------
FROM rust:1-alpine AS rust-tools
# g++ is needed by delta_plugin's directxtex crate (C++ build script); the others are for plox and git deps.
RUN apk add --no-cache g++ musl-dev openssl-dev openssl-libs-static git
# renovate: datasource=gitlab-tags depName=portmod/delta-plugin
ARG DELTA_PLUGIN_VERSION=0.25.3
# renovate: datasource=github-tags depName=rfuzzo/plox
ARG PLOX_VERSION=v0.5.0
RUN cargo install --locked --git https://gitlab.com/portmod/delta-plugin --tag "$DELTA_PLUGIN_VERSION" \
 && cargo install --locked --git https://github.com/rfuzzo/plox --tag "$PLOX_VERSION" plox

# ---- Go tools: prebuilt binaries from the GitLab CI artifacts of a pinned tag ----------------
FROM alpine:3 AS go-tools
RUN apk add --no-cache curl unzip
ARG TARGETARCH
# renovate: datasource=gitlab-tags depName=modding-openmw/gocoverify
ARG GOCOVERIFY_VERSION=1.1
# renovate: datasource=gitlab-tags depName=modding-openmw/lumenaire versioning=regex:^(?<major>\d+)\.(?<minor>\d+)(?:-(?<prerelease>test))?$
ARG LUMENAIRE_VERSION=1.0-test
# Optional: pin the exact build. Empty = verify against the checksum file published with the zip.
ARG GOCOVERIFY_SHA256=""
ARG LUMENAIRE_SHA256=""
COPY --chmod=0755 docker/fetch-gitlab-tool.sh /usr/local/bin/fetch-gitlab-tool
RUN fetch-gitlab-tool gocoverify "$GOCOVERIFY_VERSION" "$GOCOVERIFY_SHA256" \
 && fetch-gitlab-tool lumenaire "$LUMENAIRE_VERSION" "$LUMENAIRE_SHA256"

# ---- tes3cmd: a single Perl script, pinned to a commit ---------------------------------------
FROM scratch AS tes3cmd
# renovate: datasource=git-refs depName=tes3cmd packageName=https://github.com/john-moonsugar/tes3cmd branch=master
ARG TES3CMD_COMMIT=f72e9ed9dd18e8545dd0dc2a4056c250cf505790
ADD https://github.com/john-moonsugar/tes3cmd.git#${TES3CMD_COMMIT} /

# ---- Runtime -------------------------------------------------------------------------------------
FROM python:3.13-alpine
# Alpine packages are not version-pinned: Alpine drops old versions from its mirrors, so pins
# break builds within weeks. The base image digest (Renovate) is what moves them forward.
RUN apk add --no-cache perl 7zip libarchive-tools

COPY --from=rust-tools /usr/local/cargo/bin/delta_plugin /usr/local/cargo/bin/plox /usr/local/bin/
COPY --from=go-tools /out/gocoverify /out/lumenaire /usr/local/bin/
COPY --from=tes3cmd --chmod=0755 /tes3cmd /usr/local/bin/tes3cmd

# mwpipe itself is a regular Python package with no dependencies.
COPY pyproject.toml README.md /build/
COPY src /build/src
RUN pip install --no-cache-dir /build && rm -rf /build

# Unprivileged user; compose.yaml maps it to your host UID/GID.
RUN adduser -D -u 1000 modder
USER modder
# mwpipe's own directories; compose.yaml bind-mounts the host's cache/state/config directories here.
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    MWPIPE_CACHE_DIR=/cache MWPIPE_STATE_DIR=/state MWPIPE_CONFIG_DIR=/config
ENTRYPOINT ["mwpipe"]
CMD ["--help"]
