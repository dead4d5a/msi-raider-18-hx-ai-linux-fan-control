# What it looks like

## Normal Candidate 14 state

```console
$ msi-fan-profile status
{
  "configuration_valid": true,
  "controller_memory": {
    "locked_bytes": 0,
    "memory_current_bytes": 12234752,
    "memory_min_effective": false,
    "memory_peak_bytes": 13262848,
    "memory_swap_current_bytes": 0,
    "unit": "msi-fan-profile.service"
  },
  "cooler_boost": "off",
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
  "keeper_active_state": "inactive",
  "keeper_job": "",
  "keeper_main_code": null,
  "keeper_main_pid": 0,
  "keeper_main_status": null,
  "keeper_restarts": 0,
  "keeper_result": "success",
  "keeper_sub_state": "dead",
  "recovery_boost_latched": false,
  "service_active_state": "active",
  "service_enabled": true,
  "service_job": "",
  "service_main_code": null,
  "service_main_pid": 1234,
  "service_main_status": null,
  "service_restarts": 0,
  "service_result": "success",
  "service_sub_state": "running",
  "shift_mode": "comfort",
  "snapshot_stable": true
}
```

The PID naturally changes each boot. This excerpt omits the detailed health
telemetry, per-channel errors, timestamps, latch ownership, and cooldown fields.
`memory_min_effective` is `false` because `system.slice` reserves no memory for
the unit's `MemoryMin`, and `locked_bytes` is `0` because neither the controller
nor the keeper locks memory. The keeper's `keeper_main_code` and
`keeper_main_status` stay `null` until the keeper has run in the current boot;
once it has, they keep how its last process ended, also after `apply-default`.
The Status section of `OPERATIONS.md` describes each key.

## Driver not loaded after a kernel update

```console
$ msi-fan-profile status
ERROR: msi_ec driver not loaded for kernel 7.0.0-99-generic
```

The controller unit is then failed rather than skipped, and it has written
nothing to the EC; the fans remain under firmware control.

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

`msi-fan-profile status` exits `1` in this state and says why the controller
stopped. After a watchdog kill it includes:

```json
{
  "keeper_active_state": "active",
  "keeper_restarts": 0,
  "keeper_result": "success",
  "recovery_boost_latched": true,
  "service_active_state": "failed",
  "service_main_code": "killed",
  "service_main_status": 9,
  "service_result": "watchdog"
}
```

After a memory-limit kill `service_result` is `oom-kill`. A fault the daemon
detects itself ends with `exit-code`, `service_main_code` `exited` and
`service_main_status` `1`. If the keeper also fails and reaches its start limit,
`keeper_active_state` is `failed`, `keeper_restarts` counts the refused restart
too, `3` after three starts, and `keeper_result` keeps the result of the
keeper's last run, such as `watchdog`.

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
