#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

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
systemctl stop msi-fan-profile.service 2>/dev/null || true
systemctl reset-failed msi-fan-profile.service 2>/dev/null || true
[[ $(systemctl show msi-fan-profile.service -p ActiveState --value) == inactive ]]
[[ $(systemctl show msi-fan-profile.service -p SubState --value) == dead ]]
[[ $(systemctl show msi-fan-profile.service -p MainPID --value) == 0 ]]
[[ -z $(systemctl show msi-fan-profile.service -p Job --value) ]]

# The daemon has exited and released the fan lock.
exec 8</run/msi-fanctl.lock
flock -x 8

# Exact identity gate. Do not write a model-specific curve on any mismatch.
[[ $(cat /sys/class/dmi/id/product_name) == 'Raider 18 HX AI A2XWIG' ]]
[[ $(cat /sys/class/dmi/id/board_name) == 'MS-1824' ]]
[[ $(cat /sys/class/dmi/id/bios_version) == 'E1824IMS.310' ]]
[[ $(cat /sys/module/msi_ec/version) == '0.13.1' ]]
[[ $(cat /sys/module/msi_ec/srcversion) == 'AB0BFAE2391B5ADD66E01BD' ]]
[[ $(cat "$base/fw_version") == '1824EMS1.108' ]]
if [[ -e /sys/module/ec_sys ]]; then
  echo 'Refusing uninstall recovery while ec_sys is loaded.' >&2
  exit 1
fi

# Restore complete factory envelope. Keep Boost on until cool verification.
printf 'on\n' > "$base/cooler_boost"
printf 'auto\n' > "$base/fan_mode"
printf '%s\n' "$factory" > "$base/fan_curve"
printf 'on\n' > "$base/cooler_boost"
[[ $(cat "$base/fan_mode") == auto ]]
[[ $(cat "$base/fan_curve") == "$factory" ]]
[[ $(cat "$base/cooler_boost") == on ]]

core=''
for h in /sys/class/hwmon/hwmon*; do
  [[ $(cat "$h/name" 2>/dev/null) == coretemp ]] && core=$h
done
[[ -n $core ]]
package=$(cat "$core/temp1_input")
ec_cpu=$(cat "$base/cpu/realtime_temperature")
ec_gpu=$(cat "$base/gpu/realtime_temperature")
(( package < 85000 && ec_cpu < 85 && (ec_gpu == 0 || ec_gpu < 70) )) || {
  echo 'Factory restored, but machine is too hot to release Cooler Boost.' >&2
  echo 'Files were not removed. Cool down and retry.' >&2
  exit 1
}
printf 'off\n' > "$base/cooler_boost"
if [[ $(cat "$base/fan_mode" 2>/dev/null) != auto ||
      $(cat "$base/fan_curve" 2>/dev/null) != "$factory" ||
      $(cat "$base/cooler_boost" 2>/dev/null) != off ]]; then
  printf 'on\n' > "$base/cooler_boost" 2>/dev/null || true
  echo 'Final factory auto/off verification failed; Boost re-requested.' >&2
  exit 1
fi

rm -f /etc/systemd/system/msi-fan-profile.service
rm -f /etc/tmpfiles.d/msi-fan-profile.conf
rm -f /usr/local/sbin/msi-fan-profile
rm -f /usr/local/libexec/msi-fan-profiled
rm -f /usr/local/libexec/msi-gpu-recover
rm -rf /usr/local/share/doc/msi-fan-profile
rm -f /run/msi-fan-profile.factory-auto /run/msi-fan-profile.ack
rm -f /run/msi-fan-profile-manager.lock /run/msi-fanctl.lock
systemctl daemon-reload

echo 'Profile service removed; exact factory auto/off verified.'
echo 'The patched msi_ec driver remains installed.'
