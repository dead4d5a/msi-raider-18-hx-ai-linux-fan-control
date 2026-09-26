# Daily operations

## Status

```bash
msi-fan-profile status
```

This command is read-only and does not require root. It returns success only for
a complete, stable default state or a complete runtime factory-override state.

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
physical fans exceed 3,000 RPM.

Release after a monitored cool interval:

```bash
sudo msi-fan-profile boost-release
```

The daemon requires 30 continuously cool seconds before release. The command can
wait indefinitely while the machine remains hot; interrupting the waiting client
does not bypass the daemon's thermal gate.

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
rate-limits continuing warnings.

One complete valid sample does not by itself resume release logic. Both physical
fans must verify under Boost first; invalid RPM telemetry remains unverified and
keeps maximum cooling latched. After that verification, the usual 30
continuously valid, cool seconds are required before automatic release.

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
