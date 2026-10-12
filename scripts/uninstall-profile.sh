#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
base=/sys/devices/platform/msi-ec
factory='58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75'

# Validate/create fixed root-owned runtime objects without following symlinks.
for lock_path in /run/msi-fan-profile-manager.lock /run/msi-fanctl.lock; do
  if [[ -e $lock_path || -L $lock_path ]]; then
    [[ ! -L $lock_path ]]
    [[ $(stat -c '%F:%U:%G:%a' "$lock_path") == 'regular empty file:root:root:600' ]]
  else
    install -o root -g root -m 0600 /dev/null "$lock_path"
  fi
done
# Serialize the whole marker/manager/service transition before creating the marker.
exec 9</run/msi-fan-profile-manager.lock
flock -x 9
if [[ -e /run/msi-fan-profile.factory-auto || -L /run/msi-fan-profile.factory-auto ]]; then
  [[ ! -L /run/msi-fan-profile.factory-auto ]]
  [[ $(stat -c '%F:%U:%G:%a' /run/msi-fan-profile.factory-auto) == 'regular empty file:root:root:600' ]]
else
  install -o root -g root -m 0600 /dev/null /run/msi-fan-profile.factory-auto
fi
systemctl disable msi-fan-profile.service
[[ $(systemctl is-enabled msi-fan-profile.service 2>/dev/null || true) == disabled ]]
systemctl stop msi-fan-profile-keeper.service 2>/dev/null || true
systemctl reset-failed msi-fan-profile-keeper.service 2>/dev/null || true
systemctl stop msi-fan-profile.service 2>/dev/null || true
systemctl reset-failed msi-fan-profile.service 2>/dev/null || true
[[ $(systemctl show msi-fan-profile.service -p ActiveState --value) == inactive ]]
[[ $(systemctl show msi-fan-profile.service -p SubState --value) == dead ]]
[[ $(systemctl show msi-fan-profile.service -p MainPID --value) == 0 ]]
[[ -z $(systemctl show msi-fan-profile.service -p Job --value) ]]
[[ $(systemctl show msi-fan-profile-keeper.service -p ActiveState --value) == inactive ]]
[[ $(systemctl show msi-fan-profile-keeper.service -p SubState --value) == dead ]]
[[ $(systemctl show msi-fan-profile-keeper.service -p MainPID --value) == 0 ]]
[[ -z $(systemctl show msi-fan-profile-keeper.service -p Job --value) ]]

# The daemon has exited and released the fan lock.
exec 8</run/msi-fanctl.lock
flock -x 8

# Exact identity gate. Do not write a model-specific value on any mismatch.
# Callers test the status: set -e does not apply inside a function that is
# called as a condition, so every check returns explicitly.
exact_identity() {
  local module_source
  [[ $(cat /sys/class/dmi/id/product_name) == 'Raider 18 HX AI A2XWIG' ]] || return 1
  [[ $(cat /sys/class/dmi/id/board_name) == 'MS-1824' ]] || return 1
  [[ $(cat /sys/class/dmi/id/bios_version) == 'E1824IMS.310' ]] || return 1
  [[ $(cat /sys/module/msi_ec/version) == '0.13.1' ]] || return 1
  module_source=$(cat /sys/module/msi_ec/srcversion) || return 1
  case "$module_source" in
    AB0BFAE2391B5ADD66E01BD) ;;
    9086C45007CBB7FA1264430) [[ -r $base/fan_control_snapshot ]] || return 1 ;;
    *) echo 'Refusing uninstall recovery on an unsupported driver build.' >&2; return 1 ;;
  esac
  [[ $(cat "$base/fw_version") == '1824EMS1.108' ]] || return 1
  if [[ -e /sys/module/ec_sys ]]; then
    echo 'Refusing uninstall recovery while ec_sys is loaded.' >&2
    return 1
  fi
}
exact_identity || exit 1

# Restore complete factory envelope. Keep Boost on until cool verification.
printf 'on\n' > "$base/cooler_boost"
printf 'auto\n' > "$base/fan_mode"
printf '%s\n' "$factory" > "$base/fan_curve"
printf 'on\n' > "$base/cooler_boost"
[[ $(cat "$base/fan_mode") == auto ]]
[[ $(cat "$base/fan_curve") == "$factory" ]]
[[ $(cat "$base/cooler_boost") == on ]]

# One reading never releases Boost. The shared, read-only helper requires
# consecutive valid, cool samples, exactly as the controllers do; an empty,
# junk or hot reading restarts the count, and it gives up within 10 seconds.
# The identity gate above ran up to 10 seconds earlier by then, so check it
# again before the next EC write; a changed driver also supplied the readings.
if ! /usr/bin/python3 -I -B "$repo/src/msi_fan_control.py" confirm-cool \
    /sys/class/hwmon "$base"; then
  if exact_identity; then
    printf 'on\n' > "$base/cooler_boost" 2>/dev/null || true
  fi
  echo 'Factory restored, but machine is too hot to release Cooler Boost, or its temperature telemetry is invalid.' >&2
  echo 'Files were not removed. Cool down and retry.' >&2
  exit 1
fi
if ! exact_identity; then
  echo 'The exact platform or driver identity changed during the cool confirmation; nothing was written after it, so Cooler Boost was not released.' >&2
  echo 'Files were not removed. Check msi_ec and ec_sys, then retry.' >&2
  exit 1
fi
printf 'off\n' > "$base/cooler_boost"
if [[ $(cat "$base/fan_mode" 2>/dev/null) != auto ||
      $(cat "$base/fan_curve" 2>/dev/null) != "$factory" ||
      $(cat "$base/cooler_boost" 2>/dev/null) != off ]]; then
  printf 'on\n' > "$base/cooler_boost" 2>/dev/null || true
  echo 'Final factory auto/off verification failed; Boost re-requested.' >&2
  exit 1
fi

rm -f /etc/systemd/system/msi-fan-profile.service
rm -f /etc/systemd/system/msi-fan-profile-keeper.service
rm -f /etc/tmpfiles.d/msi-fan-profile.conf
rm -f /etc/kernel/postinst.d/zz-msi-fan-profile-readiness
rm -f /usr/local/sbin/msi-fan-profile
rm -f /usr/local/libexec/msi-fan-profiled
rm -f /usr/local/libexec/msi-gpu-recover
rm -f /usr/local/libexec/msi_fan_control.py
rm -f /usr/local/libexec/msi-fan-kernel-readiness
rm -rf /usr/local/share/doc/msi-fan-profile
rm -f /run/msi-fan-profile.factory-auto /run/msi-fan-profile.ack /run/msi-fan-profile.recovery-boost /run/msi-fan-profile.health.json
rm -f /run/msi-fan-profile-manager.lock /run/msi-fanctl.lock
systemctl daemon-reload

echo 'Profile service removed; exact factory auto/off verified.'
echo 'The patched msi_ec driver remains installed.'
