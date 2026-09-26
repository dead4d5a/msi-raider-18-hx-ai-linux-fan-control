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

### 2. EC-resident control

After activation, the EC follows Candidate 14 itself. The daemon does not poll
and rewrite fan values. This avoids a user-space controller fighting firmware.

### 3. Root-only serialization

The daemon holds `/run/msi-fanctl.lock` throughout its lifetime. The manager
uses `/run/msi-fan-profile-manager.lock` for command serialization. Both are
root-owned mode `0600` and recreated by `tmpfiles.d` after boot.

### 4. Read-only runtime monitoring

Once-per-second monitoring checks:

- exact hardware, BIOS, EC, driver version and source identity;
- exact curve and advanced mode;
- CPU package plus EC CPU temperature (hotter value wins);
- EC GPU temperature;
- both physical WMI fan RPM channels;
- shift mode remains `comfort`;
- suspend/resume time discontinuity.

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
Cooler-Boost-off toggle cannot defeat the latch while the thermal condition is
active.

High temperature alone is not proof that the fan controller is unsafe: once
both physical fans have verified at maximum cooling, stopping the daemon would
only lose monitoring during a persistent high-heat condition. The daemon still
fails and invokes full-cooling recovery for an invalid identity, curve/state
drift, a write/readback error, watchdog failure, or a plausible-but-low physical
fan response. Invalid telemetry instead enters the degraded state below.

### 6. Degraded telemetry

One nonnumeric, out-of-range, or otherwise implausible temperature/RPM snapshot
does not immediately put the EC back into factory mode or fail the daemon. The
raw EC CPU value `255°C` is explicitly recognized as an unavailable sentinel.
The daemon enters a non-expiring degraded-telemetry state: it keeps Candidate 14
in `advanced` mode, latches and reasserts Cooler Boost, clears cooldown progress,
and continues watchdog heartbeats. The journal logs the transition immediately
and then rate-limits continuing warnings to once per minute.

A complete valid sample is not sufficient to release this state. Both physical
fans must first verify under Cooler Boost; invalid WMI RPM telemetry remains
unverified and keeps maximum cooling asserted. Only then can the existing
30-continuously-valid-and-cool-second release timer begin. Plausible but
persistently low RPM after a Boost request remains a genuine fan-response fault.

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

## Cooler Boost overrides

- Boost-on requires daemon acknowledgement and both fans above 3,000 RPM.
- Release requires 30 continuously valid, cool seconds.
- A thermal automatic latch cannot be defeated by an external off toggle.
- Failure recovery and its keeper retain Candidate 14/`advanced` and leave Boost
  on until attended inspection; physical verification is reported only for valid
  RPM telemetry.

Explicit `factory-auto`, a clean service stop, manager setup errors before a
service start is attempted, and installation or upgrade staging remain
intentional factory `auto/off` transitions. They are not used as a response to
a runtime fault.

## Suspend/resume

The daemon compares `CLOCK_BOOTTIME` and monotonic time. A suspend gap triggers a
forced reprogramming of Candidate 14 under the same auto/Boost envelope. This
path is implemented; final attended suspend validation is still marked pending
in `VALIDATION.md` until completed.

## What this cannot guarantee

Fans have thermal and mechanical inertia. The Core Ultra 9 285HX can produce
heat faster than fan RPM can change. Even fans pre-stabilized at maximum did
not hold the most demanding CPU load below the mid-90s. A hard lower limit
requires CPU power control, which is outside this project's scope.
