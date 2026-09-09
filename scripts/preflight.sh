#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

base=/sys/devices/platform/msi-ec
factory='58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75'
failed=0

check_equal() {
    local label=$1 actual=$2 expected=$3
    if [[ $actual == "$expected" ]]; then
        printf 'PASS %-24s %s\n' "$label" "$actual"
    else
        printf 'FAIL %-24s got=<%s> expected=<%s>\n' "$label" "$actual" "$expected"
        failed=1
    fi
}

check_equal product "$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)" 'Raider 18 HX AI A2XWIG'
check_equal board "$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)" 'MS-1824'
check_equal bios "$(cat /sys/class/dmi/id/bios_version 2>/dev/null || true)" 'E1824IMS.310'
check_equal kernel_module "$(cat /sys/module/msi_ec/version 2>/dev/null || true)" '0.13.1'
check_equal module_source "$(cat /sys/module/msi_ec/srcversion 2>/dev/null || true)" 'AB0BFAE2391B5ADD66E01BD'
check_equal ec_firmware "$(cat "$base/fw_version" 2>/dev/null || true)" '1824EMS1.108'
check_equal shift_mode "$(cat "$base/shift_mode" 2>/dev/null || true)" 'comfort'

if mokutil --sb-state 2>/dev/null | grep -q 'SecureBoot enabled'; then
    echo 'PASS Secure Boot enabled'
else
    echo 'FAIL Secure Boot is not enabled'
    failed=1
fi

if lsmod | grep -q '^ec_sys '; then
    echo 'FAIL ec_sys is loaded'
    failed=1
else
    echo 'PASS ec_sys is unloaded'
fi

curve=$(cat "$base/fan_curve" 2>/dev/null || true)
if [[ $curve == "$factory" ]]; then
    echo 'PASS factory curve present'
elif [[ $curve == '50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120' ]]; then
    echo 'PASS Candidate14 curve present'
else
    echo "FAIL unknown curve: $curve"
    failed=1
fi

printf 'fan_mode=%s\ncooler_boost=%s\n' \
    "$(cat "$base/fan_mode" 2>/dev/null || true)" \
    "$(cat "$base/cooler_boost" 2>/dev/null || true)"

if ((failed)); then
    echo 'PREFLIGHT FAILED — do not install.' >&2
    exit 1
fi

echo 'PREFLIGHT PASSED'
