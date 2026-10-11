# Profile service installation

Install the patched driver and validate its factory curve first. See
[DRIVER_INSTALL.md](DRIVER_INSTALL.md).

## Compatibility gate

Run:

```bash
./scripts/preflight.sh
```

Every line must pass. The profile installer deliberately supports only:

```text
Raider 18 HX AI A2XWIG
MS-1824
BIOS E1824IMS.310
EC 1824EMS1.108
msi_ec 0.13.1 / AB0BFAE2391B5ADD66E01BD (legacy)
          or / 9086C45007CBB7FA1264430 (snapshot ABI required)
```

Do not edit the checks to force a related model.

These are two explicitly validated builds, not a wildcard source-version check.
The snapshot build requires its read-only interface and never silently falls
back. See the staged driver-transition notes in [DRIVER_INSTALL.md](DRIVER_INSTALL.md).

The deployed legacy build's patch is kept as
`patches/msi-ec-0.13.1-legacy-fan-curve.patch`, only to restore a lost legacy
source tree. If `sudo dkms status -m msi_ec` lists any version other than
`0.13.1`, read
[Stale DKMS registrations](DRIVER_INSTALL.md#stale-dkms-registrations).

## Install

```bash
sudo ./scripts/install-profile.sh
```

The script:

- refuses to overwrite an existing installation;
- runs the release checks, including the unit tests, without root: as the
  invoking sudo user, or as `nobody` on a private copy of the checkout;
- installs root-owned executables and their shared read-only ABI helper;
- installs the read-only kernel readiness check
  `/usr/local/libexec/msi-fan-kernel-readiness` and a kernel postinst hook that
  runs it, logs the result, and never fails a kernel installation (see the
  kernel update section of [Rollback](ROLLBACK.md#kernel-updates));
- installs a root-only tmpfiles lock;
- installs the matching README, operations, and safety documentation used by
  the systemd unit;
- installs and enables—but does not start—the systemd service.

Activate after reviewing the output:

```bash
sudo msi-fan-profile apply-default
msi-fan-profile status
```

A healthy state includes:

```json
{
  "service_enabled": true,
  "service_active_state": "active",
  "service_sub_state": "running",
  "factory_override": false,
  "curve": "candidate14",
  "fan_mode": "advanced",
  "cooler_boost": "off",
  "snapshot_stable": true
}
```

## Upgrade an existing profile

The regular installer deliberately refuses to overwrite an existing deployment.
For a guarded in-place upgrade from this repository, use:

```bash
sudo ./scripts/upgrade-profile.sh
```

The upgrade command verifies the source manifest and exact platform (running
the release checks without root, as the installer does), stages all
artifacts, moves the existing controller to verified factory `auto/off`,
disables it for the replacement window, keeps a root-only backup, refreshes the
manager/daemon/recovery helper/readiness check/kernel hook/unit/tmpfiles/
documentation as one release (adding the readiness check and kernel hook to
older deployments), and reactivates only through the new guarded manager. Fresh installs and upgrades
write an `INSTALL_RECORD` with the deployed artifact hashes under
`/usr/local/share/doc/msi-fan-profile/`.
If the upgrade stops before activation after entering the safe state, it leaves
the service disabled in factory auto mode. Once activation begins, a failed
start or health check leaves runtime cooling, the recovery keeper, and boot
enablement untouched; it reports the error and retained backup location. It
never replaces runtime recovery with factory mode merely because activation or
status verification failed. Inspect both units before deciding on another action.

If the existing manager cannot complete `factory-auto`, for example because the
machine is too hot to release Cooler Boost, the upgrade stops before replacing
any artifact, and the script itself changes no boot enablement. Let the machine
cool and rerun the upgrade. If a cool retry still fails, or to resume the
installed profile now, run `sudo msi-fan-profile apply-default` and rerun the
upgrade when cool. Managers older than this release also disable the service on
that failure, so their cool retry fails until `apply-default` has been run.

## Verify systemd

```bash
systemctl status msi-fan-profile.service --no-pager
sudo journalctl -u msi-fan-profile.service -b --no-pager
```

The daemon should report `Candidate14 applied` and update its watchdog status.
It uses about one read-only monitoring pass per second and does not continuously
write fan speeds.

## Reboot validation

Check the next boot kernel with the read-only readiness check; it must end with
`KERNEL READINESS PASSED`. Then reboot once, preferably while cool (see
[Rollback](ROLLBACK.md#before-a-kernel-update-or-reboot)):

```bash
/usr/local/libexec/msi-fan-kernel-readiness && sudo reboot
```

After boot:

```bash
mokutil --sb-state
cat /sys/module/msi_ec/version
cat /sys/module/msi_ec/srcversion
msi-fan-profile status
```

Also verify the root-only locks were recreated:

```bash
sudo stat -c '%U:%G:%a %n' \
  /run/msi-fanctl.lock \
  /run/msi-fan-profile-manager.lock
```

Expected ownership/mode is `root:root:600`.

## Do not install alongside

Do not run this service concurrently with:

- MControlCenter fan-curve writes
- NBFC/NBFC-Linux
- ISW
- fancontrol/other EC writers
- `ec_sys` with write support
- `msi_ec debug=1`
- calibration-era tools such as `/usr/local/bin/msi-fanctl`; see the superseded
  tools section of [Operations](OPERATIONS.md#superseded-calibration-tools)

The daemon and manager hold a shared root-only lock, but unrelated software does
not know about that lock.
