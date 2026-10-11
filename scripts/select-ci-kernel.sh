#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Select only the concrete headers installed by CI's generic metapackage: the
# 24.04 GA series by default, or the HWE series this laptop tracks.
set -euo pipefail

if (($# > 1)); then
    echo "Usage: $0 [linux-headers-generic|linux-headers-generic-hwe-24.04]" >&2
    exit 2
fi
meta_package=linux-headers-generic
if (($# == 1)); then
    meta_package=$1
fi
case "$meta_package" in
    linux-headers-generic|linux-headers-generic-hwe-24.04) ;;
    *)
        echo "Unsupported kernel header metapackage: $meta_package" >&2
        exit 2
        ;;
esac
[[ $(dpkg-query -W -f='${Status}' "$meta_package") == 'install ok installed' ]] || {
    echo "The generic kernel header metapackage is not installed: $meta_package" >&2
    exit 1
}
dependencies=$(dpkg-query -W -f='${Depends}' "$meta_package")
mapfile -t header_packages < <(
    printf '%s\n' "$dependencies" | tr ',' '\n' |
        awk '/^[[:space:]]*linux-headers-[0-9][[:alnum:].+-]*-generic([[:space:]]+\([^()|]*\))?[[:space:]]*$/ {print $1}'
)
if ((${#header_packages[@]} != 1)); then
    echo 'Expected exactly one versioned generic header dependency.' >&2
    exit 1
fi
header_package=${header_packages[0]}
[[ $(dpkg-query -W -f='${Status}' "$header_package") == 'install ok installed' ]] || {
    echo "The selected kernel header package is not installed: $header_package" >&2
    exit 1
}
printf '%s\n' "${header_package#linux-headers-}"
