#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -Eeuo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if (($# < 1 || $# > 2)); then
  echo "Usage: $0 PINNED_UPSTREAM_CHECKOUT [KERNEL_RELEASE]" >&2
  exit 2
fi
driver_source=$(cd -- "$1" && pwd)
kernel_release=${2:-$(uname -r)}
[[ $kernel_release =~ ^[[:alnum:]_.+-]+$ ]] || {
  echo 'Invalid kernel release.' >&2
  exit 2
}
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
expected_patch=0b27ad8fb03613fe3bdbdc39598dc59c438c30f78c311eb125076f7562069f37
[[ $(git -C "$driver_source" rev-parse HEAD) == "$expected_commit" ]]
[[ $(git -C "$driver_source" rev-parse 'HEAD^{tree}') == "$expected_tree" ]]
[[ -z $(git -C "$driver_source" status --porcelain) ]] || {
  echo 'The pinned upstream checkout must be clean.' >&2
  exit 1
}
[[ $(git -C "$driver_source" archive --format=tar HEAD | sha256sum | awk '{print $1}') == "$expected_archive" ]]
patch_file=$repo/patches/msi-ec-0.13.1-exact-fan-curve.patch
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
[[ $(modinfo -F name "$module") == msi_ec ]]
[[ $(modinfo -F version "$module") == 0.13.1 ]]
[[ $(modinfo -F srcversion "$module") == 9086C45007CBB7FA1264430 ]]
[[ $(modinfo -F vermagic "$module") == "$kernel_release "* ]]
echo "Pinned driver patch and unprivileged build verification: PASS ($kernel_release)"
