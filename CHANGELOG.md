# Changelog

## Unreleased

- Select CI kernel headers from the installed generic metapackage, not the
  highest version across runner kernel flavors; check matching release, module
  support and exact battery-hook exports before building the pinned driver.
- Reject overdue same-poll telemetry before cooldown release and report actual
  observation freshness; retain Boost without an off/on flicker after slow reads.
- Add default-off, explicitly enabled timing diagnostics for validation, with no
  diagnostic clock calls/counter accumulation on the disabled path.
- Eliminate hardware scans while awaiting acknowledgements, reuse validated
  status identity values, and query enablement with batched systemd properties.
- Cache keeper WMI discovery with read-failure/resume invalidation, and keep
  one-shot recovery Boost asserted between RPM verification attempts.
- Add a fresh read-only driver control snapshot, shared exact legacy/snapshot
  build validation, and compiled mocked-I/O coverage for every snapshot read
  failure position. The new driver is not yet deployed/hardware-validated.
- Package the shared ABI helper with guarded upgrades and consolidate duplicate
  CI checks into the release verifier.
- Prevent mixed low/unavailable RPM spin-up samples from falsely proving a fan
  response failure; keep per-channel fan monitoring during temperature faults.
- Use incremental runtime/keeper response checks without blocking one-second
  control repair, and distinguish response-floor verification from maximum RPM.
- Publish atomic, PID-bound health with freshness, channel errors, Boost ownership,
  pending release, and cooldown progress; make degraded/stale status unsuccessful.
- Release the manager lock before cooldown waits, batch systemd property queries,
  and preserve latest manual-on intent when cancelling a queued release.
- Preserve runtime recovery after upgrade activation/status failures and reject
  implausible temperatures before an explicit factory Boost release.
- Reset cooldown on monitoring gaps; add resume, interrupted-cooldown, request,
  health-contract, upgrade-failure, and opt-in isolated systemd integration tests.
- Add a pinned-source, unprivileged driver patch/build verifier and CI job; retain
  explicit limits on hardware validation and thermal optimality claims.
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
- Carry a verified, root-only recovery-Boost handoff marker from the recovery
  helper/keeper into the next managed start, so recovery cooling becomes an
  automatic 30-second-cooldown latch rather than an accidental permanent manual
  override.
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
