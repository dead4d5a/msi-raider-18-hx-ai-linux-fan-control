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
mode. If the transition fails, it disables the service and prefers factory auto
with Cooler Boost on.

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

On a service failure, verify:

```text
fan_mode=auto
factory curve restored
cooler_boost=on
```

The service intentionally does not restart after failure. Inspect the cause,
wait for cooldown, and use `apply-default` only after resolving it.

## Direct sysfs writes

Do not write the curve or fan mode manually during normal operation. Use the
manager. The driver interface exists for transactional, exact-profile operations
and recovery—not arbitrary experimentation.
