# What it looks like

## Normal Candidate 14 state

```console
$ msi-fan-profile status
{
  "cooler_boost": "off",
  "curve": "candidate14",
  "driver_srcversion": "AB0BFAE2391B5ADD66E01BD",
  "driver_version": "0.13.1",
  "ec_firmware": "1824EMS1.108",
  "factory_override": false,
  "fan_mode": "advanced",
  "service_active_state": "active",
  "service_enabled": true,
  "service_job": "",
  "service_main_pid": 1234,
  "service_sub_state": "running",
  "shift_mode": "comfort",
  "snapshot_stable": true
}
```

The PID naturally changes each boot.

## Temporary maximum cooling

```console
$ sudo msi-fan-profile boost-on
Cooler Boost enabled and physically verified.

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

After a watchdog, identity, curve, fan-response, or service failure, the intended
state is deliberately noisy:

```text
service:       failed (Restart=no)
curve:         factory
fan mode:      auto
Cooler Boost:  on
```

Inspect logs before releasing Boost:

```bash
sudo journalctl -u msi-fan-profile.service -b --no-pager
```
