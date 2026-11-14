# Audit record

The audit of October 2026 examined this project in five areas: the kernel
and driver (K), the daemon (D), the recovery keeper and manager (R),
efficiency (E), and the scripts, units and tests (S). It produced 46
findings, and a verification pass checked each one and adjusted its
severity. A completeness review then added ten review gaps (RG). Most
findings were addressed, at least in part, by the commits of 2026-10-11 up
to `e4d4e19` (`v0.2.0`). This record lists every finding and review gap
once, with its subject, its severity, what became of it and an anchor that
can be checked.

## How to read this record

Each finding is worded as the audit found it in October 2026. Severity uses
the audit's scale, high, medium, low or info, as it stood after the
verification pass. The review gaps had no verification pass; their severity
is the completeness review's own.

Each row has one disposition:

- fixed: a change in this repository, cited by its commit, resolves the
  finding;
- closed by evidence: an event or a measurement, cited in the anchor, shows
  that the finding no longer applies;
- accepted: the behaviour stays on purpose, as the anchor describes;
- dropped: no change is planned, for the reason given;
- deferred beyond 1.0: the change follows the 1.0 release, for the reason
  given;
- host action: the owner acts on the validation host, outside the files this
  project installs;
- open: the work is not finished, and the anchor names the work items that
  carry it.

When a fix left part of a finding for later, the disposition is followed by
`left:`, that part and its own disposition, with the work item that carries
it where there is one.

An anchor is a commit, given by its short hash, a file and line, a CI run,
the evidence at the end of this file, or a work item. File and line
references are to `v0.2.0`: `git show v0.2.0:docs/SAFETY.md | sed -n 95p`
prints the line cited as `docs/SAFETY.md:95`. The commits `afddd83` (ST-2),
`6ec0761` (ST-4), `c29e7fc` (ST-5) and `48041b2` (RL-2a) came after
`v0.2.0` and belong to 0.2.1. Work items such as ST-9 are those of the
roadmap to 1.0; each gets a link to its GitHub issue once the issues are
published.

| Disposition | Rows |
|---|---:|
| fixed | 18 |
| closed by evidence | 5 |
| accepted | 4 |
| dropped | 4 |
| deferred beyond 1.0 | 1 |
| host action | 2 |
| open | 22 |
| total | 56 |

## Kernel and driver

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| K1 | An unusable driver on a new kernel makes the controller and its keeper skip silently: no failed unit, no `OnFailure`, no alert | medium | fixed; left: a failure notification, open (ST-17) | `0b95cea` removed the primary unit's `fan_curve` condition, so a missing or unusable driver now fails the unit, and `msi-fan-profile status` names the cause |
| K2 | No readiness check or alert before a reboot: the DKMS outcome of an unattended kernel installation stays unseen until the next boot | low | fixed; left: a desktop note, open (ST-17) | `b306c7e` added the read-only check `scripts/check-kernel-readiness.sh` and the kernel hook `/etc/kernel/postinst.d/zz-msi-fan-profile-readiness` |
| K3 | CI builds neither the kernel series nor the driver source the laptop runs, and never runs unprompted | low | closed by evidence | `9b6811a` restored the deployed legacy patch and added a driver-build matrix and a weekly schedule; CI run 42631122198 (evidence below) |
| K4 | The `srcversion` pin depends on Kbuild's dependency-path layout and does not cover `ec_memory_configuration.h` | low | open | LD-12; the pins are `src/msi_fan_control.py:18-20` |
| K5 | A stale `msi_ec/0.13` DKMS registration with `AUTOINSTALL=yes` becomes the driver for new kernels whenever 0.13.1 is unregistered | info | closed by evidence; left: the unregistered source directory `/usr/src/msi_ec-0.13`, host action (LD-16) | `b306c7e` makes the readiness check warn about another registered version (`scripts/check-kernel-readiness.sh:288-306`); the check of 2026-11-11 printed no warning, and DKMS listed only 0.13.1 (evidence below) |
| K6 | The fallback kernel 7.0.0-28 has no `msi_ec` build, and removed kernels leave files behind | info | closed by evidence; left: the boot and module files of removed kernels, host action (LD-16) | `b306c7e` makes the readiness check warn about an installed kernel without a build (`scripts/check-kernel-readiness.sh:308-312`); the check of 2026-11-11 printed no warning (evidence below) |
| K7 | Start-up hwmon discovery has no ordering or retry against udev | low | fixed | `0b95cea` waits up to 10 s at start-up for the hwmon devices, with watchdog heartbeats |
| K8 | The GPU driver's kernel modules are missing for kernels from 7.0.0-34 on; the project does not depend on them | info | closed by evidence | The module is loaded again (evidence below); `d3bfadb` describes the effect on idle fan speed in `docs/OPERATIONS.md:450-465` |

## Daemon

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| D1 | Manual Cooler Boost cancels or suppresses the automatic latch, so an external Boost off skips the 30 s cooldown after an alarm or a sensor fault | low | fixed; left: two residuals, accepted | `f87e432` keeps the automatic latch independent of manual Boost. The residuals: a queued `boost-release` is acknowledged only by the latch's own release (`docs/SAFETY.md:215-221`); and an off that lands in the milliseconds between two Boost reads of one pass is reasserted without being counted as a withdrawal, so Boost then stays on as manual Boost, the noisy direction |
| D2 | Three main-loop tests read the real `/run` recovery latch, so the release checks, and an upgrade with them, fail whenever a keeper latch exists | medium | fixed | `6b91e17` moved the main-loop, manager and recovery tests off the host's `/run`, `/sys` and systemd |
| D3 | A resume or runtime-revert reapply switches Boost off, and the gap check latches it again at once: needless EC writes and false warnings | low | open | LD-8 |
| D4 | A runtime EC revert to factory auto is reapplied without a journal entry, a counter or a rate limit | low | open | LD-8 |
| D5 | hwmon paths are resolved once through the `/sys/class/hwmon/hwmonN` links and never pinned or resolved again | info | open | LD-9 |
| D6 | A transient `sd_notify` or acknowledgement-write failure ends the daemon at once instead of letting the watchdog decide | info | open | ST-8 for acknowledgement writes; `afddd83` (ST-2) already lets a failed runtime notification pass to the watchdog |
| D7 | Controller memory is swappable and not protected from the OOM killer | low | open | ST-4, whose check on the validation host follows the deployment of 0.2.1, and ST-7. `0b95cea` set `MemorySwapMax=0`, `MemoryMin=32M` and `OOMScoreAdjust=-900` on both units; `MemoryMin` has no effect on the validation host, which `6ec0761` (ST-4) reports in `status` and `c29e7fc` (ST-5) documents; ST-7 will lock the memory instead |
| D8 | Test gaps around timing-dependent and I/O paths | low | open | LD-8 for a slow-reapply test and ST-8 for the acknowledgement path. Done: the whole-loop allocation tests (`7c198b5`), the Boost ownership scenarios (`f87e432`) and failed runtime notifications (`afddd83`) |
| D9 | Each pass is dominated by deliberate checks; only small redundant EC reads remain | info | fixed | `2fe747e` drops the separate firmware read of each legacy monitoring pass and reuses the Boost value read for the fan-response check; the deliberate checks stay |

## Recovery keeper and manager

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| R1 | A hot `factory-auto` disables the service and cannot succeed afterwards: Boost forced on, upgrade retries blocked | medium | fixed; left: a residual, accepted | `13b85bc`: a refused cool release exits 1 with Boost on and keeps the service enabled and the recovery latch; `7853461` corrects the upgrade's message when a later step fails after `factory-auto`. The residual: a refusal with no recovery latch leaves Boost on as manual Boost until `boost-release`, the noisy direction; LD-14 will document it |
| R2 | `ExecStopPost` returns 0 for systemd `success` results that are not a clean exit, so full cooling is staged but no keeper starts | info | accepted | Kept on purpose: it needs a stop within about 100 ms of a start, it leaves full cooling staged, and a failure status would start the keeper after such operator stops; `c5e4aeb` pins the behaviour in a test |
| R3 | Latch creation is not atomic; a torn or invalid latch disables the keeper, the daemon start and `factory-auto` | info | open | ST-9; until then `c29e7fc` (ST-5) documents the way out in `docs/OPERATIONS.md`, section "Torn recovery latch" |
| R4 | `factory-auto` creates its marker before stopping the daemon, and the daemon treats the marker as a fault | low | fixed | `13b85bc` makes the daemon take a new marker as a clean stop request |
| R5 | The 3,000 RPM fan-response floor is met without Boost, so recovery and the keeper cannot detect an ineffective Boost command | info | accepted | The documents call it a response floor, not proof of maximum speed: `docs/SAFETY.md:95`, `docs/ARCHITECTURE.md:81`, `docs/OPERATIONS.md:160-161` |
| R6 | The root programs use `#!/usr/bin/env python3`, so their interpreter depends on `PATH`, where `/usr/local/bin` comes first | low | fixed | `0b95cea` pins `#!/usr/bin/python3 -I` in all three programs |
| R7 | No tests for the recovery decisions or the `factory-auto` failure paths; the manager tests have no guard against the real hardware or systemd | medium | fixed; left: a residual, accepted | `6b91e17` guards the manager tests against the host; `c5e4aeb` tests the `ExecStopPost` decisions, the Boost release, the latch and the lock; `13b85bc` runs `factory-auto` against an in-memory systemd and EC. The residual: no test compares the latch payload across programs; each defines the same `RECOVERY_LATCH_PAYLOAD` (`src/msi-fan-profile:36`, `src/msi-fan-profiled:34`, `src/msi-gpu-recover:29`) |
| R8 | `boost-release` waits by running `systemctl show` every 250 ms, with no time limit | info | dropped | The cost is small and arises only while a release is pending; the wait ends with the daemon's acknowledgement, with a newer `boost-on`, or when the daemon stops or is replaced (`src/msi-fan-profile:642-670`, `:709`) |

## Efficiency

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| E1 | Monitoring on the legacy driver reads the EC firmware version twice every second, in the daemon and in the keeper | low | fixed; left: the further saving from the snapshot build, dropped | `2fe747e` skips the separate firmware read in the daemon and keeper loops. The snapshot build's saving is moot since that commit, and that build has never been loaded |
| E2 | Cooler Boost is read from the EC up to three times per pass although the value is known | info | fixed; left: the other reads, accepted | `2fe747e` reuses the value of the fan-response read. The reads at the start of a pass, before a reassertion, on the degraded path, for a release request and for the minute status line, and the readbacks after writes, stay, so that each decision uses a fresh value |
| E3 | Each sysfs read costs nine syscalls and rebuilds pathlib objects | low | dropped | The daemon uses about 0.3 % of one core (evidence below); the read is `src/msi-fan-profiled:86-88`. Kept-open descriptors (ST-12) come only if measured stalls call for them |
| E4 | Health publication uses `json.dump` on a text stream, which falls back to the slower pure-Python encoder | info | dropped | About 0.3 % of one core, as above; `src/msi-fan-profiled:584` |
| E5 | `verify_boost` writes Cooler Boost on every attempt, a read-modify-write of a shared EC byte | info | open | LD-9; the write is `src/msi-fan-profiled:237-242` |
| E6 | The daemon reads telemetry through `hwmonN` links captured at start-up, not through the devices' resolved paths | low | open | LD-9 |
| E7 | Without the GPU driver's kernel module from 7.0.0-34 on, the idle discrete GPU stays fully powered and drives fan 2 to about 4,200 RPM | medium | closed by evidence | The module is loaded again (evidence below); `d3bfadb` documents the effect in `docs/OPERATIONS.md:450-465` |
| E8 | No whole-loop memory regression test and no runtime memory telemetry | low | deferred beyond 1.0 | LD-13 will add a minute-level memory series. Reason: `status` reports the controller's memory (`0b95cea`), the whole-loop allocation tests exist (`7c198b5`), and the soak captures give three points |

## Scripts, units and tests

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| S1 | An upgrade is blocked whenever a keeper recovery latch exists: three daemon tests read the real `/run` latch | medium | fixed | `6b91e17` moves the latch of those tests into a temporary directory; `7859ca0` puts a latch into the upgrade's sandbox test |
| S2 | Install and upgrade run the whole unit-test suite as root on the live machine | low | open | ST-6. `7859ca0` runs the release checks without root. That the drop from root to the invoking user works is inferred from the two real upgrades of 2026-10-11, whose terminal output was not kept; the transcript of session A is to show its `Running release checks as uid` line |
| S3 | Kernel upgrades are unverified until after boot, and a missing patched driver makes the controller skip silently | low | fixed; left: a boot-time check, accepted | `0b95cea` (visible failure) and `b306c7e` (readiness check). The boot-time check was not built: a missing driver already fails the unit, a status check at boot risked false failures, and the opt-in notifier of ST-17 will report an absent controller from its timer |
| S4 | CI builds neither the deployed legacy driver nor the HWE 7.0 kernel series, runs on no schedule, and does not assert the Python minor version | low | open | LD-7 for the Python-version assertion. The CI part is closed by evidence: `9b6811a` and CI run 42631122198 (evidence below) |
| S5 | The memory limit is not a hard bound, since swap is unbounded, and nothing warns before an out-of-memory kill | low | open; left: a memory warning, deferred beyond 1.0 (LD-13) | ST-4, whose check on the validation host follows the deployment of 0.2.1: `6ec0761` adds the restart counts to `status`. `0b95cea` made `MemoryMax=64M` a hard bound with `MemorySwapMax=0` and added `controller_memory` to `status`. The warning waits because the bound is hard and `status` already shows the figures |
| S6 | A failure after file replacement leaves old and new files mixed, a stale `INSTALL_RECORD` and no documented restore | low | open; left: a two-phase replacement, deferred beyond 1.0 | LD-5 will add a marker, a failure message with the backup path and a restore procedure. The two-phase replacement waits because those make a partly replaced install visible and recoverable, which is what the roadmap's default for a partial upgrade asks of 1.0 |
| S7 | The leak test covers only `publish_health`; no whole-loop test for the daemon or the keeper | low | fixed | `7c198b5`; `7853461` lengthened the warm-up to 5,000 passes |
| S8 | The unit-file tests pin few of the directives the safety model depends on, and the install and uninstall scripts have no tests | low | open | LD-2 for the install script. `0b95cea` pins every safety-relevant unit directive, with a deny-list, and `e4d4e19` added a sandbox test of the uninstall script |
| S9 | The root services choose their interpreter through `PATH`, with `/usr/local/bin` first | low | fixed | `0b95cea` pins `#!/usr/bin/python3 -I`, and the release checks verify it |
| S10 | The upgrade stops the keeper outside the manager lock before the identity gate of `factory-auto`, and bare `[[ ]]` assertions fail without a message | low | open | LD-4 |
| S11 | `uninstall-profile.sh` releases Boost in bash without the plausibility checks of the programs, and leaves files behind | info | fixed; left: the leftovers, open (LD-4) | `ff58abf` and `e4d4e19`: the uninstall uses the shared cool confirmation and checks the identity again before the next EC write (`scripts/uninstall-profile.sh:84-97`) |
| S12 | The units rely on `/run` being writable under `ProtectSystem=strict`, which the systemd documentation calls read-only | info | dropped | Refuted by the verification pass: under `ProtectSystem=strict` (`systemd/msi-fan-profile.service:25`) the daemon rewrites `/run/msi-fan-profile.health.json` every second |
| S13 | `INSTALL_RECORD` always says `source_tree_dirty=yes`, and nothing checks that `SHA256SUMS` lists every tracked file | info | open | LD-3 |

## Review gaps

| ID | Finding | Severity | Disposition | Anchor |
|---|---|---|---|---|
| RG-1 | The release upgrade to Ubuntu 26.04 is offered, and nothing gates the OS, Python, systemd or DKMS version | medium | open; left: deferring the offer, host action (LD-17); a CI canary, deferred beyond 1.0 (RL-11) | LD-7. `d3bfadb` documents what to do before a release upgrade (`docs/ROLLBACK.md:31-62`). The canary waits because 1.0 does not claim Ubuntu 26.04 and LD-7 will make install, upgrade and status refuse an unvalidated runtime |
| RG-2 | `docs/VALIDATION.md`, `README.md` and `docs/SAFETY.md` are not updated for the memory-limit kill of 2026-10-10 and contradict the observed hot clean stop | low | fixed | `d3bfadb` records the field evidence, the hot-stop behaviour and the runbooks; the version cut followed with the tags `v0.1.0` and `v0.2.0` of 2026-11-11 (RL-1) and `48041b2` (RL-2a) |
| RG-3 | Host memory limits: host-wide out-of-memory stalls recur on the validation host | low | host action | ST-18, which does not gate 1.0; `0b95cea` hardened the units themselves |
| RG-4 | Boot-time control gap: until Candidate 14 is applied, the EC runs its firmware curve without monitoring, and on 2026-10-11 a slow `systemd-tmpfiles-setup` stretched that to 85 s | low | accepted; left: the documented figure, open (VA-6) | `d3bfadb` documents the boot window (`docs/OPERATIONS.md:66-84`); VA-6 will correct the figure it gives |
| RG-5 | A firmware update offered by the desktop's firmware updater would bypass the BIOS and EC pre-update procedure | low | open | RL-7 will add it to the reaction table; `d3bfadb` documents it (`docs/ROLLBACK.md:3-9`) |
| RG-6 | Suspend and hibernation are disabled on the validation host, which is not documented, and the resume path stays unvalidated | low | open | PL-3 will report the sleep targets in `status` and preflight; `d3bfadb` documents the masking (`docs/OPERATIONS.md:436-448`) |
| RG-7 | A failed mount of `/usr/local`, which both units and the recovery helper need, leaves the primary unit `Dependency failed` without `OnFailure` | info | open | LD-14; the dependency is `RequiresMountsFor=/usr/local /usr` (`systemd/msi-fan-profile.service:5`, `systemd/msi-fan-profile-keeper.service:4`) |
| RG-8 | Calibration-era EC writers stay on the host, such as the untracked `/usr/local/bin/msi-fanctl` with its looser Boost-release gate | info | host action | LD-16; `d3bfadb` documents them (`docs/OPERATIONS.md:467-476`) |
| RG-9 | `shift_mode` drift is fatal to the daemon, and no project command restores it | info | accepted | A manual procedure: `docs/OPERATIONS.md:411-434` (`d3bfadb`) |
| RG-10 | Any local user can add EC read load that competes with the daemon's 2 s acquisition budget | info | open | LD-14 and VA-11 |

## Concerns from the final verification

The final verification of the October fixes raised the concerns below. Its
other concerns repeat rows above: a partly replaced install, `MemoryMin`
without effect on the validation host, the dirty flag in `INSTALL_RECORD`,
and gaps that no fix had addressed, namely the OS gate, the `/usr/local`
mount and EC read contention. The last two rows are notes from the same
work: the sensor-debounce change `ff58abf` and the verification pass.

| Concern | Disposition | Anchor |
|---|---|---|
| The release checks heat the CPU to about 98 °C just before the upgrade's `factory-auto` step, so the installed manager can refuse the cool release; seen on 2026-10-11 | open | LD-15 will run the checks at idle priority on efficiency cores. Meanwhile `c26e1bf` waits for a cool margin before the hand-over, and `2e46cae` continues at full cooling when a refusal leaves verified full cooling |
| During the upgrades of 2026-10-11 the installed manager and daemon still behave as before the fixes: a hot `factory-auto` disables the service, and the daemon can take the `factory-auto` marker for a fault | closed by evidence | The upgrades ended with `e4d4e19` installed, as section 0.2.0 of `CHANGELOG.md` records (`48041b2`); every hand-over since then uses the fixed manager and daemon |
| The release checks' drop from root to the invoking user has never run in a real upgrade | open | ST-6: inferred from the two upgrades of 2026-10-11, to be confirmed by the transcript of session A |
| The whole-loop memory tests are statistical; a rare spurious failure would stop an upgrade before it changes anything | accepted | `7853461` lengthened the warm-up to 5,000 passes; `CHANGELOG.md:136-140` |
| CI is unproven: the driver-build matrix and the weekly schedule have never run, and the legacy pin for kernels before 6.13 is computed, not measured | closed by evidence | CI run 42631122198 (evidence below) |
| The readiness hook runs from `/etc/kernel/postinst.d` only, so it can report a spurious FAIL when header packages are configured after the kernel image | open | LD-6; until then `docs/ROLLBACK.md:134-135` says to run the check again by hand |
| Behaviour an operator sees changes: a keyboard off during an automatic latch is reasserted; a manual `boost-on` during a latch keeps Boost on after its cooldown; a missing driver fails the primary unit; a hot clean stop ends in Candidate 14 with Boost on and a failed unit; a hot `factory-auto` exits 1 with Boost on | closed by evidence | Each is documented: `docs/SAFETY.md:210-233` and `:323-351`, `docs/OPERATIONS.md:187-234` and `:343-369` |
| The runtime 30 s cooldown accepts a reading of 0 °C for the CPU package and the EC CPU | accepted | `docs/SAFETY.md:316-321`: one such sample cannot complete 30 continuous seconds while the other samples are hot |
| The daemon and the keeper open a new notify socket for every message; one socket could be reused | open | ST-8 |

## Evidence cited above

### CI run 42631122198

[Run 42631122198](https://github.com/dead4d5a/msi-raider-18-hx-ai-linux-fan-control/actions/runs/42631122198)
of the `checks` workflow ran on its weekly schedule at `e4d4e19` on
2026-11-09. The `static` job and the four driver builds passed. The legacy
build against `linux-headers-generic` selected 6.8.0-149-generic and
printed:

```text
Built module srcversion: 518BD36E89E2B3026A56490
Pinned legacy driver patch and unprivileged build verification: PASS (6.8.0-149-generic)
```

That `srcversion` is the legacy build's CI-only pin for kernels before 6.13
(`CONTRIBUTING.md:94-97`), which until then had been computed, not measured.
The scheduled runs of 2026-10-12, 2026-10-19, 2026-10-26, 2026-11-02 and
2026-11-09 all passed at `e4d4e19`. GitHub keeps run logs for a limited
time, so the two lines are quoted here.

### Readiness check of 2026-11-11

Run by hand without root on 2026-11-11, the installed check
`/usr/local/libexec/msi-fan-kernel-readiness` printed 12 PASS lines and no
WARN line for 7.0.0-42-generic, ended with
`KERNEL READINESS PASSED: 7.0.0-42-generic` and exited with status 0. The
check warns about another registered `msi_ec` version with `AUTOINSTALL`
set and about an installed kernel without an `msi_ec/0.13.1` build
(`scripts/check-kernel-readiness.sh:288-312`), so neither existed. On the
same day `dkms status` listed only `msi_ec/0.13.1`, installed for
7.0.0-34, 7.0.0-38 and 7.0.0-42. The raw output is not reproduced here,
because it names the module-signing certificate.

### The GPU driver's kernel module

The kernel module of the discrete GPU's driver, missing for 7.0.0-34 and at
first for 7.0.0-38, was in place and loaded under 7.0.0-38 by the end of
2026-10-11, and it was loaded under 7.0.0-42 on 2026-11-11. The controller
does not use it; it reads the GPU temperature from the EC.

### CPU use of the daemon

When the daemon's run of about 10.3 days ended on 2026-11-10, systemd
reported `Consumed 45min 21.802s CPU time` for it, about 0.3 % of one core.

## Keeping this record current

When a work item named here is finished, its rows change to their final
disposition, with its commit or evidence as the anchor. RL-4 will make a
current record a precondition of each release, and RL-10 will check before
1.0 that no row is open.
