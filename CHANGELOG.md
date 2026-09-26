# Changelog

## Unreleased

- Keep Candidate 14 active with physically verified Cooler Boost during
  sustained safety alarms instead of failing after 15 seconds of high heat.
- Report sustained maximum-cooling and degraded-telemetry states through systemd
  status and rate-limited journal warnings while escalating actual identity,
  curve, watchdog, write, and genuine fan-response faults to full-cooling
  recovery.
- Replace the identity-verified runtime-fault factory fallback with Candidate 14
  in `advanced` mode plus Cooler Boost on, with physical fan verification when
  valid telemetry permits it.
- Replace the five-second telemetry-integrity failure with a non-expiring
  degraded-telemetry latch. It treats the EC `255°C` byte as an invalid
  sentinel, keeps Candidate 14 and Cooler Boost latched, and requires complete
  valid telemetry plus physical fan verification before cooldown can begin.
- Add an `OnFailure` full-cooling keeper that holds the fan lock and continuously
  reasserts Candidate 14/`advanced`/Boost after any genuine daemon fault, while
  keeping the primary failure visible and avoiding a primary-service restart loop.
- Prevent a post-start `apply-default` failure from overwriting that runtime
  recovery with the factory curve.

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
