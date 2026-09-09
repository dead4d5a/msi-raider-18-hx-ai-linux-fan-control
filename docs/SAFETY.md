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
fails. systemd kills it and runs recovery.

### 6. systemd watchdog and recovery

The service is `Type=notify` with a five-second watchdog and `SIGKILL` watchdog
action. `ExecStopPost` is root-owned and independently:

- verifies exact immutable identity;
- waits for the fan lock;
- restores the factory curve and auto mode;
- leaves Cooler Boost on for any failed service;
- performs no model-specific write if identity is unknown.

The service uses `Restart=no` so a fault is not hidden by an automatic loop.

## Cooler Boost overrides

- Boost-on requires daemon acknowledgement and both fans above 3,000 RPM.
- Release requires 30 continuously cool seconds.
- A thermal automatic latch cannot be defeated by an external off toggle.
- Failure recovery leaves Boost on until attended inspection.

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
