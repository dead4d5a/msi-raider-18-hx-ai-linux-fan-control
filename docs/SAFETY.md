# Safety model and architecture

## Threat and failure assumptions

An EC write can affect cooling before user space can recover. The design
therefore prefers a noisy machine over an under-cooled machine and limits every
write to exact, reviewed values.

## Layers

### 1. Exact-platform driver interface

The local `msi_ec` patch exposes one whole-curve transaction only on the exact
DMI/BIOS/EC combination. It:

- parses exactly 26 decimal bytes;
- rejects nonmonotonic or weak upper curves;
- establishes firmware auto plus Cooler Boost before writing;
- reads the complete original curve;
- writes and verifies the complete request;
- attempts every original byte and verifies rollback on failure;
- exposes no arbitrary EC address.

The new pinned snapshot build also exposes a read-only, versioned control
snapshot. It freshly checks firmware and serializes curve/control/temperature
reads under the driver's existing control mutex. It does not replace write
checks or transaction rollback. User space accepts only the two documented
source hashes, and the new build must provide a valid snapshot; errors never
silently fall back to another ABI. Firmware itself can update telemetry during
the serialized read, so a snapshot is not claimed to freeze the entire EC.

### 2. EC-resident control

After activation, the EC follows Candidate 14 itself. The daemon does not poll
and rewrite fan values. This avoids a user-space controller fighting firmware.

### 3. Root-only serialization

The daemon holds `/run/msi-fanctl.lock` throughout its lifetime. The manager
uses `/run/msi-fan-profile-manager.lock` for command serialization. Both are
root-owned mode `0600` and recreated by `tmpfiles.d` after boot.
The manager releases its lock after enqueuing a Boost request, before waiting
for acknowledgement or cooldown; waiting never blocks later operator actions.

### 4. Read-only runtime monitoring

Once-per-second monitoring checks:

- exact hardware, BIOS, EC, driver version and source identity;
- exact curve and advanced mode;
- CPU package plus EC CPU temperature (hotter value wins);
- EC GPU temperature;
- both physical WMI fan RPM channels;
- shift mode remains `comfort`;
- suspend/resume time discontinuity.

On the pinned legacy build, the per-second EC firmware check is the one the
driver's `fan_curve` read performs in the kernel: it re-checks DMI and the EC
firmware string and fails the read with `EOPNOTSUPP` on a mismatch. The daemon
and keeper read `fan_curve` before any other EC value of a pass instead of
reading `fw_version` separately, as the snapshot build's snapshot already
does. Every write and the manager's status still read `fw_version`.

### 5. Thermal/fan escalation

The daemon requests and physically verifies Cooler Boost for:

- CPU >=103°C immediately;
- CPU >=100°C for two seconds;
- CPU >=98°C for nine seconds;
- GPU >=85°C immediately;
- GPU >=82°C for two seconds;
- GPU >=80°C for nine seconds;
- fan 1 below 1,000 RPM for four seconds while CPU >=80°C;
- fan 2 below 1,000 RPM for four seconds while GPU >=70°C.

If an alarm persists 15 seconds despite physically verified Boost, the daemon
enters a visible sustained-safety-alarm state. It remains active with Cooler
Boost latched on, continues one-second monitoring and physical fan verification,
and reports the condition through both the journal and systemd status. It emits
an additional warning at most once per minute while the condition persists.

The automatic latch is released only after 30 continuously valid, cool seconds.
It is not released merely because one sample looks cool, and an external
Cooler-Boost-off toggle cannot defeat the latch, either while the thermal
condition is active or during its cooldown. This holds when manual Boost was
already on: the alarm still sets the latch.

High temperature alone is not proof that the fan controller is unsafe: once
both physical fans have verified at maximum cooling, stopping the daemon would
only lose monitoring during a persistent high-heat condition. The daemon still
fails and invokes full-cooling recovery for an invalid identity, curve/state
drift, a write/readback error, watchdog failure, or a plausible-but-low physical
fan response. Invalid telemetry instead enters the degraded state below.
The response floor is 3,000 RPM per fan, not proof of maximum physical speed.
Runtime response evidence is checked once per monitoring pass, separately for
each healthy fan channel. A failure requires the 12-second response allowance
plus at least three recent, continuously valid low readings spanning two seconds.
Missing RPM resets that channel's low evidence rather than proving failure.

### 6. Degraded telemetry

One nonnumeric, out-of-range, or otherwise implausible temperature/RPM snapshot
does not immediately put the EC back into factory mode or fail the daemon. The
raw EC CPU value `255°C` is explicitly recognized as an unavailable sentinel.
The exact firmware meaning of `255` remains unconfirmed; it is never accepted as
a plausible temperature. Channels are sampled independently, so other valid
RPM observations remain usable even when one temperature is invalid.
The daemon enters a non-expiring degraded-telemetry state: it keeps Candidate 14
in `advanced` mode, latches and reasserts Cooler Boost, clears cooldown progress,
and continues watchdog heartbeats. The journal logs the transition immediately
and then rate-limits continuing warnings to once per minute.

A complete valid sample is not sufficient to release this state. Both physical
fans must first verify under Cooler Boost; invalid WMI RPM telemetry remains
unverified and keeps maximum cooling asserted. Only then can the existing
30-continuously-valid-and-cool-second release timer begin. Plausible but
persistently low RPM after a Boost request remains a genuine fan-response fault.
An observation gap or a sensor acquisition taking over two seconds resets
cooldown and RPM-response evidence and reasserts Boost before release eligibility.
Time without sufficiently continuous monitoring cannot satisfy the cool timer.
Health records the earliest acquisition time, not the later publication time;
an overdue acquisition does not refresh the last-valid timestamp.
An atomic, root-owned `0644` health snapshot exposes valid-channel data, failures,
sample freshness, latch ownership, response verification, and cooldown progress.
Failure to publish diagnostics is reported but does not stop working cooling;
the manager reports missing/stale health as unsuccessful status.

On the combined ABI, any kernel snapshot I/O error makes the entire control
result unavailable; a temperature-byte read failure cannot be distinguished from
a firmware/curve/control failure. It therefore invokes the full-cooling keeper
rather than using the legacy per-channel degraded path. A successfully read raw
`255` remains a per-channel sensor fault and retains the normal degraded latch.
This conservative distinction is not permission to use an unchecked fallback.

Optional timing diagnostics are disabled by default and do not determine policy.
Enabling them for testing adds bounded counters only, with no extra hardware
reads. Disabling them does not disable safety clocks, the two-second acquisition
budget, one-second monitoring, the watchdog or the 30-second cooldown.

### 7. systemd watchdog and recovery

The service is `Type=notify` with a five-second watchdog and `SIGKILL` watchdog
action. `ExecStopPost` is root-owned and independently:

- verifies exact immutable identity;
- waits for the fan lock;
- requests Cooler Boost and reapplies Candidate 14 in `advanced` mode for a
  failed service;
- verifies final profile state and both physical fans when valid WMI telemetry
  is available;
- performs no model-specific write if identity is unknown.

The primary service uses `Restart=no` so a real fault is not hidden by an
automatic daemon restart loop. Its `OnFailure` unit starts a separate,
watchdog-protected recovery keeper after `ExecStopPost` completes. The keeper
holds the same root-only fan lock, continuously reasserts Candidate 14,
`advanced`, and Cooler Boost, and never infers cooldown or releases Boost from
incomplete telemetry. It yields before a guarded manager start, and its own
bounded restart policy makes a keeper failure visible. A sustained, physically
verified safety alarm and degraded telemetry are active cooling states, not
primary-service faults.
Keeper RPM verification is incremental and never stalls control repair. An
upgrade failure after activation begins likewise leaves runtime recovery and
boot enablement intact instead of invoking factory rollback.

After verified recovery cooling is staged, the helper or keeper records a
root-owned, mode-`0600`, same-boot recovery-Boost marker. The daemon validates
and consumes it only after it has successfully reapplied Candidate 14 during a
guarded handoff. The inherited Boost is then an automatic latch: it remains on
until telemetry is valid and cool continuously for 30 seconds. The keeper itself
never releases Boost, and a marker that is absent, malformed, or unsafe is never
treated as permission to release cooling.

The primary unit has no driver-presence condition; only the operator's runtime
`factory-auto` marker may skip it. A condition skip is not a failure and never
starts the keeper, so a missing, rejected, or unpinned `msi_ec` after a kernel
update instead fails the primary at identity validation, before any EC write,
with a journal reason such as `msi_ec driver not loaded for kernel <release>`.
`ExecStopPost` logs the same reason and writes nothing. The keeper keeps its
`fan_curve` condition, so it is skipped when the driver interface is absent and
refuses to write when an unpinned driver is present. The EC then stays on its
own firmware fan control until the driver is repaired. To catch this before a
reboot, a read-only kernel readiness check, also run by a kernel postinst hook
that can never fail a kernel installation, reports whether the next kernel has
the pinned, MOK-signed DKMS build (see the kernel update section of
`ROLLBACK.md`).

At startup only, the daemon gives udev coldplug up to 10 seconds, polled every
0.5 seconds with watchdog heartbeats, to register the `msi_wmi_platform` and
`coretemp` hwmon devices before its first EC write. It still requires exactly
one of each, fails as before after that bound, and never retries discovery
during runtime monitoring.

Both units have `MemoryMax=64M` with `MemorySwapMax=0`, so the memory limit is
a hard, deterministic leak tripwire rather than a limit reached only after swap
fills. Reaching it kills the controller like any other fault: recovery and the
keeper take over cooling. `OOMScoreAdjust=-900` makes the kernel's global OOM
killer prefer other processes, but deliberately does not exempt the controller
from its own limit. `MemoryMin=32M` requests reclaim protection; cgroup v2
honours it only when the ancestor `system.slice` also has a protection
allocation, which a stock Ubuntu host does not set.

The daemon, recovery helper, keeper, and manager start with
`#!/usr/bin/python3 -I`: the distribution interpreter rather than the first
`python3` in `PATH`, in isolated mode without `PYTHON*` environment variables,
user site packages, or the script directory on the import path. Each program
inserts its fixed helper directory explicitly.

## Cooler Boost overrides

- Boost-on requires daemon acknowledgement and both fans above 3,000 RPM.
- Release requires 30 continuously valid, cool seconds.
- A thermal automatic latch cannot be defeated by an external off toggle.
- Automatic and manual Boost ownership are independent. A thermal alarm,
  invalid telemetry, an observation gap or a recovery handoff sets the
  automatic latch even while manual Boost is on, and a `boost-on` request or
  keyboard toggle never cancels the latch. An external or keyboard off during
  the latch withdraws manual Boost while the latch keeps reasserting Boost.
  When the latch's cooldown completes, Boost goes off only if no manual Boost
  remains; a queued `boost-release` never switches Boost off under the latch.
- Failure recovery and its keeper retain Candidate 14/`advanced` and leave Boost
  on until a guarded daemon handoff; the verified recovery marker then makes it
  an automatic, 30-second-cooldown latch. Physical verification is reported only
  for valid RPM telemetry.

Explicit `factory-auto`, a clean service stop, manager setup errors before a
service start is attempted, and installation or upgrade staging remain
intentional factory `auto/off` transitions. They are not used as a response to
a runtime fault. Each switches Boost off only when it is cool.

## Clean stop while hot

A clean stop is a `systemctl stop`, a shutdown or reboot, or the stop inside
`factory-auto` or an upgrade. The daemon first restores the factory curve and
`auto` mode with Cooler Boost on. It then releases Boost only when the CPU
package and EC CPU are below 85 °C and the EC GPU is below 70 °C. When it is
hotter, the stop deliberately ends in full cooling instead:

1. The daemon exits `1` with `factory restored; Boost release unsafe`, so
   systemd records a failed stop.
2. `ExecStopPost` treats it like a runtime fault. It re-stages and verifies
   Candidate 14/`advanced` with Boost on and records the recovery-Boost marker.
3. While the system is running, `OnFailure` starts the keeper, which holds that
   state, and `status` exits `1`. When the machine is cool, `apply-default`
   hands over to the daemon, which releases Boost after 30 continuously valid,
   cool seconds. Within `factory-auto`, the factory marker skips the keeper. The
   manager then restores the factory curve with Boost on and reports
   `factory staged with Boost on`.
4. During a shutdown, systemd cannot enqueue the keeper and logs that
   `OnFailure=` job as failed. The EC keeps Candidate 14/`advanced` with Boost
   on until power-off. Boost does not survive the reboot, the `/run` marker is
   gone, and the next boot applies Candidate 14 as usual.

This is the intended, safe direction: a stop never leaves a hot machine on the
quieter factory curve with Boost released. The failed unit after a hot stop is
not a fault to repair. Let the machine cool, then run `apply-default`.

## Suspend/resume

The daemon compares `CLOCK_BOOTTIME` and monotonic time. A suspend gap triggers a
forced reprogramming of Candidate 14 under the same auto/Boost envelope. This
path is implemented; final attended suspend validation is still marked pending
in `VALIDATION.md` until completed. The validation host keeps its sleep targets
masked until then, and Secure Boot lockdown disables hibernation, so this path
does not run there.

## What this cannot guarantee

Fans have thermal and mechanical inertia. The Core Ultra 9 285HX can produce
heat faster than fan RPM can change. Even fans pre-stabilized at maximum did
not hold the most demanding CPU load below the mid-90s. A hard lower limit
requires CPU power control, which is outside this project's scope.
