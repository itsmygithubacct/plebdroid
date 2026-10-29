#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Reproduce the pinned Debian rootfs packaged as assets/debian/rootfs.tar.xz.
#
#   tools/build_debian_rootfs.sh OUTPUT_DIR
#
# Requirements on a Debian/Ubuntu x86-64 or arm64 host, run as an ordinary
# user: mmdebstrap (1.5.7 produced the pinned bytes), unprivileged user
# namespaces with /etc/subuid and /etc/subgid ranges, debian-archive-keyring,
# xz-utils 5.8.x, python3, and, on a non-arm64 host, qemu-user-binfmt.
# Packages come from snapshot.debian.org, so the same snapshot and options give
# the same tar. The signed InRelease files, the tar, and the xz digests are
# checked against native/debian/pins.json. An InRelease mismatch fails before
# mmdebstrap. A tar or xz mismatch fails and leaves the files for review.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 OUTPUT_DIR" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
PINS="$HERE/native/debian/pins.json"
OUT=$1
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

pin() { python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1]))["rootfs"][sys.argv[2]]; print(",".join(v) if isinstance(v, list) else v)' "$PINS" "$1"; }
SNAPSHOT=$(pin snapshot)
INCLUDE=$(pin include)
TAR_SHA256=$(pin tar_sha256)
XZ_SHA256=$(pin xz_sha256)
RELEASE_SHA256=$(pin release_sha256)
SECURITY_RELEASE_SHA256=$(pin security_release_sha256)
TAR="$OUT/debian-trixie-arm64-$SNAPSHOT.tar"

for tool in mmdebstrap xz sha256sum python3; do
    command -v "$tool" >/dev/null || { echo "missing build tool: $tool" >&2; exit 2; }
done
if [ "$(uname -m)" != aarch64 ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    echo "arm64 binfmt is not registered; install qemu-user-binfmt" >&2
    exit 2
fi

# Every file time in the rootfs is clamped to the snapshot time.
SOURCE_DATE_EPOCH=$(python3 -I -c 'import datetime,sys; print(int(datetime.datetime.strptime(sys.argv[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()))' "$SNAPSHOT")
export SOURCE_DATE_EPOCH

# The installed system uses the live Debian mirrors for apt. Only the build
# reads the snapshot. This hook text is part of the pinned bytes.
SOURCES_HOOK='printf "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: https://deb.debian.org/debian-security\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n" > "$1"/etc/apt/sources.list.d/debian.sources; rm -f "$1"/etc/apt/sources.list'
# mmdebstrap copies the build host's /etc/hostname and /etc/resolv.conf into
# the chroot. Replace them, so no build-host detail ships and other hosts
# produce the same bytes; the launcher binds Android's DNS over resolv.conf.
IDENTITY_HOOK='printf "plebdroid\n" > "$1"/etc/hostname; printf "127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n127.0.1.1\tplebdroid\n" > "$1"/etc/hosts; printf "# Plebdroid binds the active Android network DNS servers over this file.\n" > "$1"/etc/resolv.conf; mkdir -p "$1"/root/Documents'
SNAP=https://snapshot.debian.org/archive

# release_sha256 is the clearsigned InRelease, not the unsigned Release file.
verify_inrelease() {
    python3 -I -c '
import hashlib, sys, urllib.request
url, expected = sys.argv[1:]
with urllib.request.urlopen(url, timeout=120) as response:
    data = response.read()
digest = hashlib.sha256(data).hexdigest()
if digest != expected:
    sys.stderr.write("InRelease sha256 %s does not match the pin %s\n%s\n" % (digest, expected, url))
    sys.exit(1)
' "$1" "$2"
}
verify_inrelease "$SNAP/debian/$SNAPSHOT/dists/trixie/InRelease" "$RELEASE_SHA256"
verify_inrelease "$SNAP/debian-security/$SNAPSHOT/dists/trixie-security/InRelease" "$SECURITY_RELEASE_SHA256"

rm -f "$TAR" "$TAR.xz"
mmdebstrap --mode=unshare --variant=apt --architectures=arm64 \
    --include="$INCLUDE" \
    --aptopt='Acquire::Check-Valid-Until "false"' \
    --customize-hook='rm -f "$1"/etc/apt/apt.conf.d/99mmdebstrap' \
    --customize-hook="$SOURCES_HOOK" \
    --customize-hook="$IDENTITY_HOOK" \
    trixie "$TAR" \
    "deb [check-valid-until=no] $SNAP/debian/$SNAPSHOT trixie main" \
    "deb [check-valid-until=no] $SNAP/debian/$SNAPSHOT trixie-updates main" \
    "deb [check-valid-until=no] $SNAP/debian-security/$SNAPSHOT trixie-security main"

actual=$(sha256sum "$TAR" | cut -d' ' -f1)
if [ "$actual" != "$TAR_SHA256" ]; then
    echo "rootfs tar sha256 $actual does not match the pin $TAR_SHA256" >&2
    exit 1
fi
# -9 needs a 192 MiB block, larger than the tar, so this is one deterministic block.
xz -9 -T4 -k -c "$TAR" > "$TAR.xz.tmp"
mv "$TAR.xz.tmp" "$TAR.xz"
actual=$(sha256sum "$TAR.xz" | cut -d' ' -f1)
if [ "$actual" != "$XZ_SHA256" ]; then
    echo "tar matches, but xz output $actual differs from the pin $XZ_SHA256;" >&2
    echo "use xz-utils 5.8.1 or update the pin and runtime/debian/main.py together" >&2
    exit 1
fi
echo "$TAR.xz"
