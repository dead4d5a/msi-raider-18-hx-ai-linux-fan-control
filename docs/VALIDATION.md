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
  the snapshot driver, forced-fault recovery, or suspend/resume.

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
  Candidate 14 was applied again at 12:06:17. v0.2.0, which contains the fix,
  replaced this build later that day (v0.2.0 tag message); its multi-day runs
  are recorded under "Field evidence from 2026-10-18 to 2026-11-11" below.
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
- Upgrade attempt on 2026-10-11 at 19:02 under bursty load: the upgrade's
  cool-down gate passed with 10 consecutive one-second samples, the last at CPU
  package 59 °C, EC CPU 60 °C and EC GPU 44 °C. 0.26 s later the installed
  6ed5498 daemon's single-reading release check found it not cool, so the
  daemon exited with `factory restored; Boost release unsafe`, `ExecStopPost`
  staged Candidate 14/`advanced` with Boost and the recovery marker, and that
  release's manager refused `factory-auto` and disabled the service. The
  upgrade replaced nothing. The operator reports that these sensors are flaky:
  the coretemp package reading jumps 20 °C or more within milliseconds under
  bursty load, and the EC sometimes returns junk such as 255 °C, 65535 RPM or
  an empty value. The cool confirmation and the full-cooling upgrade handover
  of 0.2.0 (`CHANGELOG.md`) respond to this.

## Field evidence from 2026-10-18 to 2026-11-11

This is field evidence for v0.2.0, unattended: events of `e4d4e19` on the
signed legacy driver `AB0BFAE2391B5ADD66E01BD`, taken from an export of
2026-11-11 (America/Denver). From the journal, the entries use its list of
boots and the lines of both units, of the readiness hook and of the kernel's
out-of-memory killer; from outside it, the install record and the boot list
of the login records (`wtmp`), which reaches back further. The events are
field observations, not attended or injected-fault tests, and they count only
for the build they happened on; code that changes afterwards needs its own
evidence. When the journal was exported, nothing before 2026-10-18 21:36 was
retained, so the first week of v0.2.0 is not covered. The monitoring-gap
warning of 0.2.0 gives no duration. The raw export stays on the validation
host, in a directory outside the repository; its units log has SHA-256
`1d44c90b56a5b2c3dc7a5e35a16a3dbe6c6c3f8ee43c968fa36cdcd1264afc50`. The
export's manifest `SHA256SUMS`, with the SHA-256 of every other file, has
SHA-256 `36a6c17dff07553836222e62fea42a0654886434d66d5c92413ba67858baab43`.
Every figure below can be recomputed from that export and this repository.
The entries are selected events, among them every monitoring gap; the full
log is in the export.

- Deployment: the install record in the export names `e4d4e19` as
  `source_commit` and the SHA-256 of that commit's `SHA256SUMS` as
  `source_manifest_sha256`, and its 12 file digests are those of the files of
  v0.2.0, so its `source_tree_dirty=yes` reached no installed file. v0.2.0 was
  deployed on 2026-10-11 (v0.2.0 tag message), before the retained journal
  begins.
- Daemon runs: the run that the clean stop of 2026-10-19 ended had started
  before the retained journal, at the earliest with the boot of 2026-10-16
  (`wtmp` boot list), at most 2.8 days earlier, and peaked at 15.6M. The run of
  5.3 days from 2026-10-19 to the end of its boot on 2026-10-24 (below) logged
  no stop line, so no peak was recorded. The run of 6.3 days from 2026-10-24 to
  2026-10-30 peaked at 13.7M and used 26 min 40 s of CPU time, and the run of
  10.3 days from 2026-10-30 that the watchdog kill of 2026-11-10 ended peaked
  at 14.0M and used 45 min 22 s: about 0.3 % of one core in both runs. All
  three peaks came with `0B memory swap peak`. The leaking 2026-09-30 build had
  reached its 64M limit on 2026-10-10 (above).
- 2026-10-18 21:36 to 2026-10-19 09:18, sustained heat: in the 702 minute status
  lines of this period the logged CPU temperature was at or above 95 °C in 55 %
  and Cooler Boost was on in 76 %, and the daemon logged
  `Cooler Boost established for alarm` 273 times, without a failure. The clean
  stop at 09:18 logged
  `Cooler Boost release confirmed: 6 consecutive valid cool samples 0.5 s apart after 2.5 s`
  and `successful candidate14 service left thermally verified factory auto/off`.
- 2026-10-20, 04:59:37 to 05:03:32: EC telemetry sentinel. The daemon logged
  `EC CPU temperature sentinel 255C` at 04:59:37 and then once a minute, and
  stayed in the same process with Candidate 14 active and Cooler Boost latched.
  At 05:03:32 it logged
  `sensor telemetry recovered; Cooler Boost remains latched until monitored cooldown completes`,
  and at 05:04:02 it released Cooler Boost after its 30-second cooldown. Single
  `implausible fan2=480000RPM` readings on 2026-10-23 and 2026-10-26 took the
  same path: telemetry recovered 2 s later, and Boost was released 30 s after
  that. These events close the attended sentinel test that was pending, for
  v0.2.0 only. Injecting the sentinel would need a raw EC write, which this
  project does not support (`SECURITY.md`), so a later release that changes
  this path rests on its unit tests until such an event occurs on it.
- 2026-10-24, 16:20:28 and 16:20:30: two monitoring gaps. Each latched Cooler
  Boost and reset the cooldown, and the second pass logged
  `sensor telemetry recovered; Cooler Boost remains latched until monitored cooldown completes`.
  The journal holds no later line from that boot, so it does not show the
  release; the next boot applied Candidate 14 at 16:21:44.
- 2026-10-30, 22:52:03: one monitoring gap, handled the same way. The automatic
  latch released Cooler Boost at 22:52:33, after its 30-second cooldown, and a
  clean stop 11 s later logged the same release confirmation, after 5.0 s, and
  left thermally verified factory auto/off.
- 2026-11-01, kernel installation: the readiness hook logged 12 `PASS` lines
  and `KERNEL READINESS PASSED: 7.0.0-42-generic`. The first boot of that kernel
  (`wtmp` boot list) followed the kill of 2026-11-10 (next entry). The retained
  journal holds no installation of a kernel that fails the check.
- 2026-11-10, 04:52:24, during a host-wide out-of-memory stall: a sensor
  acquisition took 5.943 s, and the next pass found a monitoring gap; both
  latched Cooler Boost. At 04:52:32, 8.5 s later, systemd logged
  `Watchdog timeout (limit 5s)!` and killed the daemon, which had run healthy
  since 2026-10-30. `ExecStopPost` (`service_result=watchdog`) verified
  Candidate 14/`advanced` with Cooler Boost on 2 s after the timeout. The
  keeper started, verified fan response at 6,153/6,315 RPM and held that state
  for 2 h 47 min, until the operator rebooted. The next boot applied
  Candidate 14 normally.

## Field evidence from 2026-11-12 to 2026-11-13

This is field evidence for v0.2.0, unattended, on the same driver, from a
later journal export of 2026-11-13 (America/Denver), which also stays on the
validation host. From the journal, the entries use the lines of both units and
of the kernel's out-of-memory killer; from outside it, the recorded exit code
of `msi-fan-profile status`. The export's units log has SHA-256
`90034453805bdd0da74a221d607c4c2a998fbb6dafea7b46dd119b84f355b3b6`, and its
manifest `SHA256SUMS`, with the SHA-256 of every other file, has SHA-256
`390b3db64b162401bcfdea14de715386e891081182f1bec86636b5d1c8a697e0`.
That the daemon had run since the boot of 2026-11-10 comes from the export of
2026-11-11, whose units log agrees line for line with this one where the two
overlap. Every figure below can be recomputed from the two exports and this
repository. The entries are selected events, among them every monitoring gap;
the full log is in the export.

- 2026-11-12, 18:10:23, during a host-wide out-of-memory stall: one monitoring
  gap, with Cooler Boost on for a CPU alarm since 17:57:11. The daemon logged
  `WARNING monitoring gap; Boost latched and cooldown reset` and, in the same
  pass, the `sensor telemetry recovered` line quoted above. Three more CPU
  alarms followed, and the daemon released Cooler Boost after its 30-second
  cooldown at 18:48:34. The kernel logged a host-wide out-of-memory kill
  0.06 s before the warning.
- 2026-11-13, during another host-wide out-of-memory stall: the 5 s watchdog
  killed the daemon, which had run since the boot of 2026-11-10 (15.1M memory
  peak). `ExecStopPost` verified Candidate 14 in `advanced` mode with Cooler
  Boost on within 2 s. The keeper started and held Boost, and then its own 5 s
  watchdog killed it three times within four minutes; the kernel needed 5 to
  14 s to finish off each killed keeper. At the third restart systemd refused
  the start (`Start request repeated too quickly`; the limit is three starts in
  15 minutes). From then on no process supervised the fans, for 9 h 35 min: the
  EC stayed at Candidate 14 in `advanced` mode with Cooler Boost on, last
  verified by the keeper at 6,153/6,400 RPM, until the daemon was started
  again at 18:44:44, with the recovery latch; the daemon released Cooler Boost
  after its 30-second cooldown, and `msi-fan-profile status` exited `0`.

Neither the kill of 2026-11-10 nor that of 2026-11-13 was a fault of the
daemon. Full cooling held both times; on 2026-11-13 supervision did not. 0.2.1
raises the watchdog of both units to 30 s.

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
thermal compatibility on another kernel. The snapshot build
`9086C45007CBB7FA1264430` was never signed, loaded or run on the validation
host, and it leaves the accepted set when the new driver build arrives (DR-10).

## Pending

Each entry names the test that closes it.

- Injected faults on the release candidate (VA-12): a kill, after which the
  keeper holds full cooling until `apply-default` hands back and the daemon
  releases Cooler Boost after 30 continuously valid, cool seconds; a 20 s
  freeze that the daemon survives; a freeze longer than the watchdog, which
  hands over to the keeper; and a second failure in the same boot. The kill
  and the hand-back are rehearsed once on 0.2.1 when it is deployed (ST-6). The
  watchdog kills of 2026-11-10 and 2026-11-13 exercised v0.2.0 up to the
  keeper, and on 2026-11-13 the daemon was started again with the
  recovery latch after the keeper had stopped at its start limit. No hand-back
  from an active keeper is recorded for v0.2.0, and field events do not
  replace an attended, injected fault.
- Guarded deployment of the release candidate with its complete record
  (VA-10), then its 14-day soak against the roadmap's soak criteria C1 to C10
  (VA-16). The sustained-load case is the soak's exposure criterion C10, and
  resume is not claimed (below). For v0.2.0 the field evidence above holds
  daemon runs of up to 10.3 days and the sustained heat of 2026-10-18 and
  2026-10-19.
- One attended boot with `module_blacklist=msi_ec` on the kernel command line,
  which confirms the visible driver-loss failure: the primary unit fails before
  any EC write, the keeper is skipped, and `msi-fan-profile status` prints
  `ERROR: msi_ec driver not loaded for kernel <release>` and exits `1`; then a
  normal boot (VA-14). The deployment of v0.2.0 and the readiness hook on a
  real kernel installation are recorded in the field evidence above.
- One attended window with timing diagnostics on and its figures recorded,
  then confirmation that diagnostics are off again (VA-11).
- Mock-EC fault injection at every EC access of the driver's `fan_curve` store
  transaction, for the deployed legacy build (VA-8) and for the new driver
  build (DR-10). `scripts/verify-driver-build.sh` checks compilation and module
  identity, not transaction behavior under injected I/O faults.

## Known limitation of 0.2.x: the fan-2 curve

On 0.2.x, fan 2, the GPU-side fan, runs one step below the Candidate 14 table
while the machine warms up. Next to each curve the EC keeps six falling
thresholds, which decide when a step is left again. The legacy driver reads,
writes and verifies the six rising thresholds and seven speeds of each fan, 26
bytes in all, and does not touch the 12 falling-threshold bytes. Their factory
values, captured during calibration, lie above Candidate 14's rising
thresholds for fan 2 (research item DR-15). Fan 1 is essentially unaffected by
them: its thresholds of 70 to 88 °C are the factory ones and keep their 3 °C
hysteresis, observed at 79 °C and inferred at 67, 73 and 85 °C; at its 50 and
60 °C thresholds, one degree under the falling values 51 and 61, the absence
of hysteresis is inferred, not observed (DR-15). For fan 2, by evidence level:

- Observed: the EC's fan-2 target alternates once a second between the table's
  step and the one below at a GPU temperature of 43 and 44 °C, and on warm-up
  fan 2 runs one step below the table up to 54 °C (DR-15: a 90 s read-only
  sample, and the minute status lines of the field evidence above). At 44 to
  54 °C that is about 3,750 RPM (status lines) instead of the 70 step's 4,330
  (calibration rows).
- Inferred from the factory falling thresholds: the same up to 78 °C, with the
  step at 79 °C and above held.
- From two rows of the calibration table only (`docs/CALIBRATION.md`): about
  4,300 RPM at 55 to 69 °C, where the table implies about 6,150.
- Unknown: any long-term effect of the alternation, and whether this unit
  honors falling thresholds that are written to it.

The alarms and Cooler Boost do not depend on the curve. That every published
measurement of Candidate 14 was taken with this behavior present is inferred
from the factory falling thresholds as well, because the legacy driver cannot
read them (DR-15). The fix is planned before 1.0 under decision D-2: a new
driver build that also writes the falling thresholds, and a curve re-tuned with
them (DR-10, DR-12, DR-18, DR-20). DR-16 adds a description of falling
thresholds and of the curve the EC executes to `docs/PROFILE.md`.

## Not claimed

- Other BIOS versions
- Other EC firmware versions
- RTX 5090 A2XWJG behavior
- Other MS-1824 marketing variants
- Other Ubuntu/kernel releases, including an upgrade to Ubuntu 26.04; field
  operation on 7.0.0-34, 7.0.0-38 and 7.0.0-42 is not attended validation
- Hibernation behavior
- Suspend and resume (decision D-3). The daemon detects a suspend from the
  discontinuity between `CLOCK_BOOTTIME` and monotonic time and forces a full
  safe reapply, but that path is covered by mocked tests only and has not run
  on the hardware with the current code. The validation host's `sleep`,
  `suspend`, `hibernate` and `hybrid-sleep` targets have been masked since
  2026-09-29 and stay masked, and Secure Boot lockdown disables hibernation
  there. Keep suspend masked on such a host.
- Hard CPU temperature enforcement below 90°C
- That Candidate 14 is optimal or holds a hard thermal ceiling, thermal
  comparisons beyond the short runs in `docs/CALIBRATION.md`, and any noise,
  throughput or battery-life comparison (decision D-4): Candidate 14 is
  measured and promising, not proven optimal. Nor is a throughput or
  battery-life improvement claimed for the controller's own efficiency
  changes, which rest on offline tests.
- Battery operation: no test on battery power is recorded.
- Uninstall and fresh installation on real hardware: the scripts are tested in
  a sandbox only (fresh installation has no sandbox test until LD-2), and the
  validation host is updated with the guarded upgrade.
- Concurrent kernel-store tests, with simultaneous writes to the driver's
  `fan_curve` store, and the real EC's behavior when an EC access fails; fault
  injection uses a mock EC (see Pending).
- Generic fan-curve editing

Contributions for another exact configuration should include DMI/BIOS/EC
identity, curve backup, physical RPM verification, failure recovery, and
repeatable measurements. Do not submit “works on MSI” claims without exact data.
