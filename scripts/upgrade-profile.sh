#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
service=msi-fan-profile.service
manager=/usr/local/sbin/msi-fan-profile
marker=/run/msi-fan-profile.factory-auto
base=/sys/devices/platform/msi-ec
factory='58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75'

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ -x $manager ]] || { echo "Existing manager is missing: $manager" >&2; exit 1; }
[[ $(stat -c '%F:%U:%G:%a' "$manager") == regular\ file:root:root:755 ]] || {
  echo "Existing manager has unsafe ownership or mode: $manager" >&2
  exit 1
}

# Validate the exact platform and the source release before touching the installed
# controller. This also prevents a mismatched digest manifest from being deployed.
"$repo/scripts/preflight.sh"
"$repo/scripts/verify-release.sh"

source_files=(
  "$repo/src/msi-fan-profile"
  "$repo/src/msi-fan-profiled"
  "$repo/src/msi-gpu-recover"
  "$repo/systemd/msi-fan-profile.service"
  "$repo/tmpfiles/msi-fan-profile.conf"
  "$repo/README.md"
  "$repo/docs/OPERATIONS.md"
  "$repo/docs/SAFETY.md"
)
target_files=(
  /usr/local/sbin/msi-fan-profile
  /usr/local/libexec/msi-fan-profiled
  /usr/local/libexec/msi-gpu-recover
  /etc/systemd/system/msi-fan-profile.service
  /etc/tmpfiles.d/msi-fan-profile.conf
  /usr/local/share/doc/msi-fan-profile/README.md
  /usr/local/share/doc/msi-fan-profile/OPERATIONS.md
  /usr/local/share/doc/msi-fan-profile/SAFETY.md
)
modes=(0755 0755 0755 0644 0644 0644 0644 0644)
must_exist=(yes yes yes yes yes no no no)
record_target=/usr/local/share/doc/msi-fan-profile/INSTALL_RECORD

for index in "${!source_files[@]}"; do
  [[ -f ${source_files[$index]} ]] || {
    echo "Missing source artifact: ${source_files[$index]}" >&2
    exit 1
  }
  [[ ! -L ${target_files[$index]} ]] || {
    echo "Refusing upgrade: installed artifact is a symlink: ${target_files[$index]}" >&2
    exit 1
  }
  if [[ ${must_exist[$index]} == yes && ! -f ${target_files[$index]} ]]; then
    echo "Refusing upgrade: required artifact is missing: ${target_files[$index]}" >&2
    exit 1
  fi
done

stage=$(mktemp -d /run/msi-fan-profile-upgrade.XXXXXXXXXX)
backup_root=/var/lib/msi-fan-profile/upgrade-backups
backup=''
safe_state=0
enabled_for_startup=0
activation_started=0

cleanup() {
  local rc=$?
  trap - EXIT
  [[ -n $stage && -d $stage ]] && rm -rf -- "$stage"
  if ((rc != 0 && activation_started)); then
    if "$manager" factory-auto >/dev/null 2>&1 && \
        systemctl disable "$service" >/dev/null 2>&1; then
      echo 'Upgrade stopped after activation; factory auto mode is restored and the service is disabled.' >&2
    else
      echo 'Upgrade stopped after activation; guarded factory fallback could not be verified. Inspect cooling immediately.' >&2
    fi
  elif ((rc != 0 && enabled_for_startup)); then
    systemctl disable "$service" >/dev/null 2>&1 || true
    echo 'Upgrade stopped before activation; the service was disabled and factory auto mode remains in effect.' >&2
  elif ((rc != 0 && safe_state)); then
    systemctl disable "$service" >/dev/null 2>&1 || true
    echo 'Upgrade stopped; the service remains disabled in factory auto mode.' >&2
  fi
  exit "$rc"
}
trap cleanup EXIT

# Stage and byte-compare every artifact before changing the current deployment.
for index in "${!source_files[@]}"; do
  staged="$stage${target_files[$index]}"
  install -D -o root -g root -m "${modes[$index]}" \
    "${source_files[$index]}" "$staged"
  cmp -s "${source_files[$index]}" "$staged"
done

# A reload clears stale manager metadata before the old manager takes the
# controller to its guarded factory-auto state.
systemctl daemon-reload
"$manager" factory-auto
systemctl disable "$service"
systemctl stop "$service" 2>/dev/null || true
systemctl reset-failed "$service" 2>/dev/null || true

[[ $(systemctl is-enabled "$service" 2>/dev/null || true) == disabled ]]
[[ $(systemctl show "$service" -p ActiveState --value) == inactive ]]
[[ $(systemctl show "$service" -p SubState --value) == dead ]]
[[ -f $marker && ! -L $marker ]]
[[ $(stat -c '%U:%G:%a' "$marker") == root:root:600 ]]
[[ $(cat "$base/fan_curve") == "$factory" ]]
[[ $(cat "$base/fan_mode") == auto ]]
[[ $(cat "$base/cooler_boost") == off ]]
safe_state=1

# Retain a root-only rollback snapshot, but never auto-restore it: a failed
# upgrade should remain in verified factory-auto state rather than re-enable an
# uncertain controller release.
install -d -o root -g root -m 0700 "$backup_root"
backup=$(mktemp -d "$backup_root/upgrade.XXXXXXXXXX")
for target in "${target_files[@]}" "$record_target"; do
  if [[ -e $target && ! -L $target ]]; then
    install -d -o root -g root -m 0700 "$backup/$(dirname "${target#/}")"
    cp -a -- "$target" "$backup/${target#/}"
  fi
done

# Replace all executable, unit, tmpfiles, and documentation artifacts from the
# verified stage while the controller is disabled and factory-safe.
for index in "${!target_files[@]}"; do
  target=${target_files[$index]}
  install -D -o root -g root -m "${modes[$index]}" "$stage$target" "$target"
done

systemd-tmpfiles --create /etc/tmpfiles.d/msi-fan-profile.conf
[[ $(stat -c '%U:%G:%a' /run/msi-fanctl.lock) == root:root:600 ]]
[[ $(stat -c '%U:%G:%a' /run/msi-fan-profile-manager.lock) == root:root:600 ]]
systemd-analyze verify /etc/systemd/system/msi-fan-profile.service
systemctl daemon-reload
[[ $(systemctl show "$service" -p NeedDaemonReload --value) == no ]]

for index in "${!source_files[@]}"; do
  source_sum=$(sha256sum "${source_files[$index]}" | awk '{print $1}')
  target_sum=$(sha256sum "${target_files[$index]}" | awk '{print $1}')
  [[ $source_sum == "$target_sum" ]]
  expected_mode=${modes[$index]#0}
  [[ $(stat -c '%U:%G:%a' "${target_files[$index]}") == "root:root:$expected_mode" ]]
done

commit=$(git -C "$repo" rev-parse --verify HEAD 2>/dev/null || printf 'unavailable')
if [[ -n $(git -C "$repo" status --porcelain 2>/dev/null || true) ]]; then
  dirty=yes
else
  dirty=no
fi
{
  printf 'source_commit=%s\n' "$commit"
  printf 'source_tree_dirty=%s\n' "$dirty"
  printf 'source_manifest_sha256=%s\n' "$(sha256sum "$repo/SHA256SUMS" | awk '{print $1}')"
  for index in "${!source_files[@]}"; do
    printf '%s  %s\n' "$(sha256sum "${target_files[$index]}" | awk '{print $1}')" \
      "${target_files[$index]}"
  done
} > "$stage/INSTALL_RECORD"
install -o root -g root -m 0644 "$stage/INSTALL_RECORD" "$record_target"

# Activate only through the new manager, which verifies Candidate14's complete
# invariant and returns to factory-auto/Boost-on if activation cannot be proved.
systemctl enable "$service"
[[ $(systemctl is-enabled "$service") == enabled ]]
enabled_for_startup=1
activation_started=1
"$manager" apply-default
"$manager" status

echo "Upgrade complete. Previous artifacts are retained at: $backup"
