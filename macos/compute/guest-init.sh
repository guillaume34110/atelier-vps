#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
/usr/bin/busybox --install -s /bin
chmod 755 /
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /work /tools /modules /mod /job /sys/fs/cgroup

if modprobe virtiofs && mount -t virtiofs atelier /work &&
    mount -t virtiofs -o ro,nosuid,nodev tools /tools && mount -t virtiofs -o ro,nosuid,nodev modules /modules &&
    modprobe loop && modprobe squashfs &&
    mount -t squashfs -o loop /modules/modloop-virt /mod &&
    modprobe virtio_blk && mount -o bind /mod/modules /lib/modules &&
    modprobe ext4 && mount -t ext4 -o nosuid,nodev /dev/vda /job &&
    mount -t cgroup2 cgroup2 /sys/fs/cgroup &&
    echo +pids > /sys/fs/cgroup/cgroup.subtree_control &&
    mkdir /sys/fs/cgroup/atelier && echo 256 > /sys/fs/cgroup/atelier/pids.max; then
    if mkdir /job/source && mkdir -m 700 /job/control &&
        cp -a /work/source/. /job/source/ && chown -R 10001:10001 /job/source &&
        cp /work/exec.py /work/request.json /job/control/ && umount /work; then
        export LD_LIBRARY_PATH=/tools/usr/lib:/tools/lib PYTHONHOME=/tools/usr
        export PATH=/tools/usr/bin:/usr/bin:/bin
        /tools/usr/bin/python3 /job/control/exec.py > /job/control/guest-supervisor.log 2>&1
        result=$?
        [ -f /job/control/stdout.log ] || : > /job/control/stdout.log
        [ -d /job/control/results ] || mkdir /job/control/results
        if mount -t virtiofs atelier /work &&
            cp /job/control/stdout.log /job/control/receipt.json /job/control/guest-supervisor.log /work/ &&
            cp -a /job/control/results /work/; then
            printf '%s\n' "$result" > /work/status.tmp
            mv /work/status.tmp /work/status
            sync
        else
            echo guest_export_failed > /dev/console
        fi
    else
        echo guest_isolation_failed > /dev/console
    fi
else
    echo guest_setup_failed > /dev/console
fi
poweroff -f
