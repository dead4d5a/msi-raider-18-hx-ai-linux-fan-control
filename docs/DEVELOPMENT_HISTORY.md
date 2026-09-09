# Development history: how Candidate 14 was derived

This project began because Linux could read physical fan RPM through
`msi_wmi_platform`, but exposed no writable PWM channels. Generic `fancontrol`
and CoolerControl therefore could not control the laptop fans. The in-tree
`msi_ec` on the tested Ubuntu kernel rejected EC `1824EMS1.108`; the current
out-of-tree project supported the firmware and exposed fan mode and Cooler
Boost, but not curve points.

## 1. Establishing a safe control path

The upstream driver was pinned, built, signed with a locally enrolled MOK, and
loaded with Secure Boot retained. Cooler Boost and firmware auto were validated
first while physical fan 1/fan 2 RPM and temperatures were monitored.

The stored curve was captured read-only:

```text
CPU 58/64/70/76/82/88 -> 0/25/35/44/58/70/75
GPU 52/58/64/70/76/82 -> 0/25/35/44/58/70/75
```

A narrow driver extension was then built so curve updates could be whole,
validated, read back, and rolled back without enabling generic raw EC writes.

## 2. What failed and why

### Stored advanced curve

Under CPU-only load, fan 1 accelerated while fan 2 fell to zero because the GPU
sensor stayed below its independent threshold. CPU package temperature reached
99°C. This demonstrated that the shared cooling system needed earlier fan-2
participation.

### Early candidates

Several candidates lowered thresholds or increased upper speed values. The key
lessons were:

- fan speed has a slow physical slew;
- a steep target cannot erase an instantaneous CPU turbo spike;
- low GPU thresholds can keep fan 2 unnecessarily loud at idle;
- reducing CPU-side upper values can worsen CPU temperature despite more fan-2
  activity;
- run-to-run CPU power varied until workers were pinned to fixed P-cores;
- measurements taken immediately after Cooler Boost were biased by residual
  maximum RPM.

The protocol was changed to fixed CPU affinity and a monitored 75-second
post-Boost settling period.

### Candidate 12

Candidate 12 was thermally strong but ran fan 2 around 2,200–3,000 RPM at idle
because its early GPU thresholds were too low.

### Candidate 13

Candidate 13 fixed idle behavior and passed 60-second individual/combined tests,
but failed a 180-second combined soak after about 124 seconds. Fan 1 was already
at physical maximum while fan 2 still had headroom.

## 3. Candidate 14

Candidate 14 preserves Candidate 13 below 55°C on the shared/GPU side and uses
stronger fan-2 upper bins only under genuinely heavy heat:

```text
GPU thresholds: 38 40 42 44 55 70
GPU speeds:     20 30 40 60 70 100 120
```

It completed the 180-second combined soak with low settled idle RPM and became
the selected default.

## 4. Safety engineering

Calibration tooling went through multiple rejected supervisor designs before
GPU load was accepted. The final calibration harness used:

- a root controller holding the fan lock continuously;
- an unprivileged NVML monitor and CUDA workload;
- one process group and systemd cgroup;
- a systemd watchdog and runtime ceiling;
- hotter-of-sensor thermal decisions;
- physical fan-stall detection;
- exact profile readback on every sample;
- root `ExecStopPost` factory recovery.

Forced-kill and marker-race tests proved that a failed service restored factory
auto with Cooler Boost on and did not leave a workload alive.

The production daemon retains the same conservative philosophy but does not
install or run synthetic calibration workloads.
