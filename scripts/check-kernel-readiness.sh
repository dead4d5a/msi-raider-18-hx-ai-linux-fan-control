#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Read-only check, before rebooting, that a kernel will load the pinned, signed
# msi_ec build. A DKMS signing failure does not fail a kernel installation, so
# an unsigned module or one signed with an unenrolled key is found only here or
# after the reboot. Installed as /usr/local/libexec/msi-fan-kernel-readiness and
# run by the kernel postinst hook; it never writes, builds, loads or signs.
set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 [KERNEL_RELEASE]

Check that KERNEL_RELEASE (default: the /boot/vmlinuz target, else the newest
installed kernel) has the pinned msi_ec 0.13.1 DKMS build, signed with the
enrolled MOK, and the modules the controller needs. Exit 0 only without FAIL.
Runs unprivileged; only the MOK certificate and EFI enrollment checks need sudo,
and only if they report that they cannot read their inputs.
EOF
}

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
modules_root=/lib/modules
boot_dir=/boot
dkms_tree=/var/lib/dkms
mok_certificate=/var/lib/shim-signed/mok/MOK.der
module_version=0.13.1

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac
if (($# > 1)); then
    usage >&2
    exit 2
fi

failed=0
pass() { printf 'PASS %-20s %s\n' "$1" "$2"; }
fail() { printf 'FAIL %-20s %s\n' "$1" "$2"; failed=1; }
warn() { printf 'WARN %-20s %s\n' "$1" "$2"; }

installed_kernels() {
    local image
    for image in "$boot_dir"/vmlinuz-*; do
        if [[ -f $image ]]; then
            printf '%s\n' "${image#"$boot_dir"/vmlinuz-}"
        fi
    done | sort -V
}

# modinfo prints the signing certificate's serial as colon-separated hex.
normalized_serial() {
    local value=${1//:/}
    value=${value^^}
    while [[ $value == 0?* ]]; do
        value=${value#0}
    done
    printf '%s' "$value"
}

if (($# == 1)); then
    kernel_release=$1
    target_source=argument
else
    image=$(readlink -f -- "$boot_dir/vmlinuz" 2>/dev/null || true)
    if [[ $image == "$boot_dir"/vmlinuz-* && -f $image ]]; then
        kernel_release=${image#"$boot_dir"/vmlinuz-}
        target_source="$boot_dir/vmlinuz"
    else
        kernel_release=$(installed_kernels | tail -n 1)
        target_source='newest installed kernel'
    fi
fi
[[ $kernel_release =~ ^[[:alnum:]][[:alnum:]_.+-]*$ ]] || {
    echo "Invalid or missing kernel release: <$kernel_release>" >&2
    exit 2
}
printf 'msi_ec readiness for kernel %s (%s)\n' "$kernel_release" "$target_source"
if ((EUID != 0)); then
    echo 'Unprivileged run: only the MOK certificate and enrollment checks may need sudo.'
fi

# Exact runtime pins come from the shared identity helper, never a copy.
if [[ -f $here/msi_fan_control.py ]]; then
    helper_dir=$here
else
    helper_dir=$here/../src
fi
declare -A pinned=()
if pins=$(/usr/bin/python3 -I -B -c '
import sys
sys.path.insert(0, sys.argv[1])
import msi_fan_control as pins
names = {pins.LEGACY_SRCVERSION: "legacy", pins.SNAPSHOT_SRCVERSION: "snapshot"}
for source in pins.SUPPORTED_SRCVERSIONS:
    print(source, names.get(source, "pinned"))
' "$helper_dir" 2>/dev/null) && [[ -n $pins ]]; then
    while read -r pin label; do
        pinned[$pin]=$label
    done <<<"$pins"
else
    fail driver_pins "cannot read SUPPORTED_SRCVERSIONS from $helper_dir/msi_fan_control.py"
fi

sb_state=$(mokutil --sb-state 2>&1 || true)
case "$sb_state" in
    *'SecureBoot enabled'*)
        secure_boot=enabled
        pass secure_boot 'enabled: the module must be signed with an enrolled key'
        ;;
    *'SecureBoot disabled'*)
        secure_boot=disabled
        warn secure_boot 'disabled: signing problems are reported as WARN'
        ;;
    *)
        secure_boot=unknown
        warn secure_boot 'state unknown: signing is checked as if enabled'
        ;;
esac
# Lockdown refuses an unsigned or untrusted module only under Secure Boot.
signing_fail() {
    if [[ $secure_boot == disabled ]]; then
        warn "$@"
    else
        fail "$@"
    fi
}

[[ -d $modules_root/$kernel_release ]] ||
    fail kernel_modules "missing $modules_root/$kernel_release: kernel is not installed"

# DKMS reports a build failure, but not a signing failure, to the kernel
# installation. "installed" alone therefore proves neither signature nor key.
dkms_installed=()
target_status=''
if dkms_status=$(dkms status -m msi_ec 2>/dev/null); then
    while IFS= read -r line; do
        if [[ $line == "msi_ec/$module_version, $kernel_release, "* ]]; then
            target_status=$line
        fi
        if [[ $line =~ ^msi_ec/${module_version//./\\.},\ ([^,:]+),\ [^,:]+:\ installed$ ]]; then
            dkms_installed+=("${BASH_REMATCH[1]}")
        fi
    done <<<"$dkms_status"
else
    dkms_status=''
    fail dkms_status 'dkms status -m msi_ec failed'
fi
has_dkms_build() {
    local release
    for release in "${dkms_installed[@]}"; do
        [[ $release == "$1" ]] && return 0
    done
    return 1
}
if has_dkms_build "$kernel_release"; then
    pass dkms_build "msi_ec/$module_version installed for $kernel_release"
elif [[ -n $target_status ]]; then
    fail dkms_build "$target_status"
else
    fail dkms_build "msi_ec/$module_version has no build for $kernel_release"
fi

module_path=$(modinfo -k "$kernel_release" -n msi_ec 2>/dev/null || true)
dkms_directory=$modules_root/$kernel_release/updates/dkms/
if [[ -z $module_path ]]; then
    fail module_path "no msi_ec module for $kernel_release"
elif [[ $module_path == "$dkms_directory"* &&
        ${module_path#"$dkms_directory"} =~ ^msi-ec\.ko(\.(zst|xz|gz))?$ ]]; then
    pass module_path "$module_path"
else
    fail module_path "$module_path is not the DKMS build; the in-tree msi_ec rejects this EC firmware"
fi

dependencies=$modules_root/$kernel_release/modules.dep
if [[ -r $dependencies ]]; then
    mapfile -t entries < <(awk -F: '{
        count = split($1, parts, "/")
        if (parts[count] ~ /^msi-ec\.ko(\.(zst|xz|gz))?$/) print $1
    }' "$dependencies")
    if ((${#entries[@]} == 1)) && [[ ${entries[0]} == updates/dkms/* &&
            $modules_root/$kernel_release/${entries[0]} == "$module_path" ]]; then
        pass modules_dep "one msi-ec entry: ${entries[0]}"
    else
        fail modules_dep "expected one updates/dkms msi-ec entry, found ${#entries[@]}: ${entries[*]}"
    fi
else
    fail modules_dep "unreadable: $dependencies"
fi

# DKMS signs with this certificate's key and, if the pair is missing, silently
# with a newly generated one that is not enrolled.
serial=''
if [[ ! -e $mok_certificate ]]; then
    signing_fail mok_certificate "missing $mok_certificate; DKMS signs with a new, unenrolled key"
elif [[ ! -r $mok_certificate ]]; then
    signing_fail mok_certificate "cannot read $mok_certificate; re-run with sudo"
else
    serial=$(openssl x509 -inform DER -in "$mok_certificate" -noout -serial 2>/dev/null || true)
    serial=${serial#serial=}
    [[ -n $serial ]] || signing_fail mok_certificate "cannot read the serial of $mok_certificate"
fi

field() { modinfo -F "$1" "$module_path" 2>/dev/null || true; }
if [[ -n $module_path ]]; then
    version=$(field version)
    if [[ $version == "$module_version" ]]; then
        pass version "$version"
    else
        fail version "got <$version>, expected <$module_version>"
    fi
    srcversion=$(field srcversion)
    if [[ -n $srcversion && -n ${pinned[$srcversion]:-} ]]; then
        pass srcversion "$srcversion (pinned ${pinned[$srcversion]} build)"
    else
        fail srcversion "unpinned build <$srcversion>; the controller will refuse it"
    fi
    vermagic=$(field vermagic)
    if [[ $vermagic == "$kernel_release "* ]]; then
        pass vermagic "$vermagic"
    else
        fail vermagic "<$vermagic> does not start with $kernel_release"
    fi

    sig_key=$(field sig_key)
    signer=$(field signer)
    if [[ -z $sig_key ]]; then
        signing_fail module_signature 'module is unsigned; Secure Boot lockdown will refuse to load it'
    elif [[ -z $serial ]]; then
        signing_fail module_signature "signed by $signer ($sig_key); cannot compare with the MOK certificate"
    elif [[ $(normalized_serial "$sig_key") == "$(normalized_serial "$serial")" ]]; then
        pass module_signature "signed by $signer with the MOK certificate ($sig_key)"
    else
        signing_fail module_signature "signed by $signer ($sig_key), not the MOK certificate (serial $serial)"
    fi
fi

if [[ -r $mok_certificate ]] && ! command -v mokutil >/dev/null; then
    signing_fail mok_enrolled 'mokutil is not installed; cannot read the enrollment state'
elif [[ -r $mok_certificate ]]; then
    test_key=$(mokutil --test-key "$mok_certificate" 2>&1 || true)
    if [[ $test_key == *'is already enrolled'* ]]; then
        pass mok_enrolled "$mok_certificate"
    else
        fingerprint=$(openssl x509 -inform DER -in "$mok_certificate" -noout -fingerprint -sha1 2>/dev/null || true)
        fingerprint=${fingerprint#*=}
        enrolled=$(mokutil --list-enrolled 2>/dev/null || true)
        if [[ -n $fingerprint && ${enrolled,,} == *"sha1 fingerprint: ${fingerprint,,}"* ]]; then
            pass mok_enrolled "$mok_certificate (listed as enrolled)"
        else
            summary=$(printf '%s\n' "$test_key" | grep -m 1 -F "$mok_certificate" || true)
            signing_fail mok_enrolled "not enrolled: ${summary:-no enrollment state; re-run with sudo}"
        fi
    fi
fi

for name in msi_wmi_platform coretemp; do
    path=$(modinfo -k "$kernel_release" -n "$name" 2>/dev/null || true)
    if [[ $path == "$modules_root/$kernel_release/"* || $path == '(builtin)' ]]; then
        pass "$name" "$path"
    else
        fail "$name" "not available for $kernel_release"
    fi
done

# A blacklist, install or options override would stop or alter a module the
# controller depends on. Blacklisting ec_sys only keeps out a module the
# controller already refuses to run beside.
pattern='^(options|install|remove|blacklist|softdep)[[:space:]]+(msi[-_]ec|msi[-_]wmi[-_]platform|coretemp|ec[-_]sys)([[:space:]]|$)'
if overrides=$(modprobe -c 2>/dev/null | grep -Ei "$pattern"; exit "${PIPESTATUS[0]}"); then
    clean=1
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        clean=0
        if [[ $line =~ ^blacklist[[:space:]]+ec[-_]sys([[:space:]]|$) ]]; then
            warn modprobe_config "$line"
        else
            fail modprobe_config "$line"
        fi
    done <<<"$overrides"
    if ((clean)); then
        pass modprobe_config 'no override for msi_ec, msi_wmi_platform, coretemp or ec_sys'
    fi
else
    fail modprobe_config 'modprobe -c failed'
fi

# A newer DKMS registration always wins; another registered version is built
# automatically for new kernels whenever 0.13.1 is not registered.
declare -A reported=()
while IFS= read -r line; do
    [[ $line =~ ^msi_ec/([0-9][[:alnum:].+~-]*)[,:] ]] || continue
    other=${BASH_REMATCH[1]}
    [[ $other != "$module_version" && -z ${reported[$other]:-} ]] || continue
    reported[$other]=1
    conf=$dkms_tree/msi_ec/$other/source/dkms.conf
    if [[ ! -r $conf ]]; then
        warn dkms_registration "msi_ec/$other is also registered; cannot read $conf"
        continue
    fi
    autoinstall=$(sed -n 's/^[[:space:]]*AUTOINSTALL=//p' "$conf" | tail -n 1)
    autoinstall=${autoinstall//[\"\']/}
    if [[ -n $autoinstall ]]; then
        warn dkms_registration "msi_ec/$other is also registered with AUTOINSTALL=$autoinstall; DKMS builds it for new kernels if $module_version is ever unregistered"
    fi
done <<<"$dkms_status"

while IFS= read -r release; do
    [[ -n $release && $release != "$kernel_release" ]] || continue
    has_dkms_build "$release" ||
        warn other_kernel "$release has no msi_ec/$module_version build; booting it leaves the fans on firmware control"
done < <(installed_kernels)

if ((failed)); then
    echo "KERNEL READINESS FAILED: do not reboot into $kernel_release until every FAIL is resolved." >&2
    exit 1
fi
echo "KERNEL READINESS PASSED: $kernel_release"
