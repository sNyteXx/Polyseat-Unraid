# Local Level-2 rehearsal image (UbuntuDev / CI only): runtime libraries of the pinned bundle plus the programs
# incusd 7.5.1 requires. Same pinned base as the build. Never used on Unraid.
ARG BASE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${BASE}
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libuv1t64 libsqlite3-0 liblzo2-2 libacl1 libseccomp2 libcap2 liblz4-1 libzstd1 \
      iproute2 rsync attr xz-utils jq util-linux procps ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    printf 'root:1000000:1000000000\n' > /etc/subuid && printf 'root:1000000:1000000000\n' > /etc/subgid
