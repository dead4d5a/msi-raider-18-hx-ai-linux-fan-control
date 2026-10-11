# Changelog

## Unreleased

- Stop `factory-auto` from disabling the service when it is merely too hot to
  release Cooler Boost. When the verified factory curve and auto mode are in
  place and only the cool release is refused (heat, or invalid or unavailable
  temperature telemetry such as the EC CPU 255 C sentinel), it now exits `1`
  with `factory staged with Boost on; ... rerun factory-auto when cool`, keeping
  Boost on, the runtime marker, boot enablement and any recovery-Boost latch, so
  Candidate 14 still returns at boot and a later start releases Boost when
  cool. A dedicated `FactoryReleaseRefused` error carries that case; identity,
  stop, lock, EC readback and postcondition failures keep the disabling
  fail-safe.
  `factory-auto` no longer requires the service to be enabled, so it succeeds
  after the documented BIOS, driver or failed-upgrade disables, and `status`
  accepts a disabled factory override. The recovery latch is cleared only after
  the factory `auto/off` postcondition passes, so a later failure can no longer
  re-assert Boost without it. `upgrade-profile.sh` reports a failed
  `factory-auto` as nothing replaced, with the cool-retry and `apply-default`
  instructions.
- Treat the `factory-auto` marker appearing while the daemon runs as a clean
  stop request, the same path as the stop signal that follows it, instead of a
  fault exit that briefly staged full-cooling recovery and a failed unit.
- Test `msi-gpu-recover`'s ExecStopPost decisions for every systemd service
  result, `release_boost_if_cool`, latch creation and lock timeout on private
  temporary files, and run the real `factory_auto()` and its fail-safe against
  an in-memory systemd and EC for cool, hot, invalid-telemetry, disabled-service
  and identity-failure transitions.
- Add a read-only pre-reboot kernel readiness check,
  `scripts/check-kernel-readiness.sh`, installed as
  `/usr/local/libexec/msi-fan-kernel-readiness`. For the `/boot/vmlinuz` target
  or a given kernel it fails unless `msi_ec/0.13.1` is DKMS-installed, resolves
  under `updates/dkms` with one `modules.dep` entry, carries a source version
  pinned in `msi_fan_control.py` and a matching `vermagic`, and is signed by the
  enrolled MOK certificate; it also checks `msi_wmi_platform`, `coretemp` and
  `modprobe.d` overrides. It warns about other `msi_ec` DKMS registrations with
  `AUTOINSTALL` and installed kernels without a build. DKMS signing failures do
  not fail a kernel installation, so this is the check that catches them.
- Install a kernel postinst hook,
  `/etc/kernel/postinst.d/zz-msi-fan-profile-readiness`, that runs the check
  after DKMS for every installed kernel, logs to the journal under the
  `msi-fan-profile` tag, warns on stderr on failure and always exits 0. Install,
  upgrade (including backup), uninstall and `INSTALL_RECORD` cover both files.
- Restore the deployed legacy driver patch from the initial release as
  `patches/msi-ec-0.13.1-legacy-fan-curve.patch` with its own pinned digest, so
  a lost `/usr/src/msi_ec-0.13.1` can be regenerated. `verify-driver-build.sh`
  takes `snapshot` (default) or `legacy` and accepts only that patch's
  checksum for the kernel's Kbuild layout.
- Build both patches in CI against the 24.04 GA and HWE kernel headers, on every
  push and pull request and weekly; `select-ci-kernel.sh` takes the header
  metapackage.
- Stop skipping the controller silently when its driver is unusable: the
  primary unit no longer has a `fan_curve` condition, so a missing, rejected or
  unpinned `msi_ec` after a kernel update fails the unit visibly at identity
  validation, before any EC write. The daemon, `ExecStopPost` and
  `msi-fan-profile status` name the reason, for example
  `msi_ec driver not loaded for kernel <release>`, instead of a generic identity
  failure or a bare errno. The keeper keeps its condition, and the factory-auto
  skip is unchanged.
- Wait up to 10 s at startup, with watchdog heartbeats, for udev to register the
  `msi_wmi_platform` and `coretemp` hwmon devices before the first EC write.
  Exactly one of each is still required, the failure after the bound is
  unchanged, and runtime monitoring never retries discovery.
- Make the 64 MiB memory limit a hard bound with `MemorySwapMax=0` on both units,
  and add `OOMScoreAdjust=-900` and `MemoryMin=32M`. `MemoryMin` is inert unless
  `system.slice` also has a protection allocation. `msi-fan-profile status`
  reports the active unit's current, peak and swap memory under a new
  `controller_memory` key, outside the stable comparison and exit status.
- Run the daemon, recovery helper, keeper and manager with
  `#!/usr/bin/python3 -I`, so a `python3` earlier in `PATH`, `PYTHON*`
  variables or user site packages cannot change their interpreter or imports.
- Pin every safety-relevant directive of both units in tests, each exactly once,
  with a deny-list for directives that would block EC writes, throttle the
  watchdog-protected loop, or let a condition skip the primary silently.
- Run release checks, including the unit-test suite, without root from install
  and upgrade: as the invoking sudo user, or as `nobody` on a private copy when
  no sudo user is known, with no supplementary groups and no new privileges.
  `verify-release.sh` itself now refuses to run as root.
- Keep daemon main-loop, manager and recovery tests off the live host: relocate
  every `/run` and sysfs path constant into a per-test directory, block
  unmocked fixed-path helpers and subprocesses, and fail when a new host path
  constant is not relocated. A keeper recovery latch in `/run` no longer makes
  release checks, and therefore guarded upgrades, fail.
- Fix a runtime memory leak that let `MemoryMax=64M` OOM-kill the controller
  after about 6.8 days, handing cooling to the keeper. Health publication
  wrapped each random temporary name in `pathlib.Path`; Python 3.12 interns path
  components as immortal strings, retaining about 100 bytes per one-second
  snapshot. The temporary is now removed by name only on failure. Allocation and
  failure-cleanup regression tests cover it; this is offline evidence, not a
  multi-day deployed soak.
- Select CI kernel headers from the installed generic metapackage, not the
  highest version across runner kernel flavors; check matching release, module
  support and exact battery-hook exports before building the pinned driver.
- Recognize only the two reproduced Kbuild source-checksum layouts in offline
  verification and print built module metadata; runtime driver ABI pins remain
  unchanged.
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
