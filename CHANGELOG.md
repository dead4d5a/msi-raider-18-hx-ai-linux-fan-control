# Changelog

## Unreleased

- Keep Candidate 14 active with physically verified Cooler Boost during
  sustained safety alarms instead of failing after 15 seconds of high heat.
- Report sustained maximum-cooling state through systemd status and rate-limited
  journal warnings while preserving failure recovery for actual integrity,
  sensor, curve, watchdog, write, and fan-verification faults.

## 0.1.0 - 2026-09-08

Initial public release for the exact tested MSI Raider 18 HX AI A2XWIG.

- Added exact-platform transactional `msi_ec 0.13.1` patch.
- Added measured Candidate 14 curve.
- Added persistent EC-resident profile daemon.
- Added read-only runtime thermal/fan monitoring.
- Added systemd watchdog and factory/Boost-on failure recovery.
- Added acknowledged Cooler Boost enable/release.
- Added runtime factory-auto override.
- Added Secure Boot/MOK/DKMS driver guide.
- Added calibration methodology and sanitized results.
- Documented final suspend/resume validation as pending.
