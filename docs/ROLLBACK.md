# Updates, rollback, and uninstall

## Before BIOS or EC firmware updates

Do not leave a model-specific custom curve active across an unvalidated firmware
update.

```bash
sudo msi-fan-profile factory-auto
sudo systemctl disable msi-fan-profile.service
```

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

DKMS attempts to rebuild for new kernels; success is not guaranteed. Before
relying on the service after a new kernel boots:

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

A service failure intentionally prefers:

```text
factory curve
fan mode auto
Cooler Boost on
service failed, Restart=no
```

Do not blindly turn Boost off after a failure. Stop heavy workloads, inspect
temperatures and logs, then use the guarded manager only after resolving the
cause.
