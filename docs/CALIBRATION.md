# Calibration method and measured results

## Method

The curve was derived empirically rather than copied from another MSI model.

1. Inventory firmware-auto RPM and temperature behavior.
2. Capture the stored factory curve three times with the kernel `ec_sys` module
   in default read-only mode (`write_support=N`, debugfs EC file mode `0400`).
3. Restore `msi_ec`; never run it concurrently with `ec_sys`.
4. Add an exact-platform transactional curve interface to signed `msi_ec`.
5. Test malformed and unsafe curve rejection plus factory no-op transaction.
6. Calibrate candidate curves with automatic factory restoration.
7. Pin CPU loads to known P-cores (`0–5`) for repeatability.
8. Build a bounded CUDA SGEMM load tied to the exact GPU UUID.
9. Run CUDA under a systemd cgroup, watchdog, thermal monitor, and root recovery.
10. Add a monitored 75-second post-Boost settling period before comparisons.
11. Run idle, CPU-only, GPU-only, combined, forced-failure, reboot, and soak tests.

The production repository does not install the calibration workload. It is
published as methodology and summarized data, not as a recommendation to stress
an unattended laptop.

## Captured factory values

```text
CPU thresholds: 58 64 70 76 82 88
CPU speeds:      0 25 35 44 58 70 75
GPU thresholds: 52 58 64 70 76 82
GPU speeds:      0 25 35 44 58 70 75
```

## Key observations

- Firmware auto used both fans for CPU-only heat.
- The stored advanced curve could stop fan 2 during CPU-only load.
- EC fan speed changes have a long physical slew; an aggressive upper target
  cannot instantly prevent turbo spikes.
- A low nonzero first point gives useful idle airflow without full speed.
- Early GPU/shared fan thresholds help CPU thermals because cooling is shared.
- Too-early GPU thresholds cause unnecessary 3,000 RPM fan-2 idle behavior.
- Candidate 14 keeps Candidate 13's low idle behavior and strengthens only the
  >55°C GPU/shared bins.

## Unbiased results

Values below use the last 20 seconds unless noted. Ambient and unrelated host
activity varied; comparisons were run under the same profile harness and fixed
workload recipes.

| Workload | Policy | GPU | CPU package | Fan 1 | Fan 2 | Result |
|---|---|---:|---:|---:|---:|---|
| GPU 75%, 60 s | firmware auto | 70.2°C | 90.3°C | 3,050 | 3,042 | pass |
| GPU 75%, 60 s | Candidate 14 | 64.4°C | 79.5°C | 2,186 | 4,339 | pass |
| CPU six P-cores, 90 s | firmware auto | 47.6°C | 90.2°C | 3,679 | 3,679 | pass |
| CPU six P-cores, 90 s | Candidate 13/14 CPU-equivalent | 45.6°C | 87.6°C | 5,525 | 3,743 | pass |
| Combined CPU6 + GPU25, 60 s | firmware auto | 59.6°C | 98.3°C | 3,618 | 3,607 | stopped at sustained cutoff |
| Combined CPU6 + GPU25, 60 s | Candidate 13 | 56.3°C | 93.7°C | 4,703 | 3,718 | pass |
| Combined CPU6 + GPU25, 180 s | Candidate 14 | 55.5°C | 94.4°C | 5,588 | 4,313 | pass |

Candidate 14 final-soak maximum package temperature was 95°C. It completed all
180 seconds and restored factory `auto/off` afterward.

## Physical cooling ceiling

At maximum Cooler Boost before load, an eight-P-core test settled around 95°C
and briefly reached 96°C. A prior instantaneous sample reached 99°C despite
both fans already at physical maximum. This demonstrates that fan-only control
cannot impose an 80–85°C hard ceiling under extreme package power.

## Evidence integrity

A sanitized summary is committed in `data/results.json`. The source machine's
full journals were hashed, not published, because journals can contain hostnames,
usernames, process data, and other nonessential identifiers.

See `data/EVIDENCE_SHA256SUMS` for hashes of the selected raw local evidence.
The raw files are not required to operate the profile.
