#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
service=msi-fan-profile.service
keeper=msi-fan-profile-keeper.service
manager=/usr/local/sbin/msi-fan-profile
marker=/run/msi-fan-profile.factory-auto
recovery_latch=/run/msi-fan-profile.recovery-boost
recovery_latch_payload=candidate14
base=/sys/devices/platform/msi-ec
hwmon=/sys/class/hwmon
factory='58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75'

# The installed manager's factory-auto refuses to release Cooler Boost at a
# coretemp package or EC CPU temperature of 85 C or more, or an EC GPU
# temperature of 70 C or more (EC GPU 0 is a powered-off dGPU), and managers
# before this release then also disable the service. The release checks heat
# the CPU, so the handover waits for 5 C under each limit over consecutive
# one-second samples. A sample is valid under the rules of the shared cool
# confirmation in msi_fan_control.py, and an invalid one restarts the count.
# Releases before this one still decide from one reading, which a spike can
# refuse even right after this gate; the full-cooling continuation after
# factory-auto covers that, and the gate keeps it the exception.
cool_package_below=80000
cool_ec_cpu_below=80
cool_ec_gpu_below=65
cool_samples=10
cool_limits="CPU package $((cool_package_below / 1000)) C, EC CPU $cool_ec_cpu_below C and EC GPU $cool_ec_gpu_below C"
cool_timeout=${MSI_FAN_UPGRADE_COOL_TIMEOUT-600}

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ -x $manager ]] || { echo "Existing manager is missing: $manager" >&2; exit 1; }
[[ $(stat -c '%F:%U:%G:%a' "$manager") == regular\ file:root:root:755 ]] || {
  echo "Existing manager has unsafe ownership or mode: $manager" >&2
  exit 1
}
if [[ ! $cool_timeout =~ ^[1-9][0-9]{0,4}$ ]] ||
    ((cool_timeout < cool_samples || cool_timeout > 86400)); then
  printf 'MSI_FAN_UPGRADE_COOL_TIMEOUT must be a whole number of seconds from %d to 86400, not %q.\n' \
    "$cool_samples" "$cool_timeout" >&2
  exit 1
fi

# Validate the exact platform and the source release before touching the installed
# controller. This also prevents a mismatched digest manifest from being deployed.
# The release checks, including the unit tests, run without root.
"$repo/scripts/preflight.sh"
"$repo/scripts/verify-release-unprivileged.sh"

source_files=(
  "$repo/src/msi-fan-profile"
  "$repo/src/msi-fan-profiled"
  "$repo/src/msi-gpu-recover"
  "$repo/src/msi_fan_control.py"
  "$repo/scripts/check-kernel-readiness.sh"
  "$repo/kernel/postinst.d/zz-msi-fan-profile-readiness"
  "$repo/systemd/msi-fan-profile.service"
  "$repo/systemd/msi-fan-profile-keeper.service"
  "$repo/tmpfiles/msi-fan-profile.conf"
  "$repo/README.md"
  "$repo/docs/OPERATIONS.md"
  "$repo/docs/SAFETY.md"
)
target_files=(
  /usr/local/sbin/msi-fan-profile
  /usr/local/libexec/msi-fan-profiled
  /usr/local/libexec/msi-gpu-recover
  /usr/local/libexec/msi_fan_control.py
  /usr/local/libexec/msi-fan-kernel-readiness
  /etc/kernel/postinst.d/zz-msi-fan-profile-readiness
  /etc/systemd/system/msi-fan-profile.service
  /etc/systemd/system/msi-fan-profile-keeper.service
  /etc/tmpfiles.d/msi-fan-profile.conf
  /usr/local/share/doc/msi-fan-profile/README.md
  /usr/local/share/doc/msi-fan-profile/OPERATIONS.md
  /usr/local/share/doc/msi-fan-profile/SAFETY.md
)
modes=(0755 0755 0755 0644 0755 0755 0644 0644 0644 0644 0644 0644)
must_exist=(yes yes yes no no no yes no yes no no no)
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
factory_transition=0
factory_verify=0
boost_retained=0
enabled_for_startup=0
activation_started=0

cleanup() {
  local rc=$?
  trap - EXIT
  [[ -n $stage && -d $stage ]] && rm -rf -- "$stage"
  if ((rc != 0 && activation_started)); then
    # Once activation has begun, the manager and systemd recovery own cooling.
    # A failed start or status check may already have staged the full-cooling
    # keeper; an upgrade rollback must not stop it or release Cooler Boost.
    echo 'Upgrade stopped after activation; runtime cooling and recovery were left untouched. Inspect controller and keeper status.' >&2
    echo "Previous artifacts are retained at: $backup" >&2
  elif ((rc != 0 && enabled_for_startup)); then
    systemctl disable "$service" >/dev/null 2>&1 || true
    if ((boost_retained)); then
      echo "Upgrade stopped before activation; the service was disabled and factory auto mode remains in effect. $(retained_cooling)" >&2
    else
      echo 'Upgrade stopped before activation; the service was disabled and factory auto mode remains in effect.' >&2
    fi
  elif ((rc != 0 && safe_state)); then
    systemctl disable "$service" >/dev/null 2>&1 || true
    if ((boost_retained)); then
      echo "Upgrade stopped; the service remains disabled in factory auto mode. $(retained_cooling)" >&2
    else
      echo 'Upgrade stopped; the service remains disabled in factory auto mode.' >&2
    fi
  elif ((rc != 0 && factory_verify)); then
    # The existing manager reported factory auto/off, or left factory auto with
    # Boost on in the verified full-cooling state, but disabling the service or
    # a later unit, marker, cooling or latch check failed. Nothing was replaced;
    # keep the service disabled, as this window intended.
    systemctl disable "$service" >/dev/null 2>&1 || true
    if ((boost_retained)); then
      echo "Upgrade stopped before replacing any artifact: the installed manager left factory auto with Cooler Boost on, but a later service, marker, cooling or recovery Boost latch check failed. $(retained_cooling)" >&2
    else
      echo 'Upgrade stopped before replacing any artifact: the existing manager reported factory auto/off, but a later service, marker, cooling or recovery Boost latch check failed.' >&2
    fi
    echo "The service was disabled, so the installed profile will not return at boot. Inspect both units, $marker and $recovery_latch, resolve the failed check, then run 'sudo $manager apply-default' before rebooting and rerun this upgrade." >&2
  elif ((rc != 0 && factory_transition)); then
    # Nothing was replaced. Leave whatever cooling and boot enablement the
    # existing manager reported: a refused hot Boost release keeps Boost on.
    echo 'Upgrade stopped before replacing any artifact: the existing manager did not reach verified factory auto/off.' >&2
    echo 'If it was too hot to release Cooler Boost, let the machine cool and rerun this upgrade.' >&2
    echo "If a cool retry still fails, or to resume the installed profile now, run 'sudo $manager apply-default' and rerun this upgrade when cool." >&2
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

# Describe the tree the release checks verified and the stage now holds, not
# whatever the checkout holds after the cool-down wait.
commit=$(git -C "$repo" rev-parse --verify HEAD 2>/dev/null || printf 'unavailable')
if [[ -n $(git -C "$repo" status --porcelain 2>/dev/null || true) ]]; then
  dirty=yes
else
  dirty=no
fi
manifest_sum=$(sha256sum "$repo/SHA256SUMS" | awk '{print $1}')

# Read one sysfs attribute into reading; empty if it is missing or unreadable.
read_attribute() {
  reading=''
  { IFS= read -r reading < "$1"; } 2>/dev/null || true
}

# Succeed if $1 is a decimal reading from $2 to $3.
valid_reading() {
  [[ $1 =~ ^[0-9]{1,6}$ ]] && ((10#$1 >= $2 && 10#$1 <= $3))
}

# An unreadable, non-numeric or out-of-range value, such as the EC's 255
# sentinel, or a CPU at 0 C, never counts as cool.
readings_valid() {
  valid_reading "$package" 1 110000 && valid_reading "$ec_cpu" 1 110 &&
    valid_reading "$ec_gpu" 0 110
}

# Sample the sensors the installed manager checks, from the same sources, into
# package, ec_cpu and ec_gpu. Succeed only if each is valid and below its cool
# limit.
sample_cooling() {
  local name cores=()
  for name in "$hwmon"/hwmon*/name; do
    read_attribute "$name"
    [[ $reading != coretemp ]] || cores+=("${name%/name}")
  done
  package=''
  # Like the manager, require exactly one coretemp device.
  if ((${#cores[@]} == 1)); then
    read_attribute "${cores[0]}/temp1_input"
    package=$reading
  fi
  read_attribute "$base/cpu/realtime_temperature"
  ec_cpu=$reading
  read_attribute "$base/gpu/realtime_temperature"
  ec_gpu=$reading
  readings_valid &&
    ((10#$package < cool_package_below && 10#$ec_cpu < cool_ec_cpu_below &&
      10#$ec_gpu < cool_ec_gpu_below))
}

# Describe one reading in degrees C: label, value, valid range, units per degree.
describe_reading() {
  if valid_reading "$2" "$3" "$4"; then
    printf '%s %d.%d C' "$1" "$((10#$2 / $5))" "$((10#$2 % $5 * 10 / $5))"
  elif [[ -z $2 ]]; then
    printf '%s unreadable' "$1"
  else
    printf '%s invalid (%q)' "$1" "$2"
  fi
}

cooling_readings() {
  printf '%s, %s, %s' "$(describe_reading 'CPU package' "$package" 1 110000 1000)" \
    "$(describe_reading 'EC CPU' "$ec_cpu" 1 110 1)" \
    "$(describe_reading 'EC GPU' "$ec_gpu" 0 110 1)"
}

# Hand over to the installed manager only once it can release Cooler Boost, so
# a refused hot release cannot stop (and, before this release, disable) the
# controller. Nothing on the running system has changed yet.
cool=0
waited=0
waiting=0
gate_started=$SECONDS
while :; do
  if sample_cooling; then
    cool=$((cool + 1))
    ((cool < cool_samples)) || break
  else
    cool=0
    if ((!waiting)); then
      waiting=1
      echo "Waiting up to $cool_timeout s for the machine to cool below $cool_limits, 5 C under the installed manager's Cooler Boost release limits: $(cooling_readings)"
    fi
  fi
  if ((waited >= cool_timeout || SECONDS - gate_started >= cool_timeout)); then
    echo "Upgrade not started: the machine did not cool below $cool_limits within $cool_timeout s; stop heavy workloads and rerun." >&2
    echo "Last readings: $(cooling_readings). No installed file, service or fan setting was changed." >&2
    readings_valid ||
      echo 'An unreadable or invalid reading never counts as cool; check the msi-ec and coretemp sensors before rerunning.' >&2
    exit 1
  fi
  if ((waited > 0 && waited % 30 == 0)); then
    echo "Still waiting for cool-down after $waited s ($cool of $cool_samples consecutive cool samples): $(cooling_readings)"
  fi
  sleep 1
  waited=$((waited + 1))
done
if ((waiting)); then
  echo "Cooled below $cool_limits after $waited s: $(cooling_readings)"
fi

# The wait can last minutes, during which the operator may pull or edit the
# checkout. The post-install check compares the deployed artifacts with the
# source, so hand over only while the source still matches the verified stage;
# otherwise that check would fail after factory-auto and the replacement.
for index in "${!source_files[@]}"; do
  cmp -s "${source_files[$index]}" "$stage${target_files[$index]}" || {
    echo "Upgrade not started: ${source_files[$index]} changed during the cool-down wait. No installed file, service or fan setting was changed; rerun." >&2
    exit 1
  }
done

# The recovery Boost latch's ownership and mode, as the daemon requires: a
# root-owned, mode-0600 regular file with one link, not a symlink.
recovery_latch_safe() {
  [[ ! -L $recovery_latch ]] &&
    [[ $(stat -c '%F:%U:%G:%a:%h' "$recovery_latch") == 'regular file:root:root:600:1' ]]
}

# A latch the new daemon consumes: safe, with exactly the payload it accepts.
recovery_latch_valid() {
  [[ -e $recovery_latch ]] && recovery_latch_safe &&
    cmp -s -- "$recovery_latch" <(printf '%s\n' "$recovery_latch_payload")
}

refuse_full_cooling() {
  echo "Not continuing at full cooling: $1." >&2
  return 1
}

# After continuing at full cooling, an exit message reports what is there now,
# not what was intended: the failed check may be exactly that Cooler Boost or
# the recovery Boost latch changed. Only reads; the script writes neither.
retained_cooling() {
  local boost latch
  boost=$(cat "$base/cooler_boost" 2>/dev/null || printf 'unreadable')
  if recovery_latch_valid 2>/dev/null; then
    latch=present
  elif [[ -e $recovery_latch || -L $recovery_latch ]]; then
    latch='present but not valid'
  else
    latch=missing
  fi
  printf 'This script did not change Cooler Boost or the recovery Boost latch: cooler_boost is %s and %s is %s.' \
    "${boost:-empty}" "$recovery_latch" "$latch"
}

# Earlier releases decide a Boost release from one reading, which a spike can
# refuse even right after the cool-down gate. Their factory-auto then leaves
# the factory curve, auto mode and Cooler Boost on, both units stopped, the
# factory-auto marker and the recovery Boost latch (managers before this
# release also disable the service). That is full cooling with no controller
# running, the safest state in which to replace files, and the new daemon
# consumes the latch and releases Boost only after 30 continuously valid, cool
# seconds. Succeed only if exactly that state verifies; every check only reads.
factory_boost_on_verified() {
  local unit
  echo "Checking the exact platform and whether the installed manager left factory auto with Cooler Boost on and no controller running."
  "$repo/scripts/preflight.sh" || refuse_full_cooling 'the exact-platform check failed' || return 1
  for unit in "$service" "$keeper"; do
    [[ $(systemctl show "$unit" -p ActiveState --value 2>/dev/null || true) == inactive &&
       $(systemctl show "$unit" -p SubState --value 2>/dev/null || true) == dead ]] ||
      refuse_full_cooling "$unit is not inactive" || return 1
  done
  [[ -f $marker && ! -L $marker &&
     $(stat -c '%U:%G:%a' "$marker" 2>/dev/null || true) == root:root:600 ]] ||
    refuse_full_cooling "no root-owned, mode-0600 factory-auto marker $marker" || return 1
  [[ $(cat "$base/fan_curve" 2>/dev/null || true) == "$factory" ]] ||
    refuse_full_cooling 'the EC is not on the factory curve' || return 1
  [[ $(cat "$base/fan_mode" 2>/dev/null || true) == auto ]] ||
    refuse_full_cooling 'fan_mode is not auto' || return 1
  [[ $(cat "$base/cooler_boost" 2>/dev/null || true) == on ]] ||
    refuse_full_cooling 'Cooler Boost is not on' || return 1
  [[ -e $recovery_latch || -L $recovery_latch ]] ||
    refuse_full_cooling "no recovery Boost latch $recovery_latch" || return 1
  recovery_latch_valid ||
    refuse_full_cooling "the recovery Boost latch $recovery_latch is not a root-owned, mode-0600 file with the expected payload" ||
    return 1
}

# A reload clears stale manager metadata before the old manager takes the
# controller to its guarded factory-auto state.
systemctl daemon-reload
systemctl stop "$keeper" 2>/dev/null || true
systemctl reset-failed "$keeper" 2>/dev/null || true
factory_transition=1
factory_status=0
"$manager" factory-auto || factory_status=$?
if ((factory_status != 0)); then
  # Any other failure shape stops here, replacing nothing, as before.
  factory_boost_on_verified || exit "$factory_status"
  echo 'The installed manager left factory auto with Cooler Boost on (too hot or unconfirmed); continuing the upgrade at full cooling; the new controller will release Boost after its 30 s cool confirmation.' >&2
  boost_retained=1
fi
factory_transition=0
factory_verify=1
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
if ((boost_retained)); then
  # Keep the latch: the new daemon consumes it once it has reapplied Candidate
  # 14, and the inherited Boost becomes its automatic latch.
  [[ $(cat "$base/cooler_boost") == on ]]
  [[ $(systemctl show "$keeper" -p ActiveState --value) == inactive ]]
  recovery_latch_valid
else
  [[ $(cat "$base/cooler_boost") == off ]]
  if [[ -e $recovery_latch || -L $recovery_latch ]]; then
    recovery_latch_safe
    rm -f -- "$recovery_latch"
  fi
fi
safe_state=1

# Retain a root-only rollback snapshot, but never auto-restore it. Before
# activation a failed upgrade remains in verified factory-auto state; after
# activation the manager and runtime recovery retain responsibility for cooling.
install -d -o root -g root -m 0700 "$backup_root"
backup=$(mktemp -d "$backup_root/upgrade.XXXXXXXXXX")
for target in "${target_files[@]}" "$record_target"; do
  if [[ -e $target && ! -L $target ]]; then
    install -d -o root -g root -m 0700 "$backup/$(dirname "${target#/}")"
    cp -a -- "$target" "$backup/${target#/}"
  fi
done

# Replace all executable, kernel hook, unit, tmpfiles, and documentation
# artifacts from the verified stage while the controller is disabled and
# factory-safe: factory auto/off, or factory auto with Cooler Boost on.
for index in "${!target_files[@]}"; do
  target=${target_files[$index]}
  install -D -o root -g root -m "${modes[$index]}" "$stage$target" "$target"
done

systemd-tmpfiles --create /etc/tmpfiles.d/msi-fan-profile.conf
[[ $(stat -c '%U:%G:%a' /run/msi-fanctl.lock) == root:root:600 ]]
[[ $(stat -c '%U:%G:%a' /run/msi-fan-profile-manager.lock) == root:root:600 ]]
systemd-analyze verify /etc/systemd/system/msi-fan-profile.service \
  /etc/systemd/system/msi-fan-profile-keeper.service
systemctl daemon-reload
[[ $(systemctl show "$service" -p NeedDaemonReload --value) == no ]]

for index in "${!source_files[@]}"; do
  source_sum=$(sha256sum "${source_files[$index]}" | awk '{print $1}')
  target_sum=$(sha256sum "${target_files[$index]}" | awk '{print $1}')
  [[ $source_sum == "$target_sum" ]]
  expected_mode=${modes[$index]#0}
  [[ $(stat -c '%U:%G:%a' "${target_files[$index]}") == "root:root:$expected_mode" ]]
done

{
  printf 'source_commit=%s\n' "$commit"
  printf 'source_tree_dirty=%s\n' "$dirty"
  printf 'source_manifest_sha256=%s\n' "$manifest_sum"
  for index in "${!source_files[@]}"; do
    printf '%s  %s\n' "$(sha256sum "${target_files[$index]}" | awk '{print $1}')" \
      "${target_files[$index]}"
  done
} > "$stage/INSTALL_RECORD"
install -o root -g root -m 0644 "$stage/INSTALL_RECORD" "$record_target"

# Activate only through the new manager, which verifies Candidate14's complete
# invariant and leaves post-start runtime recovery untouched if activation fails.
systemctl enable "$service"
[[ $(systemctl is-enabled "$service") == enabled ]]
enabled_for_startup=1
activation_started=1
"$manager" apply-default
"$manager" status

echo "Upgrade complete. Previous artifacts are retained at: $backup"
if ((boost_retained)); then
  echo "Cooler Boost stays on as the new controller's automatic latch until it has seen 30 continuously valid, cool seconds."
fi
