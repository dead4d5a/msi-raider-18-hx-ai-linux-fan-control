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
drift, a write/readback error, a watchdog timeout, or a plausible-but-low
physical fan response. A failed watchdog notification alone is reported and
does not end it (section 7). Invalid telemetry instead enters the degraded
state below.
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

The service is `Type=notify` with a 30-second watchdog and `SIGKILL` watchdog
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

Both units use `WatchdogSec=30s`. The watchdog does not enforce the one-second
monitoring or the two-second freshness rule; the daemon enforces both itself.
However late it resumes, the pass that sees an observation gap or a sensor
acquisition of more than two seconds latches Cooler Boost and resets the
cooldown (section 6). The watchdog only bounds how long a daemon that does not
resume stays in place before `ExecStopPost` and the keeper take over. Compared
with the former 5 s, a daemon that alone hangs is replaced up to 25 s later,
and a daemon stall of 7 to 30 s now passes without Boost until the daemon
resumes; with 5 s, the kill and the recovery helper had Boost on about 7 s into
such a stall. For that time the EC runs its curve without alarm escalation: in
the field, with Boost off and the CPU at 88 °C or above, the median fan speeds
were about 3,100 RPM (fan 1) and 3,700 RPM (fan 2), roughly half and 60 % of
full speed. A stall longer than 30 s still ends in a kill and a keeper hold.
The keeper has the same value because the same stall delays it: on 2026-11-13
its former 5 s watchdog killed it three times within four minutes of one
host-wide out-of-memory stall, and its start limit of three starts in 15
minutes then left the EC at full cooling with no process supervising it.

A failed runtime watchdog notification is reported and does not stop cooling.
Each monitoring pass of the daemon and each repair pass of the keeper ends
with one `WATCHDOG=1` notification that also carries the status line. If that
send fails, for example because the service manager does not drain its
notification socket during a host-wide out-of-memory stall, the program logs
`WARNING runtime watchdog notification failed (consecutive failures: N)` with
the error, at the first failure and then at most once a minute, and the loop
goes on. Once a notification succeeds again after such a warning, one line
says so. The watchdog, not a single failed notification, ends a controller
that stays silent: when no notification reaches systemd for 30 s, systemd
kills the unit and records the result `watchdog`. As after any other
watchdog timeout, `ExecStopPost` and the keeper then take over from the
daemon, and a killed keeper is restarted within its start limit. Before 0.2.1,
one failed notification ended the daemon with exit status 1, recorded as the
result `exit-code` like a fault the daemon detects itself, and the keeper took
over at full cooling at once. It ended the keeper too, and three such exits
within 15 minutes would have used up the keeper's start limit. This is the one
deliberate exception to preferring a noisy machine: a failed notification no
longer brings full cooling at once. Monitoring is not reduced: until the
watchdog acts, the daemon keeps its one-second checks and latches Boost on
every alarm, gap or invalid sample as before. Startup stays strict: a failed
`READY=1`, or a failed heartbeat before it, still ends startup with exit
status 1. Two other places stay strict too. A failed heartbeat while the
daemon verifies Boost during a Candidate 14 reapply still ends the daemon in
full-cooling recovery, and a failed heartbeat during the clean stop still
refuses the Boost release and ends the stop in full cooling.

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
a runtime fault. Each switches Boost off only when coolness is confirmed as
described next. An upgrade whose installed manager cannot confirm that instead
replaces files at full cooling, as described under
[Upgrade handover at full cooling](#upgrade-handover-at-full-cooling).

## Confirming cool before a release

Every decision that switches Cooler Boost off, or leaves the factory envelope
at `auto/off`, outside the runtime cooldown uses one shared, read-only helper
in `msi_fan_control.py`: the daemon's clean stop, the `ExecStopPost` helper
after a successful stop, the manager's `factory-auto`, and
`uninstall-profile.sh`. One reading never decides. Under bursty load the
coretemp package reading on this laptop jumps 20 °C or more for milliseconds,
and the EC sometimes returns junk such as 255 °C or an empty value.

- A sample reads the coretemp package (`temp1_input`, m°C) and the EC CPU and
  GPU realtime temperatures (°C), each through the caller's own reader.
- A sample is valid only if each value is one to six ASCII digits within 1 to
  110,000 m°C (package), 1 to 110 °C (EC CPU) or 0 to 110 °C (EC GPU). The
  EC's 255 sentinel, an empty, signed or non-numeric value, and a CPU at 0 °C
  are invalid; an unreadable attribute is unavailable.
- A valid sample is cool when the package and EC CPU are below 85 °C and the EC
  GPU is below 70 °C (0, a powered-off dGPU, counts as cool). These are the
  release limits used before; they did not change.
- Release requires six consecutive valid, cool samples started at least 0.5 s
  apart, so at least 2.5 s of agreement. An invalid, unavailable or hot sample
  restarts the count. The check gives up as soon as six samples can no longer
  fit in 10 s, which is after about 7.5 s when every sample is hot, and the
  wait never exceeds 10 s plus one sample.

A single spike or junk value therefore can neither release Boost nor, by
itself, refuse a release. A cool machine releases after about 2.5 s; a spike
right after the stop delays it by about 0.5 s. When the window ends without
agreement, each caller does exactly what one hot reading made it do before:
Boost stays on, the daemon exits `1`, `factory-auto` reports
`factory staged with Boost on`, and the uninstall removes nothing. The log
names the most recent unsafe sample. When every sample was valid and cool but
they came too slowly to fill the window, for example from a slow EC, the log
says `samples too slow` instead, and `factory-auto` reports
`cool confirmation did not complete` rather than heat or unusable telemetry.

Entering cooling never waits for this confirmation. In the daemon, the manager
and `ExecStopPost`, an unsafe sample reasserts Cooler Boost at once if anything
switched it off, including a keyboard toggle during the wait. `ExecStopPost`
runs after the daemon has already released Boost, so there the first unsafe
sample switches Boost back on before the verdict, and a later confirmed window
releases it again. The uninstall script keeps the Boost it set just before the
check and requests it again when it refuses.

The uninstall script validates the exact platform and driver before its first
EC write, and the confirmation can take 10 s, during which a driver reload or
`ec_sys` could appear. The confirmation's readings would then also have come
from an unverified driver. So, like every other EC writer, which checks identity
inside each write, the script repeats the full identity check after the
confirmation and before the next EC write: before it switches Boost off, and
before it requests Boost again on a refusal. If identity no longer matches, it
writes nothing more, not even Boost on, because no model-specific write goes to
an unverified driver. Boost stays as it was set and verified before the
confirmation unless something else switched it off meanwhile, the script exits
`1` with
`The exact platform or driver identity changed during the cool confirmation; ...`,
and nothing is removed.

The daemon's clean stop runs under `TimeoutStopSec=30s`, which systemd arms
when it sends `SIGTERM` and follows with `SIGKILL`. A stop during normal
monitoring needs at most one monitoring pass, the factory restore, 10 s of
confirmation and the release. A stop that arrives while the daemon is still
starting is handled only after startup, which can spend up to 10 s waiting for
the hwmon devices and up to 12 s verifying Boost, so those waits and a 10 s
confirmation could reach the 30 s limit. The daemon therefore records when the
first stop signal arrived. It gives the confirmation only what remains of the
30 s, less a 5 s margin for the release writes and the exit, and never more
than 10 s. If that remainder cannot hold six samples (2.5 s), it refuses the
release without sampling and logs
`WARNING Cooler Boost release refused: N s of the 30 s stop timeout remain ...`.
The daemon then exits `1` into full-cooling recovery, as after a hot stop,
instead of being killed. A stop that the `factory-auto` marker starts before
any signal gets the full 10 s, because the stop timer is not running yet. The
daemon sends a watchdog heartbeat with every sample. That matters when the
`factory-auto` marker starts the stop before the stop request arrives, because
systemd still enforces `WatchdogSec=30s` then. `ExecStopPost` runs under its own
`TimeoutStopSec=30s`; on its success path it spends at most 10 s confirming.
The manager has no watchdog, and its 50 s `systemctl stop` covers the daemon's
and the helper's waits. A cool clean stop takes about 2.5 s longer than before,
and a cool `factory-auto`, which confirms three times, about 7.5 s longer.

The runtime release of an automatic latch or a queued manual release is not
changed: it already requires 30 continuously valid, cool seconds, and an
invalid sample enters the degraded state and resets the cooldown. Its own
plausibility ranges still accept 0 for the package and the EC CPU, but a
single such sample cannot complete 30 continuous seconds while the other
samples are hot.

## Clean stop while hot

A clean stop is a `systemctl stop`, a shutdown or reboot, or the stop inside
`factory-auto` or an upgrade. The daemon first restores the factory curve and
`auto` mode with Cooler Boost on. It then releases Boost only once six
consecutive valid samples, 0.5 s apart within 10 s, show the CPU package and
EC CPU below 85 °C and the EC GPU below 70 °C (see the previous section). When
it stays hotter, the telemetry stays invalid, or a stop that arrived during
startup leaves too little of the stop timeout to confirm, the stop
deliberately ends in full cooling instead:

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

## Upgrade handover at full cooling

An upgrade hands over through the installed release's `factory-auto`, and
releases before this one decide a Boost release from one reading, which a
spike can refuse right after the upgrade's cool-down gate. Their refusal
leaves the state described above: the factory curve in `auto` mode with Boost
on, both units stopped, the `factory-auto` marker, and the recovery-Boost
marker. That is maximum cooling with no controller running, the safest state in
which to replace files. `upgrade-profile.sh` therefore continues only when, after
the failed `factory-auto`, all of these verify by reading alone:

- `preflight.sh` passes again: exact product, board, BIOS, EC firmware, pinned
  driver build, `comfort` shift mode, Secure Boot and no `ec_sys`;
- the factory curve, `fan_mode` `auto` and `cooler_boost` `on`;
- the primary service and the keeper are `inactive/dead`;
- the `factory-auto` marker is a root-owned, mode-`0600` regular file;
- the recovery-Boost marker is a root-owned, mode-`0600` regular file with one
  link and exactly the payload the daemon accepts.

It then disables the service for the replacement window, keeps the
recovery-Boost marker, replaces and records the artifacts, and activates only
through the new manager. The new daemon validates and consumes the marker after
it has reapplied Candidate 14, so the inherited Boost becomes its automatic
latch and is released only after 30 continuously valid, cool seconds. Without a
valid marker, the daemon would keep the inherited Boost as manual Boost, so a
missing or invalid marker stops the upgrade, as does any other failed check:
nothing is replaced and the manager's exit status is kept. The upgrade script
never writes the EC and never releases Boost itself. The service and the
markers are checked again after the disable, before any file is replaced: Boost
must still be on, the keeper inactive and the recovery-Boost marker valid. If a
check fails after the upgrade has continued, its exit message reads Cooler
Boost and the marker again and reports what it finds, such as
`cooler_boost is off` or a marker that is `missing`, since the failed check may
be exactly that.

## Host memory stalls

When the host runs out of memory, the kernel can hold any process for seconds
while it reclaims memory, in an allocation or in a page fault. Such host-wide
out-of-memory stalls recur on the validation host. The daemon and the keeper
are held like every other process and run no code until the kernel lets them
continue.

A late daemon latches Cooler Boost in its first pass after the stall. When an
acquisition starts more than 2 s after the previous one, or takes longer than
2 s, that pass reasserts Boost as an automatic latch and resets the cooldown
and the fan-response evidence (section 6); time without continuous monitoring
never counts toward the 30-second cool timer. Each stall is logged once, as a
`WARNING` with its length, where the time went, and the memory pressure of the
unit and of the host, read after the Boost write. The keeper reasserts full
cooling in every pass and logs `WARNING recovery keeper stall ...` when a
pass, with any overrun of the sleep before it, takes more than 2 s.

The watchdog is for a process that does not resume. Both units use
`WatchdogSec=30s` with `SIGKILL` (section 7). When the daemon has sent no
notification for 30 s, systemd kills it and records the result `watchdog`.
`ExecStopPost` then stages and verifies Candidate 14 in `advanced` mode with
Cooler Boost on and records the recovery-Boost marker, and the keeper holds
that state; a killed keeper is restarted 30 s later, within its start limit.
This release does not hand back by itself: full cooling stays until
`apply-default`, which needs no reboot (`OPERATIONS.md`, "After a watchdog
kill").

The cost of 30 s is that a daemon stall of 7 to 30 s passes without Boost
until the daemon resumes. In that time no alarm can raise Cooler Boost. In the
field, with Boost off and the CPU at 88 °C or above, the median fan speeds were
about 3,100 RPM (fan 1) and 3,700 RPM (fan 2), roughly half and 60 % of full
speed. A shorter watchdog gains little in a host-wide stall, because the
recovery helper and the keeper need memory too and wait in the same way.

What cannot be guaranteed:

- A stall inside a kernel allocation cannot be bounded from user space. No
  timer of the daemon, the recovery helper or the keeper runs while the kernel
  holds it, and even the watchdog's `SIGKILL` takes effect only when the kernel
  lets the process go: on 2026-11-13 the kernel needed 5 to 14 s to finish off
  each killed keeper.
- `MemoryMin=32M` is not effective when the parent cgroup reserves nothing, as
  `system.slice` does on a stock Ubuntu host (section 7). While a unit is
  active, `msi-fan-profile status` reports this as
  `controller_memory.memory_min_effective`, which reads `false` on the
  validation host.
- The keeper's start limit still ends the guard. When the keeper fails again
  after three starts within 15 minutes, systemd refuses to restart it
  (`Start request repeated too quickly`), and `keeper_result` reads
  `start-limit-hit`. The EC keeps what the keeper last asserted, Candidate 14
  in `advanced` mode with Cooler Boost on, but no process reasserts it or
  watches the temperatures and fans; nothing switches Boost back on if a key
  press turns it off. This was observed on 2026-11-13: the keeper's former 5 s
  watchdog killed it three times within four minutes of one stall, and the
  fans stayed at full cooling without a supervising process for 9 h 35 min,
  until `apply-default` restored normal control. The 30 s watchdog makes this
  less likely; it does not rule it out.

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
