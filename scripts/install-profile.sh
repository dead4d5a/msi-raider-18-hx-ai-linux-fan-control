#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
"$repo/scripts/preflight.sh"

paths=(
  /usr/local/sbin/msi-fan-profile
  /usr/local/libexec/msi-fan-profiled
  /usr/local/libexec/msi-gpu-recover
  /etc/systemd/system/msi-fan-profile.service
  /run/systemd/system/msi-fan-profile.service
  /usr/lib/systemd/system/msi-fan-profile.service
  /lib/systemd/system/msi-fan-profile.service
  /etc/systemd/system/multi-user.target.wants/msi-fan-profile.service
  /etc/tmpfiles.d/msi-fan-profile.conf
  /usr/local/share/doc/msi-fan-profile
  /run/msi-fanctl.lock
  /run/msi-fan-profile-manager.lock
  /run/msi-fan-profile.factory-auto
  /run/msi-fan-profile.ack
)
for path in "${paths[@]}"; do
  [[ ! -e $path && ! -L $path ]] || {
    echo "Refusing to overwrite existing path: $path" >&2
    exit 1
  }
done
load_state=$(systemctl show msi-fan-profile.service -p LoadState --value 2>/dev/null || true)
[[ -z $load_state || $load_state == not-found ]] || {
  echo "Refusing to replace an already loaded unit: LoadState=$load_state" >&2
  exit 1
}

installed=0
rollback() {
  local rc=$?
  trap - ERR INT TERM EXIT
  if ((installed)); then
    systemctl disable --now msi-fan-profile.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/multi-user.target.wants/msi-fan-profile.service
    rm -f /etc/systemd/system/msi-fan-profile.service
    rm -f /etc/tmpfiles.d/msi-fan-profile.conf
    rm -f /usr/local/sbin/msi-fan-profile
    rm -f /usr/local/libexec/msi-fan-profiled
    rm -f /usr/local/libexec/msi-gpu-recover
    rm -rf /usr/local/share/doc/msi-fan-profile
    rm -f /run/msi-fan-profile.factory-auto /run/msi-fan-profile.ack
    rm -f /run/msi-fan-profile-manager.lock /run/msi-fanctl.lock
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  echo "Installation failed; artifacts created by this invocation were removed (rc=$rc)." >&2
  exit "$rc"
}
trap rollback ERR INT TERM
installed=1

install -o root -g root -m 0755 "$repo/src/msi-fan-profile" /usr/local/sbin/msi-fan-profile
install -o root -g root -m 0755 "$repo/src/msi-fan-profiled" /usr/local/libexec/msi-fan-profiled
install -o root -g root -m 0755 "$repo/src/msi-gpu-recover" /usr/local/libexec/msi-gpu-recover
install -o root -g root -m 0644 "$repo/systemd/msi-fan-profile.service" /etc/systemd/system/msi-fan-profile.service
install -o root -g root -m 0644 "$repo/tmpfiles/msi-fan-profile.conf" /etc/tmpfiles.d/msi-fan-profile.conf
install -d -o root -g root -m 0755 /usr/local/share/doc/msi-fan-profile
install -o root -g root -m 0644 "$repo/docs/OPERATIONS.md" /usr/local/share/doc/msi-fan-profile/OPERATIONS.md
install -o root -g root -m 0644 "$repo/docs/SAFETY.md" /usr/local/share/doc/msi-fan-profile/SAFETY.md

systemd-tmpfiles --create /etc/tmpfiles.d/msi-fan-profile.conf
systemctl daemon-reload
systemctl enable msi-fan-profile.service
[[ $(systemctl is-enabled msi-fan-profile.service) == enabled ]]
[[ $(stat -c '%U:%G:%a' /run/msi-fanctl.lock) == root:root:600 ]]
[[ $(stat -c '%U:%G:%a' /run/msi-fan-profile-manager.lock) == root:root:600 ]]

trap - ERR INT TERM
installed=0
cat <<'EOF'
Profile files installed and enabled, but not started.
Start and verify with:
  sudo msi-fan-profile apply-default
  msi-fan-profile status
EOF
