#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Run the release checks, including the unit-test suite, without root.
# install-profile.sh and upgrade-profile.sh run as root on the live controller
# host; a test with a missing mock must fail with EACCES instead of reaching
# sysfs, /run, or systemd.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
cd /

copy=''
cleanup() {
    [[ -z $copy ]] || rm -rf -- "$copy"
}
trap cleanup EXIT

if [[ ${SUDO_UID:-} =~ ^[1-9][0-9]*$ && ${SUDO_GID:-} =~ ^[0-9]+$ ]]; then
    uid=$SUDO_UID
    gid=$SUDO_GID
else
    uid=$(id -u nobody)
    gid=$(id -g nobody)
fi
unprivileged=(setpriv --reuid="$uid" --regid="$gid" --clear-groups
              --no-new-privs --reset-env --)

tree=$repo
if ! "${unprivileged[@]}" test -r "$repo/SHA256SUMS"; then
    # The checkout may sit below a private home directory. Test a root-owned
    # copy that only root and the unprivileged group can traverse.
    copy=$(mktemp -d)
    find "$repo" -mindepth 1 -maxdepth 1 ! -name .git -exec cp -R -t "$copy" -- {} +
    chmod -R u=rwX,go=rX -- "$copy"
    chgrp -- "$gid" "$copy"
    chmod 0750 -- "$copy"
    tree=$copy
fi

echo "Running release checks as uid $uid gid $gid in $tree"
"${unprivileged[@]}" env PYTHONDONTWRITEBYTECODE=1 "$tree/scripts/verify-release.sh"
