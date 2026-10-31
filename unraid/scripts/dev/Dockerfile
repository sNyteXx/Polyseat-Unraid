# WIP / DO NOT USE ON UNRAID. Local-dev builder image (NOT-FOR-UNRAID). Built once per Dockerfile content;
# the build runs unprivileged inside `docker run` with dropped capabilities and resource limits.
# The base image is pinned by digest and Go by version and SHA-256 (see unraid/build-pins.env).
# Ubuntu apt packages are not version-pinned: results are source-pinned, not bit-for-bit reproducible
# across image rebuilds.
ARG BASE
FROM ${BASE}
ARG GO_VERSION
ARG GO_SHA256
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      autoconf automake autoconf-archive libtool libtool-bin build-essential binutils git pkg-config \
      libsqlite3-dev libacl1-dev libcap-dev libseccomp-dev libudev-dev libuv1-dev liblz4-dev libzstd-dev \
      zlib1g-dev liblzma-dev liblzo2-dev \
      libbtrfs-dev libdevmapper-dev meson ninja-build patchelf protobuf-compiler python3 file curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:$PATH GOTOOLCHAIN=local
