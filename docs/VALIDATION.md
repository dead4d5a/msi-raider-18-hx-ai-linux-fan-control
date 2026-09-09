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
- Forced `SIGKILL` recovery to factory auto + Boost on.
- Runtime factory override and direct-start refusal.
- Daemon detection of a marker appearing after service start.
- Manager acknowledgement for Boost-on and 30-second cool release.
- Reboot persistence with root-only lock recreation and Candidate 14 auto-apply.

## Pending

- Final attended suspend/resume test confirming the daemon logs a resume event,
  forcibly reprograms Candidate 14, and preserves watchdog service health.

The implementation already uses a suspend-time discontinuity between
`CLOCK_BOOTTIME` and monotonic time and forces a full safe reapply. Until the
attended result is recorded, suspend/resume should be treated as implemented but
not finally validated.

## Not claimed

- Other BIOS versions
- Other EC firmware versions
- RTX 5090 A2XWJG behavior
- Other MS-1824 marketing variants
- Other Ubuntu/kernel releases
- Hibernation behavior
- Hard CPU temperature enforcement below 90°C
- Generic fan-curve editing

Contributions for another exact configuration should include DMI/BIOS/EC
identity, curve backup, physical RPM verification, failure recovery, and
repeatable measurements. Do not submit “works on MSI” claims without exact data.
