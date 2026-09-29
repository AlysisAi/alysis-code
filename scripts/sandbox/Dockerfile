# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG VARIANT=dev
ARG SOURCE_URL=https://github.com/AlysisAi/alysis-code
ARG GIT_SHA=dev
ARG VERSION=dev
ARG SOURCE_DATE_EPOCH=0

FROM ${PYTHON_IMAGE} AS base
SHELL ["/bin/sh", "-euxc"]

ARG TARGETPLATFORM
ARG SOURCE_URL
ARG GIT_SHA
ARG VERSION
ARG SOURCE_DATE_EPOCH
# Freeze Debian package indexes instead of resolving against a moving mirror.
ARG DEBIAN_SNAPSHOT=20260927T000000Z

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="Alysis Code Sandbox" \
      org.opencontainers.image.description="Minimal Alysis Code shell sandbox runtime image." \
      org.opencontainers.image.licenses="Apache-2.0"

# Upgrade inherited libraries as well as installing tools, using the same
# immutable snapshot for both. Installing tools alone leaves old base packages.
# The official Python base already supplies python/python3, pip, and venv.
# Installing Debian python3-venv would add a second, unused interpreter and wheels.
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -ri \
          -e "s#https?://(deb|security)\.debian\.org/debian-security#https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/#g" \
          -e "s#https?://deb\.debian\.org/debian#https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/#g" \
          {} +; \
    printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get update; \
    apt-get upgrade -y --with-new-pkgs --no-install-recommends; \
    packages="bash ca-certificates curl git ripgrep build-essential tini xz-utils"; \
    case "${TARGETPLATFORM:-linux/amd64}" in \
        linux/*) packages="${packages} bubblewrap" ;; \
        *) echo "Skipping bubblewrap for TARGETPLATFORM=${TARGETPLATFORM:-unknown}" ;; \
    esac; \
    pinned=""; \
    for package in ${packages}; do \
        version="$(apt-cache policy "${package}" | awk '/Candidate:/ {print $2; exit}')"; \
        if [ -z "${version}" ] || [ "${version}" = "(none)" ]; then \
            echo "No apt candidate found for ${package}" >&2; \
            exit 1; \
        fi; \
        echo "APT pin: ${package}=${version}"; \
        pinned="${pinned} ${package}=${version}"; \
    done; \
    apt-get install -y --no-install-recommends ${pinned}; \
    apt-mark manual ${packages}; \
    rm -rf /var/lib/apt/lists/*

# Exercise the provided interpreter and its bundled venv support at build time.
RUN test "$(command -v python3)" = /usr/local/bin/python3; \
    python3 -m venv /tmp/alysis-venv-smoke; \
    /tmp/alysis-venv-smoke/bin/python -m pip --version; \
    rm -rf /tmp/alysis-venv-smoke

RUN groupadd --gid 1000 alysis; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash alysis; \
    install -d -m 0755 -o 1000 -g 1000 /workspace

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM base AS dev
SHELL ["/bin/sh", "-euxc"]

USER root

ARG TARGETARCH
# Each downloaded tool is version- and digest-pinned for both supported platforms.
ARG NODE_VERSION=24.21.0
ARG NODE_LINUX_AMD64_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_LINUX_ARM64_SHA256=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2
# Node's bundled npm still contains affected transitive packages. Replace the
# complete upstream npm distribution instead of patching its dependency tree.
ARG NPM_VERSION=11.20.0
ARG NPM_SHA256=d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483
ARG GO_VERSION=1.26.8
ARG GO_LINUX_AMD64_SHA256=d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b
ARG GO_LINUX_ARM64_SHA256=211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_LINUX_AMD64_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUSTUP_LINUX_ARM64_SHA256=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
ARG RUST_VERSION=1.91.1

ENV RUSTUP_HOME=/home/alysis/.rustup \
    CARGO_HOME=/home/alysis/.cargo \
    PATH=/home/alysis/.cargo/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update; \
    packages="pkg-config"; \
    pinned=""; \
    for package in ${packages}; do \
        version="$(apt-cache policy "${package}" | awk '/Candidate:/ {print $2; exit}')"; \
        if [ -z "${version}" ] || [ "${version}" = "(none)" ]; then \
            echo "No apt candidate found for ${package}" >&2; \
            exit 1; \
        fi; \
        echo "APT pin: ${package}=${version}"; \
        pinned="${pinned} ${package}=${version}"; \
    done; \
    apt-get install -y --no-install-recommends ${pinned}; \
    apt-mark manual ${packages}; \
    rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH:-amd64}" in \
        amd64) node_arch=x64; node_sha="${NODE_LINUX_AMD64_SHA256}" ;; \
        arm64) node_arch=arm64; node_sha="${NODE_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Node TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    node_archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/${node_archive}"; \
    echo "${node_sha}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
    rm /tmp/node.tar.xz; \
    node --version; \
    npm --version

RUN curl --proto '=https' --tlsv1.2 -fsSLo /tmp/npm.tgz \
        "https://registry.npmjs.org/npm/-/npm-${NPM_VERSION}.tgz"; \
    echo "${NPM_SHA256}  /tmp/npm.tgz" | sha256sum -c -; \
    rm -rf /usr/local/lib/node_modules/npm; \
    mkdir -p /usr/local/lib/node_modules/npm; \
    tar -xzf /tmp/npm.tgz -C /usr/local/lib/node_modules/npm --strip-components=1; \
    rm /tmp/npm.tgz; \
    test "$(npm --version)" = "${NPM_VERSION}"; \
    test "$(npx --version)" = "${NPM_VERSION}"

RUN case "${TARGETARCH:-amd64}" in \
        amd64) go_arch=amd64; go_sha="${GO_LINUX_AMD64_SHA256}" ;; \
        arm64) go_arch=arm64; go_sha="${GO_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Go TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/go.tgz \
        "https://go.dev/dl/go${GO_VERSION}.linux-${go_arch}.tar.gz"; \
    echo "${go_sha}  /tmp/go.tgz" | sha256sum -c -; \
    rm -rf /usr/local/go; \
    tar -C /usr/local -xzf /tmp/go.tgz; \
    rm /tmp/go.tgz; \
    go version

USER alysis
RUN case "${TARGETARCH:-amd64}" in \
        amd64) rust_host=x86_64-unknown-linux-gnu; rustup_sha="${RUSTUP_LINUX_AMD64_SHA256}" ;; \
        arm64) rust_host=aarch64-unknown-linux-gnu; rustup_sha="${RUSTUP_LINUX_ARM64_SHA256}" ;; \
        *) echo "Unsupported Rust TARGETARCH=${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -fsSLo /tmp/rustup-init \
        "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${rust_host}/rustup-init"; \
    echo "${rustup_sha}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --profile minimal --default-toolchain "${RUST_VERSION}" \
        --default-host "${rust_host}" --no-modify-path; \
    rm /tmp/rustup-init; \
    rustc -V; \
    cargo -V

USER root
RUN node -v; \
    npm -v; \
    go version; \
    pkg-config --version

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM dev AS server
SHELL ["/bin/sh", "-euxc"]

USER root
WORKDIR /opt/alysis
# The digest is an official multi-platform uv image; the lockfile supplies hashes
# for all Python runtime and build dependencies.
COPY --from=ghcr.io/astral-sh/uv@sha256:2381d6aa60c326b71fd40023f921a0a3b8f91b14d5db6b90402e65a635053709 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md /opt/alysis/
COPY src /opt/alysis/src
ENV UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/alysis/.venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/alysis/.venv/bin:/home/alysis/.cargo/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN uv sync --locked --no-dev --no-editable --extra server; \
    uv cache clean; \
    alysis --version

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD git --version && rg --version && alysis --version
CMD ["sh", "-lc", "echo alysis-sandbox ready"]
WORKDIR /workspace
USER alysis

FROM ${VARIANT} AS final
USER alysis
