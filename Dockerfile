# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
# mwpipe: Nexus collection -> OpenMW, plus plox, delta_plugin, GOCoverify, Lumenaire and tes3cmd.
#
# Versions are only ever set in the annotated ARG lines below, which Renovate keeps current
# (see renovate.json5). Base images get their @sha256 digest from Renovate too.

# ---- Rust tools: built from source at a pinned tag -------------------------------------------
FROM rust:1-alpine@sha256:8d0e82abfdf9a38071e07cabc5afba4ddbe077631a80e4e742a760c38beadc33 AS rust-tools
# g++ is needed by delta_plugin's directxtex crate (C++ build script); the others are for plox and git deps.
RUN apk add --no-cache g++ musl-dev openssl-dev openssl-libs-static git
# A git tag can move, so the build uses the commit of the tag. Renovate updates the tag and the commit together.
# renovate: datasource=gitlab-tags depName=portmod/delta-plugin digest=commit
ARG DELTA_PLUGIN_VERSION=0.25.3
ARG DELTA_PLUGIN_COMMIT=757c0da9621c09a2eace7d25e6f101da2cc53efe
# renovate: datasource=github-tags depName=rfuzzo/plox digest=commit
ARG PLOX_VERSION=v0.5.0
ARG PLOX_COMMIT=f17ec41ccc59f9235db136282b5f76e916cd6e4a
RUN cargo install --locked --git https://gitlab.com/portmod/delta-plugin --rev "$DELTA_PLUGIN_COMMIT" \
 && cargo install --locked --git https://github.com/rfuzzo/plox --rev "$PLOX_COMMIT" plox

# ---- Go tools: prebuilt binaries from the GitLab CI artifacts of a pinned tag ----------------
FROM alpine:3@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf AS go-tools
RUN apk add --no-cache curl unzip
ARG TARGETARCH
# renovate: datasource=gitlab-tags depName=modding-openmw/gocoverify
ARG GOCOVERIFY_VERSION=1.1
# renovate: datasource=gitlab-tags depName=modding-openmw/lumenaire versioning=regex:^(?<major>\d+)\.(?<minor>\d+)(?:-(?<prerelease>test))?$
ARG LUMENAIRE_VERSION=1.0-test
# SHA-256 of each zip, for each architecture. Renovate cannot calculate these: when it changes a version
# above, the build fails until you set the new values. Get them with:
#   docker/fetch-gitlab-tool.sh --print-sha256 NAME VERSION
ARG GOCOVERIFY_SHA256_AMD64=5878ac3116d369b248cc46c0f7a8467e6d28db61088829d7f019bedc4def9abf
ARG GOCOVERIFY_SHA256_ARM64=9237c7925b6e43eadbf8d441b9b2b93a8178e5ba5db71e0ea8f30912ddbcd21f
ARG LUMENAIRE_SHA256_AMD64=6ff39fe0cd58af6894dd645a80cb55b72f2587c42108932fcb8b39b66462df0c
ARG LUMENAIRE_SHA256_ARM64=b40fe81f3b46eac11156c9653e0f804ac523596e03692672443a0ceebcaad2b8
COPY --chmod=0755 docker/fetch-gitlab-tool.sh /usr/local/bin/fetch-gitlab-tool
RUN case "$TARGETARCH" in \
      amd64) gocoverify_sha="$GOCOVERIFY_SHA256_AMD64" lumenaire_sha="$LUMENAIRE_SHA256_AMD64" ;; \
      arm64) gocoverify_sha="$GOCOVERIFY_SHA256_ARM64" lumenaire_sha="$LUMENAIRE_SHA256_ARM64" ;; \
      *) echo "No pinned SHA-256 for TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
    esac \
 && fetch-gitlab-tool gocoverify "$GOCOVERIFY_VERSION" "$gocoverify_sha" \
 && fetch-gitlab-tool lumenaire "$LUMENAIRE_VERSION" "$lumenaire_sha"

# ---- tes3cmd: a single Perl script, pinned to a commit ---------------------------------------
FROM scratch AS tes3cmd
# renovate: datasource=git-refs depName=tes3cmd packageName=https://github.com/john-moonsugar/tes3cmd branch=master
ARG TES3CMD_COMMIT=f72e9ed9dd18e8545dd0dc2a4056c250cf505790
ADD https://github.com/john-moonsugar/tes3cmd.git#${TES3CMD_COMMIT} /

# ---- Runtime -------------------------------------------------------------------------------------
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
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

# mwpipe's own directories. compose.yaml mounts named volumes on /cache and /state (a new volume copies
# the mode of the image directory, so 1777 lets any UID from compose's `user:` write to it) and the
# files in ./config on /config, read-only.
RUN mkdir -m 1777 /cache /state && mkdir /config

# Unprivileged user; compose.yaml maps it to your host UID/GID.
RUN adduser -D -u 1000 modder
USER modder
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    MWPIPE_CACHE_DIR=/cache MWPIPE_STATE_DIR=/state MWPIPE_CONFIG_DIR=/config
ENTRYPOINT ["mwpipe"]
CMD ["--help"]
