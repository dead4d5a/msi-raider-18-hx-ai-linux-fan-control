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

`controller_memory` names the active unit (the controller or its keeper) and
reports its cgroup's current, peak, and swap memory in bytes from the same
batched `systemctl show` query. It is `null` when neither unit is active, and a
value is `null` when systemd cannot report it. It is diagnostic only and never
enters the stable comparison or the exit status. Both units have a hard
`MemoryMax=64M` without swap, so a peak approaching 64 MiB foreshadows a
memory-limit kill and keeper takeover.

When the pinned driver is not loaded, status exits `1` with
`ERROR: msi_ec driver not loaded for kernel <release>`. See
[Controller failed: driver not loaded](#controller-failed-driver-not-loaded).

## Kernel updates

Before rebooting into a newly installed kernel, run the read-only readiness
check; it exits `0` only when that kernel will load the pinned, signed driver:

```bash
/usr/local/libexec/msi-fan-kernel-readiness [KERNEL_RELEASE]
```

Without an argument it checks the kernel `/boot/vmlinuz` points to. Run it as a
normal user. The kernel postinst hook
`/etc/kernel/postinst.d/zz-msi-fan-profile-readiness` runs it for every
installed kernel and logs the result with the `msi-fan-profile` tag
(`sudo journalctl -t msi-fan-profile`). A DKMS signing failure does not fail a
kernel installation, so a `FAIL` here is often the only warning before the
reboot. Do not reboot into a kernel that fails; the fans would stay on firmware
control.

Each `FAIL` line names the failed step: the `msi_ec/0.13.1` DKMS build for that
kernel, the module `modinfo` resolves, its pinned source version and
`vermagic`, its signature by the enrolled MOK certificate, `msi_wmi_platform`,
`coretemp`, or a `modprobe.d` override. `WARN` lines do not fail the check. If
header packages were configured after the kernel image, DKMS may build the
module after the hook ran; run the check again by hand before deciding. Repair a
failure for that kernel in an attended session, or boot a kernel the check
passes for; reboot into the new kernel only once its check exits `0`. If a
failing kernel has already booted, see
[Controller failed: driver not loaded](#controller-failed-driver-not-loaded).
In the source checkout, which is not installed, the kernel update section of
`docs/ROLLBACK.md` explains each check and warning in full, and
`docs/DRIVER_INSTALL.md` covers the DKMS build, signing and MOK enrollment.

## Boot window

The controller starts after `systemd-modules-load` and after
`systemd-tmpfiles-setup`, which recreates its root-only locks. Until the journal
shows `Candidate14 applied`, the EC follows its firmware's own automatic curve.
That is thermally safe, but this project does not monitor it. On a normal boot
of the validation host the window is about 13 seconds.

When `/tmp` is not a tmpfs, `systemd-tmpfiles-setup` empties it at every boot
(Ubuntu's `D /tmp` rule), so large `/tmp` contents lengthen the window. On
2026-10-11 that step took 78 seconds, and Candidate 14 was applied 85 seconds
after boot. Keep large scratch data out of `/tmp`, or make `/tmp` a tmpfs; that
is a host choice. To measure a boot:

```bash
systemd-analyze blame | head -n 5
sudo journalctl -b -o short-monotonic -u msi-fan-profile.service --no-pager |
  grep 'Candidate14 applied'
```

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
the handoff remains a separate manual override. It never cancels the latch, and
it keeps Boost on after the latch's cooldown until it is itself released.

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
While an automatic latch is active (thermal alarm, invalid telemetry,
observation gap or recovery handoff), the daemon switches Boost straight back
on after a keyboard off. That off only withdraws manual Boost, so Boost goes
off once the latch's own 30-second cooldown completes. `status` reports both
owners as `health.manual_boost` and `health.automatic_boost`.

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
Run `apply-default` to return without rebooting. `factory-auto` never changes
boot enablement, so it also succeeds for a persistently disabled service, and
`status` accepts the factory override either way.

If the machine is too hot to release Cooler Boost (CPU package or EC CPU at or
above 85 °C, or EC GPU at or above 70 °C), or the temperature telemetry is
invalid or unavailable, `factory-auto` exits `1` with
`factory staged with Boost on; ... rerun factory-auto when cool`. The verified
factory curve and auto mode are in place with Cooler Boost on. The marker,
boot enablement, and any recovery-Boost marker are left as they were, so
`status` exits `1` until the transition completes. Rerun `factory-auto` once the
machine is cool, or run `apply-default` to resume Candidate 14; a retained
recovery marker then makes its Boost an automatic 30-second-cooldown latch.
Any other failure, such as an identity, stop, lock, EC readback, or
postcondition failure, still uses the fail-safe that persistently disables the
service with factory auto and Boost on.

If the daemon notices the marker before the stop request arrives, it takes the
same clean-stop path as for the stop request, not a fault exit.

## Stopping or rebooting while hot

A clean stop releases Cooler Boost only when it is cool. When a
`systemctl stop` or a reboot catches the machine hot, the journal shows
`ERROR ProfileError: factory restored; Boost release unsafe`, followed by
`runtime recovery verified Candidate14/advanced with Cooler Boost on`. This is
intended. While the system keeps running, the unit is failed, the keeper holds
full cooling, and `status` exits `1`; run `sudo msi-fan-profile apply-default`
once the machine is cool. At shutdown the keeper cannot start, and systemd logs
`Failed to enqueue OnFailure= job`. The EC keeps full cooling until power-off,
and the next boot applies Candidate 14 as usual. Inside `factory-auto` the
result is the `factory staged with Boost on` state above. To get the
`successful candidate14 service left thermally verified factory auto/off` line
at a reboot, reboot when cool. See `SAFETY.md`.

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

## After a keeper takeover (for example a memory-limit kill)

`status` exits `1` with an active keeper and `recovery_boost_latched` set: the
keeper holds Candidate 14/`advanced` with Cooler Boost on and never releases it.
Find out why the primary stopped:

```bash
msi-fan-profile status
sudo systemctl status msi-fan-profile.service msi-fan-profile-keeper.service --no-pager
sudo journalctl -u msi-fan-profile.service -u msi-fan-profile-keeper.service -b \
  --no-pager | tail -n 40
```

`Failed with result 'oom-kill'` with a `64.0M memory peak` means the controller
reached its own `MemoryMax=64M`. The 2026-09-30 build did so after about 6.8
days because of a health-publication leak, which 6ed5498 fixes. Then, while the
keeper keeps cooling:

1. Reduce heavy workloads.
2. Run `sudo msi-fan-profile apply-default`. It stops the keeper and starts the
   daemon, which reapplies Candidate 14 and keeps the inherited Boost as an
   automatic latch. Use it rather than `systemctl start`.
3. Wait at least 30 seconds. Once telemetry has been valid and cool for 30
   seconds, the journal shows
   `automatic Cooler Boost released after 30-second cooldown`. While it is
   still hot, `health.automatic_boost` stays `true`; wait instead of repeating
   commands.
4. Run `msi-fan-profile status` again. It should exit `0`, with the keeper
   inactive, no recovery latch, and `cooler_boost` `off`.
5. If the installed release leaks, install a fixed one in a cool window with the
   guarded `sudo ./scripts/upgrade-profile.sh` (see `docs/INSTALL.md` in the
   source checkout). Afterwards,
   check `controller_memory.memory_peak_bytes` over several days; it should
   stay far below 64 MiB.

## Controller failed: driver not loaded

After a kernel update, `msi-fan-profile.service` can appear in
`systemctl --failed` with no keeper running. Diagnose it:

```bash
msi-fan-profile status
sudo journalctl -u msi-fan-profile.service -b --no-pager | tail -n 20
uname -r
sudo dkms status -m msi_ec
modinfo -n msi_ec
/usr/local/libexec/msi-fan-kernel-readiness "$(uname -r)"
sudo journalctl -k -b --no-pager | grep -Ei 'msi[-_]ec|lockdown'
```

`msi_ec driver not loaded for kernel <release>` means no `msi_ec` module is
loaded. The usual causes are a failed DKMS build, a module signed with a key
that is not enrolled (refused under lockdown), or an in-tree `msi_ec` that
rejected this EC firmware.
`msi_ec driver loaded for kernel <release> is not the pinned 0.13.1 build` means
another `msi_ec`, usually the in-tree one, loaded instead. The readiness check
names the failing step. Neither the controller nor its recovery helper has
written to the EC, the keeper is skipped, and the fans are on the firmware's own
automatic control. Repair the driver for that kernel in an attended session, or
boot a kernel that passes the readiness check. Then run
`sudo msi-fan-profile apply-default`. See [Kernel updates](#kernel-updates)
and, in the source checkout, the kernel update section of `docs/ROLLBACK.md`.

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
use its separately requested, monitored release path. Boost goes off only when
neither holds it.

Stop or reduce the workload if this occurs. This fan-only controller cannot
guarantee a CPU temperature below the mid-90s under every sustained workload;
CPU power policy is deliberately outside its scope.

The same sustained state can result from a verified fan-stall alarm. Treat that
as a hardware fault and inspect the fan path before relying on the system.

## Shift mode drift

The daemon, including a start by `apply-default`, and `preflight.sh` require
`shift_mode` to be `comfort`. If it changes, the daemon fails with
`EC shift mode is no longer comfort`, and the keeper holds full cooling; the
keeper itself ignores the shift mode. **This project never writes
`shift_mode` and has no command to restore it.** First find what changed it,
such as another EC tool or a firmware update. Then, in an attended session
while the machine is cool:

```bash
sudo msi-fan-profile factory-auto               # must end in factory auto/off
cat /sys/devices/platform/msi-ec/shift_mode
echo comfort | sudo tee /sys/devices/platform/msi-ec/shift_mode >/dev/null
cat /sys/devices/platform/msi-ec/shift_mode     # must print comfort
sudo msi-fan-profile apply-default
msi-fan-profile status
```

`factory-auto` stops the daemon and keeper and blocks both from starting, so
nothing else writes to the EC during the single manual write. If it reports
`factory staged with Boost on`, wait until it is cool and rerun it first. Write
only `comfort`, the validated value. If the write or its readback fails, stay in
factory auto and investigate.

## Suspend and hibernation

On the validation host, `sleep.target`, `suspend.target`, `hibernate.target` and
`hybrid-sleep.target` are masked. Secure Boot lockdown also disables hibernation
there (`/sys/power/disk` reads `[disabled]`). A desktop idle-suspend request
therefore fails. The daemon's resume path, a forced Candidate 14 reapply, is
implemented and covered by mocked tests, but has not run in the field with the
current code. Keep the targets masked until the attended suspend test in
`docs/VALIDATION.md` of the source checkout passes. Check them with:

```bash
systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## Idle fan speed and the NVIDIA driver

The controller does not use the NVIDIA driver; it reads the GPU temperature from
the EC. Without an NVIDIA module for the running kernel, the discrete GPU stays
powered in D0 with no runtime power management. On the validation host
(7.0.0-34, and 7.0.0-38 until its module was installed), the idle EC GPU
temperature rose from about 33 °C to about 42–47 °C, and fan 2 ran at about
3,600–4,200 RPM instead of about 1,450 RPM. That is Candidate 14 responding as
designed, not a controller fault. Installing the matching NVIDIA packages is an
operator choice outside this project, and the kernel readiness check does not
cover it. To check a kernel before rebooting, and the bound driver after boot:

```bash
modinfo -k KERNEL_RELEASE -n nvidia
lspci -k -d 10de:
```

## Superseded calibration tools

A host used for calibration may still have tools from before this release, such
as `/usr/local/bin/msi-fanctl`. The installer neither installs, records nor
removes them. `msi-fanctl` writes `fan_mode` and Cooler Boost directly, accepts
an unpatched driver build, and releases Boost at up to 95 °C, above this
project's 85 °C gate. Its only coordination with the controller is
`/run/msi-fanctl.lock`, so it can still run whenever the controller is stopped,
failed or in factory override. Do not use it alongside this controller. Archive
or remove it, and any other unrecorded EC writer, in an attended session.

## Direct sysfs writes

Do not write the curve or fan mode manually during normal operation. Use the
manager. The driver interface exists for transactional, exact-profile operations
and recovery—not arbitrary experimentation. The attended `shift_mode` restore
above is the only manual EC write these documents describe.
