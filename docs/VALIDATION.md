# Validation status

## Completed on the exact tested laptop

- Secure Boot remained enabled throughout.
- Local MOK enrollment and signed DKMS loading.
- Exact module version/source/signature checks.
- Driver autoload after reboot.
- Factory curve capture and exact readback.
- Rejection of malformed and unsafe curves.
- Transactional identical-factory write/readback.
- Candidate application only under auto + physically verified Cooler Boost.
- Physical fan 1/fan 2 RPM monitoring.
- Idle, CPU-only, GPU-only, and combined workloads.
- 180-second combined Candidate 14 soak.
- Clean service start/stop.
- systemd watchdog progression.
- Forced `SIGKILL` recovery to Candidate 14/`advanced` with Cooler Boost on
  and both physical fans verified on 2026-09-26.
- Runtime factory override and direct-start refusal.
- Daemon detection of a marker appearing after service start.
- Manager acknowledgement for Boost-on and 30-second cool release.
- Reboot persistence with root-only lock recreation and Candidate 14 auto-apply.
- Observed EC CPU `255°C` telemetry degradation on 2026-09-29 from 02:59:13
  through 03:11:13 America/Denver: the then-installed controller stayed active,
  retained Boost, recovered telemetry, and released after monitored cooldown at
  03:11:44. This is live evidence for the preceding implementation, not a
  deployment or hardware validation of the 2026-09-30 changes.
- Guarded user-space upgrade on 2026-09-30 with the signed legacy driver
  `AB0BFAE2391B5ADD66E01BD` retained. All ten installed artifacts matched their
  recorded hashes and root-owned modes; the shared ABI helper imported correctly.
- The new primary remained active with fresh normal health, no channel errors
  or restarts, advancing watchdog notifications, and persistent boot enablement.
  Both services had timing diagnostics disabled; health contained no timing field.
  That build later leaked memory until its memory limit killed it; see the field
  evidence below.
- Real Boost-on acknowledgement verified both fans at 5,714 RPM, followed by an
  acknowledged release after 30 continuously cool seconds and return to normal
  health. This validates the deployed user-space path on the legacy driver, not
  the new snapshot driver, forced-fault recovery, or suspend/resume.

## Field evidence on 2026-10-10 and 2026-10-11

These are production events on the signed legacy driver
`AB0BFAE2391B5ADD66E01BD`, taken from the journal (America/Denver). All but the
operator-run upgrade attempt at 19:02 were unattended. They are field
observations, not attended or injected-fault tests.

- Memory-limit kill: the 2026-09-30 build's daemon, started at the 2026-10-03
  boot, ran until 2026-10-10 11:30:34, six days and 20 hours, when
  `MemoryMax=64M` killed it (`Failed with result 'oom-kill'`, 64.0M memory peak,
  8.6M swap peak). Health publication leaked about 100 bytes per second;
  6ed5498 fixes it.
- Unattended keeper takeover: in the same second, `ExecStopPost`
  (`service_result=oom-kill`) verified Candidate 14/`advanced` with Cooler Boost
  on. The `OnFailure` keeper started, verified fan response at 3,076/3,720 RPM
  against the 3,000 RPM floor, and held that state for about 24.5 hours without
  a restart (10.8M memory peak, no swap).
- Latch handoff: on 2026-10-11 at 12:03:41, `apply-default` stopped the keeper.
  The daemon applied Candidate 14 with
  `preserved_boost=True recovery_latch=True` and released the inherited Boost as
  an automatic latch at 12:04:12, after its 30-second cooldown.
- A cool guarded upgrade deployed 6ed5498 at 12:06. The stop logged
  `successful candidate14 service left thermally verified factory auto/off`, and
  Candidate 14 was applied again at 12:06:17. A multi-day run of the fix is
  still pending.
- Reboot from 7.0.0-34 to 7.0.0-38: at the 12:43:36 shutdown the machine was too
  hot to release Boost. The daemon restored factory auto with Boost on and
  exited `1` with `factory restored; Boost release unsafe`; `ExecStopPost` then
  verified Candidate 14/`advanced` with Boost on. systemd could not enqueue the
  `OnFailure` keeper during shutdown and logged that. This is the intended hot
  stop described in `SAFETY.md`. On 7.0.0-38 the signed DKMS driver loaded at
  3.5 s, but `systemd-tmpfiles-setup` finished only at 84.0 s, so Candidate 14
  was applied at 85.6 s, with `preserved_boost=False recovery_latch=False`:
  Boost did not survive the reboot. The EC ran its firmware auto curve until
  then.
- A cool reboot on 7.0.0-38 at 14:40 left verified factory auto/off. On the next
  boot `systemd-tmpfiles-setup` finished at 8.8 s and Candidate 14 was applied
  at 13.4 s.
- Upgrade attempt on 2026-10-11 at 19:02 under bursty load (six database insert
  processes at about 80% CPU each): the upgrade's cool-down gate passed with 10
  consecutive one-second samples, the last at CPU package 59 °C, EC CPU 60 °C
  and EC GPU 44 °C. 0.26 s later the installed 6ed5498 daemon's single-reading
  release check found it not cool, so the daemon exited with
  `factory restored; Boost release unsafe`, `ExecStopPost` staged Candidate
  14/`advanced` with Boost and the recovery marker, and that release's manager
  refused `factory-auto` and disabled the service. The upgrade replaced
  nothing. The operator reports that these sensors are flaky: the coretemp
  package reading jumps 20 °C or more within milliseconds under bursty load,
  and the EC sometimes returns junk such as 255 °C, 65535 RPM or an empty
  value. The unreleased cool confirmation and full-cooling upgrade handover in
  `CHANGELOG.md` respond to this.

## Field evidence on monitoring stalls, 2026-10-24 to 2026-11-13

These are events of v0.2.0 (`e4d4e19`), unattended, on the signed legacy
driver `AB0BFAE2391B5ADD66E01BD`, taken from the journal of both units
(America/Denver). They are field observations, not attended or injected-fault
tests. The monitoring-gap warning of 0.2.0 gives no duration. The raw journal
exports stay on the validation host, in a directory outside the repository;
the SHA-256 of each export's units log is given below.

- 2026-10-24, 16:20:28 and 16:20:30: two monitoring gaps. Each latched Cooler
  Boost and reset the cooldown, and the second pass logged
  `sensor telemetry recovered; Cooler Boost remains latched until monitored cooldown completes`.
  The journal holds no later line from that boot, so it does not show the
  release; the next boot applied Candidate 14 at 16:21:44.
- 2026-10-30, 22:52:03: one monitoring gap, handled the same way. The automatic
  latch released Cooler Boost at 22:52:33, after its 30-second cooldown, and a
  clean stop 11 s later left thermally verified factory auto/off.
- 2026-11-10, 04:52:24, during a host-wide out-of-memory stall: a sensor
  acquisition took 5.943 s, and the next pass found a monitoring gap; both
  latched Cooler Boost. At 04:52:32, 8.5 s later, systemd logged
  `Watchdog timeout (limit 5s)!` and killed the daemon, which had run healthy
  since 2026-10-30 (memory peak 14.0 MB). `ExecStopPost`
  (`service_result=watchdog`) verified Candidate 14/`advanced` with Cooler
  Boost on 2 s after the timeout. The keeper started, verified fan response at
  6,153/6,315 RPM and held that state for 2 h 47 min, until the operator
  rebooted. The next boot applied Candidate 14 normally.
  Source for these three entries: the export of 2026-11-11, units log SHA-256
  `1d44c90b56a5b2c3dc7a5e35a16a3dbe6c6c3f8ee43c968fa36cdcd1264afc50`.
- 2026-11-13, during another host-wide out-of-memory stall: the 5 s watchdog
  killed the daemon, which had run since the boot of 2026-11-10 (memory peak
  15.1 MB). `ExecStopPost` verified Candidate 14 in `advanced` mode with Cooler
  Boost on within 2 s. The keeper started and held Boost, and then its own 5 s
  watchdog killed it three times within four minutes; the kernel needed 5 to
  14 s to finish off each killed keeper. At the third restart systemd refused
  the start (`Start request repeated too quickly`; the limit is three starts in
  15 minutes). From then on no process supervised the fans, for 9 h 35 min: the
  EC stayed at Candidate 14 in `advanced` mode with Cooler Boost on, last
  verified by the keeper at 6,153/6,400 RPM, until the operator ran
  `sudo msi-fan-profile apply-default`. That started the daemon with the
  recovery latch; the daemon released Cooler Boost after its 30-second
  cooldown, and `msi-fan-profile status` exited `0`. Source: a later journal
  export, units log SHA-256
  `90034453805bdd0da74a221d607c4c2a998fbb6dafea7b46dd119b84f355b3b6`.

Neither kill was a fault of the daemon. Full cooling held both times; on
2026-11-13 supervision did not. 0.2.1 raises the watchdog of both units to
30 s.

## Automated validation on 2026-09-30

- Mocked recovery-marker handoff and manual-latch separation, including heat or
  invalid telemetry interrupting cooldown at 29 seconds.
- Mixed low/unavailable RPM, independent valid-fan monitoring during invalid
  temperatures, real nonresponse evidence, and nonblocking keeper repair.
- Mocked suspend offsets during automatic/manual Boost, degraded telemetry, and
  queued release; cooldown reset and preserved latch ownership.
- All CPU/GPU/fan-stall thresholds, interrupted threshold timers, monitoring
  gaps, request arrival order, cancellation, stale acknowledgements, and service
  replacement while a manager request waits.
- Atomic health ownership/mode/schema/freshness validation, including a real
  publisher/manager round trip with only root ownership virtualized.
- Relocated, mocked full upgrade-script failures before and after activation,
  including a refused hot handover that continues at full cooling with the
  recovery-Boost marker kept, and every other refusal state that stops it.
- Opt-in isolated user-systemd tests for a missed watchdog, post-stop-before-
  keeper ordering, exclusive control and keeper yield, and restart-limit
  exhaustion. All use unique user units and fake files, not production helpers
  or the production privileged sandbox.
- Isolated unprivileged application/build of the exact pinned driver patch for
  `7.0.0-34-generic`, with module version/source/vermagic checks; no installation,
  signing, loading, or hardware access.
- Mocked 2.5/3-second same-poll stalls near cooldown completion, including
  combined snapshot stalls: no Boost release until a new full cool interval,
  honest observation/last-valid timestamps and reset physical-response evidence.
- Default-off/invalid timing settings make zero diagnostic-clock calls; enabled
  bounded metrics account for stalls/deadlines without changing cooling behavior.
- Thirty-second acknowledged-release waits make no hardware scans while pending;
  terminal acknowledgement reads only Boost. Status performs one fresh snapshot
  on the new ABI and two batched systemd queries for its stability comparison.
- Keeper discovery cache reuse, read-failure/resume invalidation, rate-limited
  opt-in metrics, and guarded one-shot Boost reassertion between retries.
- Strict shared parser/build pins and a compiled mock-EC harness for the actual
  snapshot callback: all 43 read failure positions, firmware freshness, mutex
  coverage, configuration visibility, state validation and raw sentinel framing.

Reproduce default checks with `./scripts/verify-release.sh`. The optional
systemd and driver checks are documented in `CONTRIBUTING.md`. CI additionally
builds the snapshot and deployed legacy patches against the runner's 24.04 GA
and HWE kernel headers, also weekly; that CI job is not evidence of attended
thermal compatibility on another kernel.

## Pending

- Final attended suspend/resume test confirming the daemon logs a resume event,
  forcibly reprograms Candidate 14, and preserves watchdog service health. The
  validation host has `sleep`, `suspend`, `hibernate` and `hybrid-sleep` targets
  masked since 2026-09-29, and Secure Boot lockdown disables hibernation there,
  so the current code's resume path has not run in the field.
- Attended EC telemetry-sentinel test confirming degraded mode remains active,
  Boost stays latched, and valid telemetry resumes the physical-verification
  path without a primary-service failure.
- Attended primary-service fault test confirming the failure keeper reasserts
  Candidate 14/`advanced`/Boost and yields cleanly to `apply-default`. The
  unattended memory-limit kill above exercised this path in the field; it does
  not replace an attended, injected fault.
- Extended attended validation of the deployed 2026-10-11 user-space release
  (6ed5498), including a multi-day run with `controller_memory` checks and the
  fault, resume, and sustained-load cases above. Successful guarded deployment
  and Boost/cooldown checks do not replace those hardware tests.
- Guarded deployment and attended validation of the unreleased changes in
  `CHANGELOG.md`, including a visible driver-loss failure after a kernel update
  and the kernel readiness hook on a real kernel installation.
- Signing/staging/deployment and attended validation of the new snapshot driver
  `9086C45007CBB7FA1264430`. The currently loaded legacy module remains unchanged.
- Matched post-deployment performance timings with diagnostics temporarily on,
  then confirmation that they are disabled after validation. No throughput or
  battery-life improvement is claimed from the offline tests alone.
- Simulated failures at each kernel EC write/readback/rollback position and
  concurrent kernel-store tests. The build verifier checks compilation and
  module identity, not transaction behavior under injected I/O faults.
- Repeated matched, longer attended thermal/performance/acoustic comparisons;
  Candidate 14 remains measured and promising, not proven optimal.

The implementation already uses a suspend-time discontinuity between
`CLOCK_BOOTTIME` and monotonic time and forces a full safe reapply. Until the
attended result is recorded, suspend/resume should be treated as implemented but
not finally validated. Keep suspend masked on such a host until it is.

## Not claimed

- Other BIOS versions
- Other EC firmware versions
- RTX 5090 A2XWJG behavior
- Other MS-1824 marketing variants
- Other Ubuntu/kernel releases, including an upgrade to Ubuntu 26.04; field
  operation on 7.0.0-34 and 7.0.0-38 is not attended validation
- Hibernation behavior
- Hard CPU temperature enforcement below 90°C
- Generic fan-curve editing

Contributions for another exact configuration should include DMI/BIOS/EC
identity, curve backup, physical RPM verification, failure recovery, and
repeatable measurements. Do not submit “works on MSI” claims without exact data.
