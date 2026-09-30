# Daily operations

## Status

```bash
msi-fan-profile status
```

This command is read-only and does not require root. It returns success only for
a complete, stable default state with fresh normal/cooling health, or a complete
runtime factory-override state. Exit status `1` indicates a configuration problem
or degraded, stale, missing, or invalid controller health; it does **not** request
a cooling change.

The nested `health` object reports sample and last-valid-sample age, per-channel
telemetry/errors, automatic/manual Boost ownership, pending release, cooldown
progress, and whether current RPM verifies fan response. The daemon atomically
publishes `/run/msi-fan-profile.health.json` as `root:root:0644`; the manager checks
ownership, mode, inode, schema, PID, and a five-second freshness limit. Dynamic
telemetry is not included in the stable configuration comparison.

## Optional timing diagnostics (testing only)

Timing diagnostics are **off by default** in both services. Only
`MSI_FAN_TIMING_METRICS=1` enables them; unset, `0`, or invalid values leave them
off (invalid values also warn). The setting is read at process startup. Disabled
diagnostics make no diagnostic-clock calls, accumulate no performance counters,
and add no `timing_metrics` field or timing log records. Required safety clocks,
sensor monitoring, watchdogs, cooldown guards, and ordinary health remain active.

For an attended test, use a temporary, named systemd drop-in. Do this only in a
cool maintenance window. The `&&` chain stops if the guarded factory transition
fails; do not bypass that failure or force a hot controller restart.

```bash
sudo msi-fan-profile factory-auto && \
sudo systemctl edit --runtime --drop-in=90-timing-metrics.conf \
  msi-fan-profile.service msi-fan-profile-keeper.service && \
sudo msi-fan-profile apply-default
```

Put this in the named drop-in for **each** service:

```ini
[Service]
Environment=MSI_FAN_TIMING_METRICS=1
```

The editor reloads systemd; the guarded start creates a new process with metrics
enabled. Primary diagnostics appear under `health.timing_metrics` in
`msi-fan-profile status`: bounded last/maximum loop and acquisition durations,
observation gaps, completed-loop/read counts, overdue reads and deadline misses.
Loop work excludes cadence sleep. Each health publication includes the previous
completed loop and the current acquisition; no history grows in memory. Keeper
diagnostics use `TIMING keeper` JSON journal records, at most once per minute,
with the keeper PID, loop/RPM timings and gap/deadline counters.

To turn diagnostics **off**, repeat the same guarded sequence and change only
this named drop-in's value to `0`. Do not use `systemctl revert`, which would
also remove unrelated overrides. Runtime drop-ins disappear on reboot, so the
packaged default returns to `0`. Confirm a newly started primary has no
`health.timing_metrics` field. Enabling/disabling never changes the fan curve,
thermal thresholds, firmware gates, watchdog policy, or cooldown requirements.

## Candidate 14 default

Enable and start the selected default:

```bash
sudo msi-fan-profile apply-default
```

This persistently enables the service and verifies the exact curve and advanced
mode. A manager setup failure before it attempts service start disables the
service and prefers factory auto with Cooler Boost on. Once service start has
been attempted, the manager does not overwrite a failed service's runtime
recovery: it stops any failure keeper, then returns Candidate 14 to the managed
daemon. A runtime fault otherwise retains Candidate 14 in `advanced` mode with
Cooler Boost on.

When `apply-default` takes over from a failure keeper, it keeps the recovery
Boost asserted through the guarded handoff. The daemon consumes a root-only
same-boot recovery marker only after it has reapplied Candidate 14, then treats
that inherited Boost as an automatic latch. It releases only after 30
continuously valid, cool seconds; a keyboard or `boost-on` request made after
the handoff remains a separate manual override.

## Temporary maximum cooling

```bash
sudo msi-fan-profile boost-on
```

The command returns only after the daemon acknowledges the request and both
physical fans reach at least 3,000 RPM. This verifies fan response to a maximum
cooling request, not that the fans have attained their calibrated maximum RPM.

Release after a monitored cool interval:

```bash
sudo msi-fan-profile boost-release
```

The daemon requires 30 continuously cool seconds before release. The command can
wait indefinitely while the machine remains hot; interrupting the waiting client
does not bypass the daemon's thermal gate.

The manager lock covers only enqueue, not the cooldown wait. A separate
`boost-on` remains usable and cancels an older release; the waiting release
client reports cancellation after the newer request is acknowledged. Requests
are processed even during degraded temperatures, but Boost-on acknowledgement
still requires valid fan response and release still requires valid temperatures.

The chassis keyboard Cooler Boost shortcut remains usable. The daemon observes
it as a manual override. Use `boost-release` for a monitored release.

## Factory automatic mode until reboot

```bash
sudo msi-fan-profile factory-auto
```

This:

1. Creates a root-only runtime override marker.
2. Stops the profile daemon.
3. Restores the captured factory curve.
4. Selects firmware auto mode.
5. Verifies Cooler Boost is off only when temperatures permit.

The profile remains enabled, but the `/run` marker blocks starts for this boot.
Because `/run` is temporary, Candidate 14 returns automatically after reboot.
Run `apply-default` to return without rebooting.

## Logs

```bash
sudo journalctl -u msi-fan-profile.service -b --no-pager
```

On a runtime service failure, `ExecStopPost` first attempts the following state:

```text
fan_mode=advanced
Candidate 14 curve present
cooler_boost=on
both fans >= 3,000 RPM when valid WMI telemetry is available
```

The failed primary remains visible (`Restart=no`), but its `OnFailure` keeper
then holds the shared fan lock and continuously reasserts the same Candidate
14/`advanced`/Boost state. If the WMI RPM channels are invalid, it reports Boost
requested without claiming physical verification. It does not deliberately
restore the factory curve or release Boost for a runtime fault. On the next
guarded `apply-default`, its root-only recovery marker makes the inherited Boost
an automatic latch, not a manual override. Inspect both units before taking over:

```bash
sudo systemctl status msi-fan-profile.service msi-fan-profile-keeper.service
```

## Degraded telemetry

Malformed, nonnumeric, or out-of-range temperature/RPM telemetry—including the
EC CPU `255°C` sentinel—enters a non-expiring degraded state rather than a
factory fallback or daemon failure. Candidate 14 stays in `advanced` mode,
Cooler Boost is reasserted if switched off, and systemd status continues to show
the elapsed degraded interval. The journal logs the transition immediately and
rate-limits continuing warnings. Channels are validated independently: an invalid
temperature cannot hide otherwise valid fan RPM. A known nonresponding fan can
still cause a physical-response fault and full-cooling recovery, while missing
RPM is reported as unverified rather than mistaken for a stall.

One complete valid sample does not by itself resume release logic. Both physical
fans must verify under Boost first; invalid RPM telemetry remains unverified and
keeps maximum cooling latched. After that verification, the usual 30
continuously valid, cool seconds are required before automatic release.
An observation gap or an acquisition taking over two seconds also resets cooldown
and physical-response evidence before any release. Health observation timestamps
refer to acquisition start, not publication time; an overdue read cannot look
newly valid merely because it has just completed. Runtime fan
verification is incremental, so verification retries never pause the control
repair/monitoring loop. Startup/reapply transactions retain bounded verification.

## Sustained high heat

After 15 seconds of an alarm with Cooler Boost physically verified, the service
remains active rather than failing. `systemctl status msi-fan-profile.service`
will report a sustained safety alarm, and the journal records the triggering
temperature/fan condition. An automatically latched Cooler Boost releases only
after 30 continuously valid, cool seconds; a manual Boost latch continues to
use its separately requested, monitored release path.

Stop or reduce the workload if this occurs. This fan-only controller cannot
guarantee a CPU temperature below the mid-90s under every sustained workload;
CPU power policy is deliberately outside its scope.

The same sustained state can result from a verified fan-stall alarm. Treat that
as a hardware fault and inspect the fan path before relying on the system.

## Direct sysfs writes

Do not write the curve or fan mode manually during normal operation. Use the
manager. The driver interface exists for transactional, exact-profile operations
and recovery—not arbitrary experimentation.
