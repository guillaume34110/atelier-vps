#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
/usr/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /work
if modprobe virtiofs && mount -t virtiofs atelier /work; then
    (
        set -e
        modprobe virtio_net
        ip link set eth0 up
        udhcpc -i eth0 -n -q -t 5
        mkdir -p /work/toolchain
        apk --root /work/toolchain --initdb --no-scripts --no-cache --keys-dir /etc/apk/keys \
            --repository https://dl-cdn.alpinelinux.org/alpine/v3.24/main add \
            alpine-baselayout busybox python3=3.14.10-r1 nodejs=24.22.1-r0
    ) > /work/bootstrap.log 2>&1
    result=$?
    printf '%s\n' "$result" > /work/status.tmp
    mv /work/status.tmp /work/status
    sync
else
    echo bootstrap_share_failed > /dev/console
fi
poweroff -f
