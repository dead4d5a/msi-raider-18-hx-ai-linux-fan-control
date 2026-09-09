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
msi_ec 0.13.1 / AB0BFAE2391B5ADD66E01BD
```

Do not edit the checks to force a related model.

## Install

```bash
sudo ./scripts/install-profile.sh
```

The script:

- refuses to overwrite an existing installation;
- installs root-owned executables;
- installs a root-only tmpfiles lock;
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

## Verify systemd

```bash
systemctl status msi-fan-profile.service --no-pager
sudo journalctl -u msi-fan-profile.service -b --no-pager
```

The daemon should report `Candidate14 applied` and update its watchdog status.
It uses about one read-only monitoring pass per second and does not continuously
write fan speeds.

## Reboot validation

Reboot once:

```bash
sudo reboot
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

The daemon and manager hold a shared root-only lock, but unrelated software does
not know about that lock.
