# Polyseat desktop seat image: rootfs build stage only. The Incus image is made from the flattened export by
# pack-image.py (see build-seat-image.sh). Never `docker run` this for anything but the build.
ARG SEAT_BASE_IMAGE
FROM ${SEAT_BASE_IMAGE}
ARG SEAT_ARCHIVE_DATE
ARG SUNSHINE_PKG_VERSION
ARG SUNSHINE_PKG_URL
ARG SUNSHINE_PKG_SHA256
ENV SEAT_ARCHIVE_DATE=${SEAT_ARCHIVE_DATE} \
    SUNSHINE_PKG_VERSION=${SUNSHINE_PKG_VERSION} \
    SUNSHINE_PKG_URL=${SUNSHINE_PKG_URL} \
    SUNSHINE_PKG_SHA256=${SUNSHINE_PKG_SHA256}
COPY provision.sh /root/provision.sh
RUN sh /root/provision.sh packages
COPY rootfs/ /
RUN sh /root/provision.sh finalize
