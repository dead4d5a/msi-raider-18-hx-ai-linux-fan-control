# Changelog

## Unreleased

Entries for the next version go below this paragraph, one or two sentences
each, under the headings `### Changed`, `### Fixed`, `### Added` and
`### Operator-visible`, in that order; a heading without entries is left out.
Changed is for behavior that differs, Fixed for a corrected defect, Added for
something new, and Operator-visible for a change to what an operator reads or
follows, such as a status field, a log line, a message or a documented
procedure. An entry goes under the one heading that names its main effect and
ends with its roadmap item ID in parentheses, and with its issue number too
once that issue exists: `(ST-1)`, later `(ST-1, #N)`. When a version is cut,
its section starts with a short summary, upgrade notes and known issues.

## 0.2.1

Hotfix for the watchdog kills of 2026-11-10 and 2026-11-13, in which a
host-wide out-of-memory stall delayed the daemon past its 5 s systemd
watchdog. On 2026-11-13 the keeper that took over was then killed by its own
5 s watchdog three times, and systemd refused to restart it. Both units now
use a 30 s watchdog, and a failed runtime watchdog notification no longer
ends the daemon or the keeper. The length of each stall the daemon or keeper
survives and where the time went, the reason each unit last stopped and the
EC state the daemon finds at startup now show in the journal or in
`msi-fan-profile status`, and the installed documents describe host memory
stalls, with runbooks for a watchdog kill and a torn recovery latch.
Candidate 14, the alarm limits and the accepted driver builds are those of
0.2.0.

### Upgrade notes

- Upgrade from 0.2.0 with the guarded `sudo ./scripts/upgrade-profile.sh`
  (`docs/INSTALL.md`). `upgrade-profile.sh`, `preflight.sh` and
  `verify-release-unprivileged.sh` are byte-identical to 0.2.0, and
  `verify-release.sh` differs only in the keeper's pinned `WatchdogSec`. No
  installed file is added or removed, and the driver is not touched.
- The upgrade hands over through the installed 0.2.0 manager's
  `factory-auto`, which releases Cooler Boost only after six consecutive
  valid, cool samples and does not disable the service when it refuses. If it
  refuses, the upgrade continues at full cooling when the state the refusal
  left verifies, and otherwise stops with nothing replaced; rerun the upgrade
  once the machine is cool, or run `sudo msi-fan-profile apply-default` to
  resume the installed profile now (`docs/INSTALL.md`).
- After the upgrade, `systemctl show -p WatchdogUSec msi-fan-profile.service`
  prints `WatchdogUSec=30s`. systemd reports a unit's watchdog interval as of
  the unit's last start, so until the keeper next starts, the same query for
  `msi-fan-profile-keeper.service` can print an older value or `infinity`;
  check `WatchdogSec=30s` in its installed unit file,
  `/etc/systemd/system/msi-fan-profile-keeper.service`, instead.

### Known issues

- A host-wide out-of-memory stall longer than 30 s still ends in a watchdog
  kill, and the keeper then holds Candidate 14/`advanced` with Cooler Boost on
  until `sudo msi-fan-profile apply-default` is run; no reboot is needed. If
  the keeper is killed three times within 15 minutes, as on 2026-11-13,
  systemd refuses to restart it, and the EC keeps full cooling with no
  process supervising it until `apply-default` (`docs/OPERATIONS.md`, "After
  a watchdog kill").
- If the recovery helper or the keeper is killed between creating
  `/run/msi-fan-profile.recovery-boost` and writing its payload, it leaves an
  empty file that no `msi-fan-profile` command removes: the daemon then
  refuses to start, the keeper exits at every start, and `apply-default`
  fails. `factory-auto`, and an upgrade, which hands over through it, must not
  be run in that state (`docs/OPERATIONS.md`, "Torn recovery latch").
- Fan 2, the GPU-side fan, still runs one step below the Candidate 14 table
  while the machine warms up: about 3,750 instead of 4,330 RPM at a GPU
  temperature of 44 to 54 °C (observed). The driver writes the six rising
  thresholds and seven speeds of each fan but leaves the EC's 12
  falling-threshold bytes, which decide when a step is left again, at their
  factory values; for fan 2 those lie above Candidate 14's rising thresholds.
  0.2.1 does not change this. The alarms and Cooler Boost do not depend on
  the curve. Under decision D-2 (option (b), decided on 2026-11-13), a driver
  build that also writes those 12 bytes and a re-tuned curve are planned for
  1.0; whether the EC honours written falling thresholds is not yet proven.

### Changed

- A failed runtime watchdog notification no longer ends the daemon, after
  which the keeper took over at full cooling, or the keeper: a warning is
  logged, the loop continues, and systemd's watchdog decides if notifications
  stay broken. `READY=1` and the heartbeats during startup, a Candidate 14
  reapply and the clean stop stay strict (ST-2).

### Fixed

- Raise `WatchdogSec` of the controller and the keeper from 5 s to 30 s, so
  that a host-wide out-of-memory stall shorter than the watchdog no longer
  makes systemd kill a healthy daemon or keeper; the 5 s watchdog killed the
  daemon on 2026-11-10 and the daemon and the keeper on 2026-11-13. The daemon
  still latches Cooler Boost in the first pass that sees a gap of more than
  2 s, but a daemon stall of 7 to 30 s now passes without Boost until the
  daemon resumes, and `systemctl show -p WatchdogUSec msi-fan-profile.service`
  prints `WatchdogUSec=30s` (ST-1).

### Operator-visible

- A monitoring stall logs one WARNING with its length to 0.1 s, split into the
  previous pass after its acquisition, the sleep overrun, this pass before its
  acquisition and the acquisition (for a gap, the previous one), with each
  channel's read time for a slow acquisition, and then a
  `stall memory pressure` line with the `some` and `full` memory pressure of
  the unit and the host, read after the Cooler Boost write and the warning,
  and the seconds since the stall, which no later warning counts again; the
  WARNING replaces the `sensor telemetry degraded` warning of a slow
  acquisition, and a gap that only repeats reported time logs nothing more.
  The keeper reasserts Cooler Boost and then logs one such WARNING and line
  when a pass, with any overrun of the sleep before it, takes more than 2 s;
  no decision and no health field uses these figures (ST-3).
- `msi-fan-profile status` says why each unit last stopped: `service_result`
  and `keeper_result` give systemd's `Result`, such as `watchdog`, `oom-kill`
  or `exit-code`, `service_restarts` and `keeper_restarts` its restart count,
  and `service_main_code` and `service_main_status`, with the same two
  `keeper_` keys, how the last main process ended. `controller_memory` gains
  `locked_bytes` and `memory_min_effective`, which is `false` when an ancestor
  cgroup reserves less than the unit's `MemoryMin`, as on the validation host;
  none of the new keys enters the exit status or the stable comparison (ST-4).
- `docs/SAFETY.md` has a new section, "Host memory stalls": what the daemon
  does when a stall makes it late, what the 30 s watchdog is for and costs,
  what a kill leads to, what cannot be guaranteed, and the unsupervised full
  cooling left when the keeper reaches its start limit, as observed on
  2026-11-13. `docs/OPERATIONS.md` gains runbooks for a watchdog kill and for
  a torn recovery latch, an empty `/run/msi-fan-profile.recovery-boost` after
  which `apply-default` fails and `factory-auto` must not be run, and
  `docs/VALIDATION.md` records the stalls of 2026-10-24 to 2026-11-13 as field
  evidence (ST-5).
- At startup the daemon logs `pre-apply EC state: curve=... mode=... boost=...`
  between the three EC reads it already makes and its first write, with the
  curve in the words of `msi-fan-profile status` (`factory`, `candidate14` or
  `unknown`), so the journal shows the EC state each start found (after a cool
  clean stop, the factory state the daemon itself restored), also for a state
  it then refuses with the existing `unrecognized pre-apply state` error. No
  EC access is added, `Candidate14 applied` is unchanged, and in-loop
  reapplies log nothing new (VA-7).

## 0.2.0 - 2026-10-11

Hardening release for the same laptop and firmware. A genuine daemon fault
now ends in full cooling, Candidate 14 in `advanced` mode with Cooler Boost
on, held by a separate keeper. A memory leak that let the 64 MiB limit kill
the controller after about 6.8 days is fixed. A read-only kernel readiness
check finds a driver build that would fail at the next boot; it is run by hand
before a reboot and by a kernel hook after each kernel installation. A clean
stop, `factory-auto` or the uninstall script switches Cooler Boost off only
after six consecutive valid, cool samples. The upgrade waits for a cool margin
and continues at full cooling when the installed manager cannot confirm a
cool release.

Deployed on the validation host on 2026-10-11 as commit `e4d4e19` and tagged
`v0.2.0` on 2026-11-11, when all 12 installed files were verified
byte-identical to that commit. At the tag, this file still lists the changes
below under Unreleased; they are moved here unchanged and predate the entry
format above.

### Upgrade notes

- Upgrade an installed profile with the guarded
  `sudo ./scripts/upgrade-profile.sh` (`docs/INSTALL.md`);
  `install-profile.sh` refuses to overwrite a deployment.
- The upgrade runs the release checks without root, which heats the CPU, and
  then waits until the CPU package and EC CPU are below 80 °C and the EC GPU
  below 65 °C for 10 consecutive seconds. If that takes longer than 600 s
  (`MSI_FAN_UPGRADE_COOL_TIMEOUT`, 10 to 86400), it exits with nothing
  changed.
- Its first change to the running controller is the installed release's
  `factory-auto`. Releases before this one decide from one reading and also
  disable the service when they refuse. If the refusal leaves verified full
  cooling, the upgrade continues at full cooling; otherwise it stops with
  nothing replaced, and `sudo msi-fan-profile apply-default` must be run
  before a cool retry.
- The upgrade adds the readiness check
  `/usr/local/libexec/msi-fan-kernel-readiness` and the kernel hook
  `/etc/kernel/postinst.d/zz-msi-fan-profile-readiness`. Run the check before
  every reboot (`docs/ROLLBACK.md`).
- The upgrade does not touch the driver. This version accepts the deployed
  legacy build of the patched `msi_ec 0.13.1` (srcversion
  `AB0BFAE2391B5ADD66E01BD`). The snapshot build it also accepts
  (`9086C45007CBB7FA1264430`) has not been deployed or validated on hardware.

### Known issues

- A host-wide out-of-memory stall can delay the daemon past its 5 s systemd
  watchdog, and systemd then kills a healthy daemon, as on 2026-11-10. The
  keeper then holds Candidate 14/`advanced` with Cooler Boost on until
  `sudo msi-fan-profile apply-default` is run; no reboot is needed
  (`docs/OPERATIONS.md`, "After a keeper takeover"). The keeper has the same
  5 s watchdog: on 2026-11-13, after such a kill of the daemon, the keeper was
  itself killed three times within four minutes, and systemd then refused to
  restart it (its start limit is three starts in 15 minutes). The EC kept
  Candidate 14/`advanced` with Cooler Boost on, but no process supervised the
  fans until `apply-default` was run. 0.2.1 raises the watchdog of both units
  to 30 s (ST-1).
- Fan 2, the GPU-side fan, runs one step below the Candidate 14 table while
  the machine warms up: about 3,750 instead of 4,330 RPM at a GPU temperature
  of 44 to 54 °C (observed). Next to each curve the EC keeps six falling
  thresholds that decide when a step is left again. The driver writes the six
  rising thresholds and seven speeds of each fan but leaves the falling
  thresholds at their factory values, which for fan 2 lie above Candidate
  14's rising thresholds. The factory values imply the same up to 78 °C;
  above 54 °C that is inferred, not observed. Fan 1 is essentially
  unaffected. The alarms and Cooler Boost do not depend on the curve, and
  every measurement behind Candidate 14 was taken with this behavior present.
  A driver build that also writes the falling thresholds, followed by a
  re-tune of the curve, is planned for 1.0 (decision D-2).

### Changes

- Continue an upgrade at full cooling when the installed manager cannot
  confirm a cool release. An upgrade runs the installed release's
  `factory-auto`, and releases before this one decide from one reading, which a
  spike can refuse even right after the cool-down gate, as on 2026-10-11. That
  refusal leaves the factory curve in `auto` mode with Cooler Boost on, both
  units stopped, the `factory-auto` marker and the recovery-Boost marker that
  `ExecStopPost` wrote after the hot clean stop; those managers also disable
  the service. The upgrade used to stop there, and the operator had to run
  `apply-default` and retry, which could loop on a busy machine. When
  `factory-auto` exits non-zero, `upgrade-profile.sh` now reruns `preflight.sh`
  and reads both units, the markers and the EC. Only if the exact platform, the
  factory curve, `auto`, Boost `on`, both units `inactive/dead`, a root-owned,
  mode-`0600` `factory-auto` marker and a recovery-Boost marker that is a
  root-owned, mode-`0600` regular file with one link and exactly the payload
  the daemon accepts (`candidate14` and a newline) all verify, it prints
  `The installed manager left factory auto with Cooler Boost on (too hot or
  unconfirmed); continuing the upgrade at full cooling; ...`. It then disables
  the service as usual, keeps the recovery-Boost marker, replaces and records
  the artifacts with no controller running, and activates through the new
  manager's `apply-default`. The new daemon consumes the marker once it has
  reapplied Candidate 14 and keeps Boost on as its automatic latch until 30
  continuously valid, cool seconds; the upgrade ends with
  `Cooler Boost stays on as the new controller's automatic latch ...`. Any
  other result, including a missing, unsafe or malformed recovery-Boost marker,
  a failed platform check, another curve, mode or Boost state, a running unit
  or a missing `factory-auto` marker, stops as before with the manager's exit
  status and nothing replaced, after a `Not continuing at full cooling: ...`
  line that names the first failed check. After continuing, the checks after
  the disable read Boost, the keeper and the recovery-Boost marker again before
  anything is replaced. If one of them or a later step fails, the exit message
  reports what it then reads instead of assuming it: `This script did not
  change Cooler Boost or the recovery Boost latch: cooler_boost is ... and ...
  is ...`, with the marker `present`, `present but not valid` or `missing`.
  The cool handover is unchanged. The cool-down gate stays, so the
  continuation remains the exception; its samples already follow the shared
  confirmation's validity rules, and an invalid one restarts its count.
- Confirm coolness over several valid samples before releasing Cooler Boost.
  Every check that switches Boost off, or leaves the factory envelope at
  `auto/off`, used to decide from one instantaneous reading: the daemon's clean
  stop, the `ExecStopPost` helper after that stop, the manager's `factory-auto`
  and `uninstall-profile.sh`. On this laptop the coretemp package reading jumps
  20 °C or more for milliseconds under bursty load, and the EC sometimes
  returns junk such as 255 °C or an empty value. On 2026-10-11 one such reading
  0.26 s after the upgrade's cool-down gate passed made the installed daemon
  refuse its clean stop and aborted the upgrade. All four now use one shared,
  read-only helper in `msi_fan_control.py`: Boost is released only after six
  consecutive valid samples, 0.5 s apart (at least 2.5 s of agreement), show
  the CPU package and EC CPU below 85 °C and the EC GPU below 70 °C (0, a
  powered-off dGPU, counts as cool). An invalid, unreadable or hot sample
  restarts the count, and the check gives up once six samples can no longer fit
  in 10 s (after about 7.5 s when every sample is hot). A cool machine releases
  after about 2.5 s, and one spike or junk value restarts the count instead of
  refusing the release; it can never release Boost by itself either. The
  limits are unchanged. A valid sample has one to six ASCII digits within 1 to
  110,000 m°C for the package, 1 to 110 °C for the EC CPU and 0 to 110 °C for
  the EC GPU, so a CPU at 0 °C, which all three programs accepted as cool,
  values such as `+50000` or `5_0000`, which the manager's `int()` accepted, and
  an empty reading, which `uninstall-profile.sh` evaluated as 0, are now
  invalid. On refusal each program does exactly what it did before (Boost
  on; the daemon exits `1` with `factory restored; Boost release unsafe`, and
  `factory-auto` reports `factory staged with Boost on`), and the log names the
  most recent unsafe sample. When every sample was valid and cool but they
  arrived too slowly to fill the window, the log says `samples too slow` and
  `factory-auto` reports `factory restored but cool confirmation did not
  complete: ...` instead of calling the telemetry unavailable. Entering
  cooling is never delayed: in the daemon, the manager and `ExecStopPost` an
  unsafe sample reasserts Boost at once. That
  undoes a keyboard off during the wait and, in `ExecStopPost`, where the
  daemon had already released Boost, switches it back on before the verdict.
  `uninstall-profile.sh` keeps the Boost it has just set and requests it again
  when it refuses. Because its confirmation can take 10 s, it now repeats its
  exact platform and driver identity check after the confirmation and before
  its next EC write, as every other EC writer does inside each write. If
  `msi_ec` was reloaded as another build or `ec_sys` appeared meanwhile, it
  writes nothing more, exits `1` with `The exact platform or driver identity
  changed during the cool confirmation; ...` and removes nothing. The daemon
  sends a watchdog heartbeat with every sample,
  because a stop started by the `factory-auto` marker still runs under
  `WatchdogSec=5s`. A cool clean stop now takes about 2.5 s longer, and a cool
  `factory-auto`, which confirms three times (daemon, `ExecStopPost`, manager),
  about 7.5 s longer; each wait ends within about 10 s. A stop that arrives
  while the daemon is still starting is handled after up to 10 s of hwmon
  wait and 12 s of Boost verification, so the daemon records when the first
  stop signal arrived and gives the confirmation only what remains of
  `TimeoutStopSec=30s` less a 5 s exit margin. With less than 2.5 s left it
  refuses the release without sampling (`WARNING Cooler Boost release refused:
  N s of the 30 s stop timeout remain ...`) and exits `1` into full-cooling
  recovery, as after a hot stop, instead of being killed by systemd. The
  runtime 30-second cooldown is unchanged; it already
  requires continuously valid, cool samples. An upgrade from an earlier release
  still runs that release's single-reading checks in its `factory-auto`, so the
  upgrade's cool-down gate stays.
- Make `upgrade-profile.sh` wait for a cool margin before it changes anything
  on the running system. Its release checks heat the CPU, and the installed
  manager's `factory-auto` refuses to release Cooler Boost at a CPU package or
  EC CPU temperature of 85 °C or an EC GPU temperature of 70 °C; managers before
  this release then also disable the service, and a retry reran the checks, so
  the upgrade could loop. After the release checks and staging, the upgrade now
  reads the manager's own sensors until the CPU package and EC CPU are below
  80 °C and the EC GPU below 65 °C for 10 consecutive one-second samples, never
  counting an unreadable or invalid reading such as the EC's 255 as cool. It
  prints the readings every 30 seconds, and after 600 seconds
  (`MSI_FAN_UPGRADE_COOL_TIMEOUT`, 10 to 86400) exits `1` with nothing changed.
  If a deployed source file changes during the wait, for example through a
  `git pull`, it also exits `1` with nothing changed instead of failing its
  post-install check after `factory-auto`, and `INSTALL_RECORD` names the commit
  and manifest from before the wait.
- Document what a clean stop does while hot: the daemon exits `1` with
  `factory restored; Boost release unsafe`, and recovery returns the EC to
  Candidate 14/`advanced` with Cooler Boost on. Record the 2026-10-10/11 field
  evidence: the OOM kill of the 2026-09-30 build, the unattended keeper
  takeover and `apply-default` handoff, and the 7.0.0-34 to 7.0.0-38 reboot.
  Add runbooks for a keeper takeover, a driver that did not load, `shift_mode`
  drift, the boot window, and kernel, release and firmware updates.
- Track automatic and manual Cooler Boost ownership independently, as
  `SAFETY.md` requires. A thermal alarm, invalid telemetry or an observation
  gap now sets the automatic latch even while manual Boost is on, and a
  `boost-on` request no longer cancels the latch. A keyboard or external off
  during the latch withdraws manual Boost while the latch reasserts Boost, so
  that off can no longer skip the 30 continuously valid, cool seconds after an
  alarm or sensor fault. The latch's cooldown switches Boost off only when no
  manual Boost remains, and a queued manual release never switches Boost off
  under the latch. Health reports both owners.
- Stop reading the EC firmware twice per second on the legacy driver. The
  daemon and keeper loops read `fan_curve` first and rely on its in-kernel DMI
  and firmware check, which fails the read on a mismatch, instead of a separate
  `fw_version` read; the daemon checks `shift_mode` after it and names a
  refused read as that check. The daemon also reuses the Boost value it reads
  for fan-response evidence. A steady legacy pass drops from 57 to 44 EC bytes
  in the daemon and from 52 to 40 in the keeper. Every write and the manager's
  status keep their own `fw_version` read.
- Test allocator growth across thousands of passes of the real daemon main loop
  and keeper loop on a private file-backed sysfs tree, with plain-function
  fakes, so a per-pass leak anywhere in either loop fails the suite, not only
  one in health publication. A 5000-pass warm-up keeps one-time allocator
  growth from failing the release checks at random.
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
  instructions. A later failure after a successful `factory-auto`, before any
  replacement, is reported as nothing replaced with the service disabled, and
  `apply-default` before rebooting, not as a hot refusal.
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
  The installed `OPERATIONS.md` that the hook names explains the checks itself
  and names uninstalled documents by their source checkout path.
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
