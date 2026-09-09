# MSI Raider 18 HX AI Linux fan control

Measured, fail-safe Linux fan control for the **MSI Raider 18 HX AI A2XWIG**
(`MS-1824`) using an exact-firmware `msi-ec` extension and an EC-resident custom
fan curve.

> [!WARNING]
> This project writes model-specific embedded-controller registers. It is **not**
> a generic MSI fan-control package. Install it only when every compatibility
> check below matches exactly. A wrong EC map can stop cooling or make the laptop
> unresponsive.

## Exact tested configuration

| Item | Tested value |
|---|---|
| Laptop | MSI Raider 18 HX AI A2XWIG |
| Board | `MS-1824` |
| BIOS | `E1824IMS.310` |
| EC firmware | `1824EMS1.108` |
| CPU | Intel Core Ultra 9 285HX |
| GPU | NVIDIA GeForce RTX 5080 Laptop GPU |
| OS | Ubuntu 24.04.4 LTS |
| Kernel tested | `7.0.0-31-generic` |
| Secure Boot | Enabled |
| Local driver | `msi_ec 0.13.1`, srcversion `AB0BFAE2391B5ADD66E01BD` |

The upstream firmware comment names the RTX 5090 A2XWJG variant. This repository
was measured on the **A2XWIG RTX 5080 variant** with the same board and exact EC
firmware. Related model names alone are not sufficient evidence of compatibility.

## What it does

- Applies one fixed, measured curve—**Candidate 14**—to the EC.
- Selects the EC's `advanced` fan mode once; it does **not** continuously write
  fan speed.
- Monitors temperatures, physical fan RPM, exact curve/mode, and immutable
  hardware/driver identity read-only.
- Uses a systemd watchdog and exact-platform recovery.
- Preserves temporary Cooler Boost overrides.
- Restores the captured factory curve and firmware `auto` mode on clean stop.
- On failure, restores factory `auto` and deliberately leaves Cooler Boost on.

It does **not** expose arbitrary EC addresses, `ec_sys` write access, debug mode,
generic custom curves, silent mode, or shift-mode changes.

## Selected profile

```text
CPU thresholds:  50 60 70 76 82 88 °C
CPU fan values:  20 30 40 50 60 80 110
GPU thresholds:  38 40 42 44 55 70 °C
GPU fan values:  20 30 40 60 70 100 120
```

The EC's speed scale runs from 0 through 150 on this MSI interface. It should not
be assumed to be ordinary PWM percentage. Physical validation uses the WMI RPM
channels instead.

## Measured behavior

Candidate 14 was selected after idle, fixed-P-core CPU, CUDA GPU, combined-load,
failure-injection, boot, and 180-second soak tests.

| Test | Result |
|---|---|
| Settled idle | about 1,585 / 1,660 RPM in the final soak |
| GPU 75% duty, 60 s | tail GPU 64.4°C; CPU 79.5°C; fans 2,186 / 4,339 RPM |
| Fixed six P-cores, 90 s | Candidate 13-equivalent active bins: tail CPU 87.6°C; fans 5,525 / 3,743 RPM |
| Six P-cores + GPU 25%, 180 s | completed; tail CPU 94.4°C; GPU 55.5°C; fans 5,588 / 4,313 RPM |

See [Calibration and results](docs/CALIBRATION.md) for methodology, comparison
caveats, and evidence hashes.

### Fan-only limit

A fan curve cannot guarantee 80–85°C under every workload. In testing, even both
fans pre-stabilized at physical maximum could not hold the CPU below the mid-90s
under the heaviest CPU tier. Reducing extreme sustained temperatures further
requires a separate CPU power policy; this project intentionally does not alter
CPU power limits.

## Start here

1. Read the [safety model](docs/SAFETY.md).
2. Install the exact patched driver using
   [Driver installation](docs/DRIVER_INSTALL.md).
3. Install the profile service using [Setup](docs/INSTALL.md).
4. Use [Operations](docs/OPERATIONS.md) for daily commands.

Quick profile installation **after** the exact driver is validated:

```bash
git clone https://github.com/dead4d5a/msi-raider-18-hx-ai-linux-fan-control.git
cd msi-raider-18-hx-ai-linux-fan-control
sudo ./scripts/install-profile.sh
sudo msi-fan-profile apply-default
msi-fan-profile status
```

Do not skip the driver guide or compatibility checks.

## Current validation status

Completed:

- Secure Boot/MOK and signed DKMS loading
- Driver and curve readback
- Physical two-fan response
- Clean start/stop and boot persistence
- Forced process kill and factory recovery
- Runtime factory override and start-race protection
- Acknowledged Cooler Boost and 30-second cool release
- Idle, CPU-only, GPU-only, combined, and 180-second soak tests

Pending:

- One final attended suspend/resume validation of forced profile reapplication
  on the exact tested laptop. The daemon already implements and logs this path.

See [Validation status](docs/VALIDATION.md).

## Documentation

- [Driver installation](docs/DRIVER_INSTALL.md)
- [Profile installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Safety and architecture](docs/SAFETY.md)
- [Profile format](docs/PROFILE.md)
- [Calibration and results](docs/CALIBRATION.md)
- [Development history](docs/DEVELOPMENT_HISTORY.md)
- [Validation status](docs/VALIDATION.md)
- [Updates and rollback](docs/ROLLBACK.md)

## License and attribution

The original service/manager code in this repository is licensed under
GPL-3.0-or-later; see [Licensing](LICENSE.md).

`patches/msi-ec-0.13.1-exact-fan-curve.patch` modifies the GPL-2.0-or-later
[BeardOverflow/msi-ec](https://github.com/BeardOverflow/msi-ec) project. The
patch retains that upstream licensing and attribution. See [NOTICE](NOTICE).

MSI is a trademark of Micro-Star INT'L CO., LTD. This is an independent
community project and is not affiliated with or endorsed by MSI.
