#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if (($# < 1 || $# > 3)); then
  echo "Usage: $0 PINNED_UPSTREAM_CHECKOUT [KERNEL_RELEASE [snapshot|legacy]]" >&2
  exit 2
fi
driver_source=$(cd -- "$1" && pwd)
kernel_release=${2:-$(uname -r)}
[[ $kernel_release =~ ^[[:alnum:]_.+-]+$ ]] || {
  echo 'Invalid kernel release.' >&2
  exit 2
}
# snapshot is the current patch; legacy is the deployed fan_curve-only build.
build=snapshot
if (($# == 3)); then
  build=$3
fi
case "$build" in
  snapshot)
    patch_name=msi-ec-0.13.1-exact-fan-curve.patch
    expected_patch=0b27ad8fb03613fe3bdbdc39598dc59c438c30f78c311eb125076f7562069f37
    ;;
  legacy)
    patch_name=msi-ec-0.13.1-legacy-fan-curve.patch
    expected_patch=16b421b04dd40a3b26f14438a8f95f83c2602692ac8700d40ccc5c6e5bb14a2d
    ;;
  *)
    echo 'Invalid driver build: expected snapshot or legacy.' >&2
    exit 2
    ;;
esac
# Linux 6.13 builds external modules from their own directory, so Kbuild
# records the local header relatively and leaves it out of srcversion. Older
# Kbuild records it absolutely and includes it.
[[ $kernel_release =~ ^([0-9]+)\.([0-9]+)([^0-9]|$) ]] || {
  echo 'Invalid kernel release.' >&2
  exit 2
}
major=$((10#${BASH_REMATCH[1]}))
minor=$((10#${BASH_REMATCH[2]}))
if ((major > 6 || (major == 6 && minor >= 13))); then
  layout=relative
else
  layout=absolute
fi
kernel_build=/lib/modules/$kernel_release/build
[[ -d $kernel_build && -f $kernel_build/Makefile ]] || {
  echo "Matching kernel headers are missing: $kernel_build" >&2
  exit 1
}
[[ -r $kernel_build/include/config/kernel.release &&
   $(<"$kernel_build/include/config/kernel.release") == "$kernel_release" ]] || {
  echo "Kernel header release does not match $kernel_release: $kernel_build" >&2
  exit 1
}
if [[ ! -r $kernel_build/.config ]] ||
    ! grep -qx 'CONFIG_MODULES=y' "$kernel_build/.config" ||
    ! grep -qxE 'CONFIG_ACPI_BATTERY=(y|m)' "$kernel_build/.config"; then
  echo "Kernel headers require module and ACPI battery support: $kernel_build" >&2
  exit 1
fi
[[ -r $kernel_build/Module.symvers && -s $kernel_build/Module.symvers ]] || {
  echo "Kernel symbol exports are missing: $kernel_build/Module.symvers" >&2
  exit 1
}
for symbol in battery_hook_register battery_hook_unregister; do
  if ! awk -v wanted="$symbol" '$2 == wanted {found=1} END {exit !found}' \
      "$kernel_build/Module.symvers"; then
    echo "Kernel headers do not export required symbol $symbol: $kernel_build" >&2
    exit 1
  fi
done
printf 'Verified kernel build prerequisites: %s (%s)\n' \
  "$kernel_release" "$(readlink -f "$kernel_build")"

expected_commit=d7fbbd88e6831e56801b860e46475cbf8ddbc7c1
expected_tree=072c10a344c22372665a729add6c10faada7c6da
expected_archive=ee49828ce69a9aa9c500d3efcb923d0ba77b570e1a83351647e074a4c039eb6b
[[ $(git -C "$driver_source" rev-parse HEAD) == "$expected_commit" ]]
[[ $(git -C "$driver_source" rev-parse 'HEAD^{tree}') == "$expected_tree" ]]
[[ -z $(git -C "$driver_source" status --porcelain) ]] || {
  echo 'The pinned upstream checkout must be clean.' >&2
  exit 1
}
[[ $(git -C "$driver_source" archive --format=tar HEAD | sha256sum | awk '{print $1}') == "$expected_archive" ]]
patch_file=$repo/patches/$patch_name
[[ $(sha256sum "$patch_file" | awk '{print $1}') == "$expected_patch" ]]

verify_dir=$(mktemp -d)
cleanup() {
  rm -rf -- "$verify_dir"
}
trap cleanup EXIT
mkdir "$verify_dir/driver"
git -C "$driver_source" archive --format=tar HEAD | tar -xf - -C "$verify_dir/driver"
git -C "$verify_dir/driver" apply --check "$patch_file"
git -C "$verify_dir/driver" apply "$patch_file"

# Build only in this temporary copy. Never run the upstream install, signing,
# DKMS, load, or reload targets and never modify the supplied checkout.
make -C "$kernel_build" M="$verify_dir/driver" -j2 modules
module=$verify_dir/driver/msi-ec.ko
for field in name version srcversion vermagic; do
  printf 'Built module %s: %s\n' "$field" "$(modinfo -F "$field" "$module")"
done
[[ $(modinfo -F name "$module") == msi_ec ]]
[[ $(modinfo -F version "$module") == 0.13.1 ]]
# Kbuild's relative/absolute dependency layouts hash local headers differently.
# Each fingerprint was reproduced from byte-identical pinned source. The
# relative ones are the runtime ABI pins; the absolute ones are offline build
# checks only: do not add them to runtime ABI pins.
module_srcversion=$(modinfo -F srcversion "$module")
case "$build:$layout:$module_srcversion" in
  snapshot:relative:9086C45007CBB7FA1264430) ;;
  snapshot:absolute:123635EB33D32BA9FCFABCA) ;;
  legacy:relative:AB0BFAE2391B5ADD66E01BD) ;;
  legacy:absolute:518BD36E89E2B3026A56490) ;;
  *)
    echo "Unexpected pinned-source $build module checksum for the $layout Kbuild layout: $module_srcversion" >&2
    exit 1
    ;;
esac
[[ $(modinfo -F vermagic "$module") == "$kernel_release "* ]]
echo "Pinned $build driver patch and unprivileged build verification: PASS ($kernel_release)"
