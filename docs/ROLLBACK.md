# Updates, rollback, and uninstall

## Before BIOS or EC firmware updates

Do not leave a model-specific custom curve active across an unvalidated firmware
update.

```bash
sudo msi-fan-profile factory-auto
sudo systemctl disable msi-fan-profile.service
```

If `factory-auto` reports `factory staged with Boost on`, the machine is too hot
to release Cooler Boost. Let it cool and rerun `factory-auto` before updating.

After the update, record:

```bash
cat /sys/class/dmi/id/product_name
cat /sys/class/dmi/id/board_name
cat /sys/class/dmi/id/bios_version
cat /sys/devices/platform/msi-ec/fw_version
```

If BIOS or EC differs from the exact tested values, do not modify the checks or
force the old `firmware=` table. Wait for explicit validation/support.

## Kernel updates

DKMS attempts to rebuild for new kernels; success is not guaranteed. A build
failure fails the kernel package's configuration, which leaves the previous
kernel as the boot default. A signing failure does not: DKMS 3.0.11 only prints
that the module will not be signed and still reports it `installed`, and if
`/var/lib/shim-signed/mok/MOK.priv` or `MOK.der` is missing it silently signs
with a newly generated key that is not enrolled. The new kernel then becomes
the boot default, and Secure Boot lockdown refuses the module at the next boot.

Check the next boot kernel **before every reboot**, as a normal user:

```bash
/usr/local/libexec/msi-fan-kernel-readiness             # /boot/vmlinuz target
/usr/local/libexec/msi-fan-kernel-readiness 7.0.0-38-generic
```

From a checkout, `./scripts/check-kernel-readiness.sh` is the same check. It is
read-only and exits `0` only without a `FAIL` line. It checks that
`msi_ec/0.13.1` is DKMS-installed for that kernel, that `modinfo` resolves
`msi_ec` to that build under `updates/dkms` with exactly one `modules.dep`
entry, version `0.13.1`, a source version pinned in `msi_fan_control.py`, and a
matching `vermagic`. It also checks that the module is signed by the
`MOK.der` certificate (its `sig_key` equals the certificate serial) and that
`mokutil` reports that certificate as enrolled, that `msi_wmi_platform` and
`coretemp` exist, and that no `modprobe.d` entry blacklists, replaces or adds
options to those modules or to `ec_sys`. With Secure Boot disabled, signing
problems are reported as `WARN`. Only the certificate and enrollment checks can
need `sudo`, and only when they report that they cannot read their inputs.

`WARN` lines do not fail the check:

- another registered `msi_ec` DKMS version with `AUTOINSTALL` set, such as an
  old unpatched `msi_ec/0.13`. DKMS builds only the newest registered version,
  so it is harmless while `0.13.1` stays registered, but any window in which
  `0.13.1` is unregistered would let it build an unpatched driver for a new
  kernel. Remove such a registration in an attended session, after confirming
  it is installed for no kernel (`dkms status -m msi_ec`);
- another installed kernel with no `msi_ec/0.13.1` build. Booting it from the
  GRUB advanced menu leaves the fans on firmware control. Prefer a fallback
  kernel the check passes for, or build the module for it in an attended
  session (`sudo dkms install -m msi_ec -v 0.13.1 -k <release>`).

The installer also adds `/etc/kernel/postinst.d/zz-msi-fan-profile-readiness`.
After the `dkms` hook, it runs the same check for every kernel the package
manager installs, including unattended upgrades, and logs each line to the
journal under the `msi-fan-profile` tag; a failure is logged at `err` priority
and also printed as a warning in the package manager's output. The hook always
exits `0` and can never fail a kernel installation. Read its result with:

```bash
journalctl -t msi-fan-profile --no-pager
```

If header packages were configured after the kernel image, DKMS may build the
module after the hook ran; run the check again by hand before rebooting.

After a new kernel boots, confirm the loaded driver:

```bash
KVER=$(uname -r)
sudo dkms status -m msi_ec -v 0.13.1 -k "$KVER"
modinfo -n msi_ec
modinfo -F version msi_ec
modinfo -F srcversion msi_ec
modinfo -F signer msi_ec
msi-fan-profile status
```

The manager/daemon deliberately refuse an unexpected driver identity.

If the pinned driver did not load for the new kernel, for example after a DKMS
build or signing failure or when the in-tree `msi_ec` rejects this EC firmware,
`msi-fan-profile status` exits `1` with
`ERROR: msi_ec driver not loaded for kernel <release>`.
`msi-fan-profile.service` then appears in `systemctl --failed`, and its journal
gives the same reason. Neither the controller nor its recovery helper writes to
the EC in that state, the keeper is skipped, and the fans stay on the firmware's
own automatic control. Repair the driver for that kernel or boot the previous
kernel, then run `sudo msi-fan-profile apply-default`.

## Remove profile service only

From the repository:

```bash
sudo ./scripts/uninstall-profile.sh
```

The script first requires exact factory `auto/off`, then removes the manager,
daemon, service, and tmpfiles rule. The patched driver remains installed.

## Manual profile rollback

```bash
sudo msi-fan-profile factory-auto
sudo systemctl disable msi-fan-profile.service
sudo systemctl stop msi-fan-profile.service
```

Verify:

```bash
cat /sys/devices/platform/msi-ec/fan_mode
cat /sys/devices/platform/msi-ec/cooler_boost
cat /sys/devices/platform/msi-ec/fan_curve
```

Expected:

```text
auto
off
58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75
```

## Remove local driver

Only after the profile has been removed and factory mode verified:

```bash
sudo rm -f /etc/modules-load.d/90-msi-ec-local.conf
sudo modprobe -r msi_ec
sudo dkms remove -m msi_ec -v 0.13.1 --all
```

If DKMS removal succeeds:

```bash
sudo rm -rf /usr/src/msi_ec-0.13.1
sudo depmod -a
sudo reboot
```

Do not delete `/usr/src/msi_ec-0.13.1` after a failed DKMS removal. It can leave
DKMS in a broken state.

The enrolled local MOK need not be deleted to remove this module.

## Emergency boot recovery

At GRUB, append for one boot:

```text
module_blacklist=msi_ec modprobe.blacklist=msi_ec
```

Then remove the profile unit/loader and exact DKMS registration. Do not manually
load the blacklisted module in that recovery boot.

## Failure state

A runtime service failure intentionally attempts:

```text
Candidate 14 curve
fan mode advanced
Cooler Boost on
both fans physically verified when valid WMI RPM telemetry is available
service failed, Restart=no
failure keeper active, continuously reasserting Candidate 14/advanced/Boost
```

If telemetry is invalid, the recovery log records that physical verification
could not be established; it does not substitute the factory curve for the
runtime fault. The keeper never treats incomplete telemetry as a reason to
release Boost. Do not blindly turn Boost off after a failure. Stop heavy
workloads, inspect both unit logs and temperatures, then use the guarded manager
only after resolving the cause. Use `factory-auto` only when an intentional
factory rollback is wanted.
