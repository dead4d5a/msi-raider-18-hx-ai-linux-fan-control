# What it looks like

## Normal Candidate 14 state

```console
$ msi-fan-profile status
{
  "cooler_boost": "off",
  "configuration_valid": true,
  "curve": "candidate14",
  "driver_srcversion": "AB0BFAE2391B5ADD66E01BD",
  "driver_version": "0.13.1",
  "ec_firmware": "1824EMS1.108",
  "factory_override": false,
  "fan_mode": "advanced",
  "health": {
    "age_seconds": 0.2,
    "state": "normal",
    "status": "fresh"
  },
  "service_active_state": "active",
  "service_enabled": true,
  "service_job": "",
  "service_main_pid": 1234,
  "service_sub_state": "running",
  "shift_mode": "comfort",
  "snapshot_stable": true
}
```

The PID naturally changes each boot. This excerpt omits the detailed health
telemetry, per-channel errors, timestamps, latch ownership, and cooldown fields.

## Temporary maximum cooling

```console
$ sudo msi-fan-profile boost-on
Cooler Boost enabled and fan response verified.

$ sudo msi-fan-profile boost-release
Cooler Boost released after monitored cooldown.
```

Release waits for 30 continuously cool seconds. It does not blindly turn the
fans down while the machine is hot.

## Factory override for the current boot

```console
$ sudo msi-fan-profile factory-auto
Factory auto/off is active until apply-default or reboot.

$ msi-fan-profile status
{
  "cooler_boost": "off",
  "curve": "factory",
  "factory_override": true,
  "fan_mode": "auto",
  "service_active_state": "inactive",
  "service_enabled": true,
  "service_main_pid": 0,
  "service_sub_state": "dead",
  "snapshot_stable": true
}
```

Because the marker lives in `/run`, Candidate 14 returns at the next boot.

## Service health

```console
$ systemctl status msi-fan-profile.service
● msi-fan-profile.service - MSI Raider exact-platform Candidate14 fan profile
     Loaded: loaded (...; enabled)
     Active: active (running)
     Status: "Candidate14 CPU 54C GPU 42C fans 1839/2681"
```

## Failure behavior

After a watchdog, curve, fan-response, or other service failure on an
identity-verified system, the intended runtime-fault recovery state is
deliberately noisy:

```text
service:       failed (Restart=no)
keeper:        active (full-cooling keeper)
curve:         candidate14
fan mode:      advanced
Cooler Boost:  on
fan RPM:       physically verified when valid WMI telemetry is available
```

If WMI RPM telemetry is invalid, the recovery log says that it could not certify
physical RPM; it still does not intentionally restore the factory curve for the
runtime fault. An identity mismatch is different: recovery makes no
model-specific write because it cannot safely establish the exact hardware.

Inspect logs before releasing Boost:

```bash
sudo journalctl -u msi-fan-profile.service -b --no-pager
```

## Degraded telemetry

An implausible temperature or RPM snapshot does not immediately select factory
mode. It reports a non-expiring degraded-telemetry state, keeps Candidate
14/`advanced`, and latches Cooler Boost until complete valid telemetry and
physical fan verification return. An automatic latch still requires 30
continuously valid, cool seconds before release. A separate keeper handles only
genuine daemon faults and continues to reassert full cooling while the primary
failure remains visible.
