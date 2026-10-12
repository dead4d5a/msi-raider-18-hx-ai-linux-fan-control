# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact driver ABI validation, fresh read-only control snapshots, and the
read-only cool confirmation behind every Cooler Boost release.

The legacy and snapshot drivers are separate, explicitly pinned builds. A
snapshot-capable build must provide its ABI; a broken ABI never falls back to
less expensive checks or to an unvalidated module.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Callable

LEGACY_SRCVERSION = "AB0BFAE2391B5ADD66E01BD"
SNAPSHOT_SRCVERSION = "9086C45007CBB7FA1264430"
SUPPORTED_SRCVERSIONS = (LEGACY_SRCVERSION, SNAPSHOT_SRCVERSION)
FIRMWARE = "1824EMS1.108"
MODULE_PATH = pathlib.Path("/sys/module/msi_ec")
VERSION_PATH = MODULE_PATH / "version"
SOURCE_PATH = MODULE_PATH / "srcversion"

# Releasing Cooler Boost, or leaving the factory envelope at auto/off, is the
# one decision that makes the machine quieter, so one reading never makes it.
# Under bursty load the coretemp package reading jumps 20 C or more for a few
# milliseconds, and the EC sometimes returns junk such as 255 C or an empty
# value. A release therefore needs COOL_CONFIRM_SAMPLES consecutive valid, cool
# samples COOL_CONFIRM_INTERVAL_SECONDS apart (at least 2.5 s of agreement)
# within COOL_CONFIRM_TIMEOUT_SECONDS. An invalid or hot sample restarts the
# count, so one spike or junk value can neither release Boost nor, by itself,
# refuse a release. The limits are the release limits used before: the CPU
# package and EC CPU below 85 C, the EC GPU below 70 C (0 is a powered-off
# dGPU). The ranges reject junk: a CPU at 0 C is never plausible.
#
# The daemon's clean stop has TimeoutStopSec=30s, armed by SIGTERM, for the
# loop exit, the factory restore and this wait. A stop during startup can
# first spend up to 10 s waiting for hwmon registration and 12 s verifying
# Boost, so the daemon passes only what remains of the stop timeout, less a
# 5 s exit margin, as the timeout, and refuses the release without sampling
# when that cannot hold one window; it then exits into full-cooling recovery
# instead of being killed. It sends a watchdog heartbeat with every sample, so
# WatchdogSec=5s, which is still enforced when the factory-auto marker starts
# the stop before SIGTERM, never expires. ExecStopPost runs under its own
# TimeoutStopSec=30s. The manager has no watchdog; factory-auto's 50 s
# systemctl stop covers both waits above. Entering cooling never waits for
# this: callers reassert Boost at once on an unsafe sample.
COOL_PACKAGE_LIMIT_MILLIDEGREES = 85000
COOL_EC_CPU_LIMIT_CELSIUS = 85
COOL_EC_GPU_LIMIT_CELSIUS = 70
PACKAGE_RANGE_MILLIDEGREES = (1, 110000)
EC_CPU_RANGE_CELSIUS = (1, 110)
EC_GPU_RANGE_CELSIUS = (0, 110)
COOL_CONFIRM_SAMPLES = 6
COOL_CONFIRM_INTERVAL_SECONDS = 0.5
COOL_CONFIRM_TIMEOUT_SECONDS = 10.0


class ControlABIError(RuntimeError):
    pass


def missing_driver_error() -> ControlABIError:
    """Name the running kernel when the pinned module exposes no version.

    After a kernel update this is the usual sign of a failed DKMS build or
    signature, or of an in-tree msi_ec that rejected this EC firmware.
    """
    release = os.uname().release
    if MODULE_PATH.exists():
        return ControlABIError(
            f"msi_ec driver loaded for kernel {release} is not the pinned "
            "0.13.1 build: it has no module version")
    return ControlABIError(f"msi_ec driver not loaded for kernel {release}")


def host_identity(read: Callable[[pathlib.Path], str],
                  base: pathlib.Path) -> dict[pathlib.Path, str]:
    """Validate immutable host/module identity without another EC transaction."""
    if pathlib.Path("/sys/module/ec_sys").exists():
        raise ControlABIError("ec_sys is loaded")
    expected = {
        pathlib.Path("/sys/class/dmi/id/product_name"): "Raider 18 HX AI A2XWIG",
        pathlib.Path("/sys/class/dmi/id/board_name"): "MS-1824",
        pathlib.Path("/sys/class/dmi/id/bios_version"): "E1824IMS.310",
        VERSION_PATH: "0.13.1",
    }
    values = {}
    for path, wanted in expected.items():
        try:
            actual = read(path)
        except FileNotFoundError as error:
            if path != VERSION_PATH:
                raise
            raise missing_driver_error() from error
        if actual != wanted:
            raise ControlABIError(f"identity mismatch {path}={actual!r}")
        values[path] = actual
    source = read(SOURCE_PATH)
    if source not in SUPPORTED_SRCVERSIONS:
        raise ControlABIError(f"unsupported exact driver build: {source!r}")
    if source == SNAPSHOT_SRCVERSION and not (base / "fan_control_snapshot").is_file():
        raise ControlABIError("pinned snapshot driver is missing its required ABI")
    values[SOURCE_PATH] = source
    return values


def identity(read: Callable[[pathlib.Path], str], base: pathlib.Path,
             *, monitoring: bool = False,
             firmware_checked_by_curve_read: bool = False) -> dict[pathlib.Path, str]:
    values = host_identity(read, base)
    # Monitoring on the new ABI gets the fresh firmware check in its snapshot.
    # The pinned legacy build's fan_curve read re-checks DMI and EC firmware
    # in the kernel and fails with EOPNOTSUPP on a mismatch. Only the 1 Hz
    # loops opt in to rely on it: they read fan_curve before any other EC
    # value of the pass. Other monitoring callers, such as the manager's status,
    # still read and return fw_version. Every mutation calls the default and
    # performs its own fresh check.
    if (not monitoring or (values[SOURCE_PATH] == LEGACY_SRCVERSION and
                           not firmware_checked_by_curve_read)):
        firmware = read(base / "fw_version")
        if firmware != FIRMWARE:
            raise ControlABIError(f"EC firmware mismatch: {firmware!r}")
        values[base / "fw_version"] = firmware
    return values


@dataclass(frozen=True)
class ControlSnapshot:
    firmware: str
    shift: str
    mode: str
    boost: str
    ec_cpu: int
    ec_gpu: int
    curve: str


def parse_snapshot(raw: str) -> ControlSnapshot:
    raw = raw.removesuffix("\n")
    if not raw.isascii() or any(ord(character) < 32 or ord(character) > 126
                              for character in raw):
        raise ControlABIError("control snapshot must be one printable ASCII line")
    tokens = raw.split()
    if len(tokens) != 33 or tokens[0] != "1" or tokens[1] != FIRMWARE:
        raise ControlABIError("unsupported control snapshot schema/firmware/length")
    if (tokens[2] not in ("turbo", "eco", "comfort", "unspecified") or
            tokens[3] not in ("auto", "silent", "advanced") or
            tokens[4] not in ("on", "off")):
        raise ControlABIError("invalid control snapshot state")
    numbers = []
    for token in tokens[5:]:
        if not token.isdecimal() or not 0 <= int(token) <= 255:
            raise ControlABIError("invalid control snapshot byte")
        numbers.append(int(token))
    return ControlSnapshot(tokens[1], tokens[2], tokens[3], tokens[4],
                           numbers[0], numbers[1],
                           " ".join(str(value) for value in numbers[2:]))


def snapshot(read: Callable[[pathlib.Path], str], base: pathlib.Path) -> ControlSnapshot:
    """One fresh acquisition; never cache or retry a malformed driver result."""
    return parse_snapshot(read(base / "fan_control_snapshot"))


@dataclass(frozen=True)
class CoolReading:
    """One classified release sample: cool, hot, invalid or unavailable."""
    state: str
    detail: str


def classify_cool_reading(package: object, ec_cpu: object,
                          ec_gpu: object) -> CoolReading:
    """Classify raw package (mC), EC CPU and EC GPU (C) attribute text.

    Only one to six ASCII digits inside the plausible range are valid, so the
    EC's 255 sentinel, an empty, signed or non-numeric value, and a CPU at 0 C
    never count as cool.
    """
    values: list[int] = []
    problems = []
    for label, raw, (low, high) in (
            ("CPU package", package, PACKAGE_RANGE_MILLIDEGREES),
            ("EC CPU", ec_cpu, EC_CPU_RANGE_CELSIUS),
            ("EC GPU", ec_gpu, EC_GPU_RANGE_CELSIUS)):
        if (isinstance(raw, str) and raw.isascii() and raw.isdecimal() and
                len(raw) <= 6 and low <= int(raw) <= high):
            values.append(int(raw))
        else:
            problems.append(f"{label} invalid ({raw!r:.24})")
    if problems:
        return CoolReading("invalid", "; ".join(problems))
    package_mc, cpu, gpu = values
    hot_package = package_mc >= COOL_PACKAGE_LIMIT_MILLIDEGREES
    hot_cpu = cpu >= COOL_EC_CPU_LIMIT_CELSIUS
    hot_gpu = not (gpu == 0 or gpu < COOL_EC_GPU_LIMIT_CELSIUS)
    detail = ", ".join((
        f"CPU package {package_mc // 1000}.{package_mc % 1000 // 100} C" +
        (f" >= {COOL_PACKAGE_LIMIT_MILLIDEGREES // 1000} C" if hot_package else ""),
        f"EC CPU {cpu} C" + (f" >= {COOL_EC_CPU_LIMIT_CELSIUS} C" if hot_cpu else ""),
        f"EC GPU {gpu} C" + (f" >= {COOL_EC_GPU_LIMIT_CELSIUS} C" if hot_gpu else "")))
    return CoolReading("hot" if hot_package or hot_cpu or hot_gpu else "cool", detail)


def cool_sampler(read: Callable[[pathlib.Path], str], core: pathlib.Path,
                 base: pathlib.Path) -> Callable[[], tuple[str, str, str]]:
    """Read the three release temperatures from the caller's own reader."""
    def sample() -> tuple[str, str, str]:
        return (read(core / "temp1_input"),
                read(base / "cpu/realtime_temperature"),
                read(base / "gpu/realtime_temperature"))
    return sample


@dataclass(frozen=True)
class CoolConfirmation:
    cool: bool
    samples: int
    elapsed: float
    last: CoolReading
    unsafe: CoolReading | None
    reason: str


def confirm_cool(sample: Callable[[], tuple[str, str, str]], *,
                 heartbeat: Callable[[str], None] | None = None,
                 on_unsafe: Callable[[CoolReading], None] | None = None,
                 monotonic: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 samples: int = COOL_CONFIRM_SAMPLES,
                 interval: float = COOL_CONFIRM_INTERVAL_SECONDS,
                 timeout: float = COOL_CONFIRM_TIMEOUT_SECONDS) -> CoolConfirmation:
    """Confirm coolness over consecutive valid samples before a Boost release.

    Samples start at least one interval apart. An unreadable sample (OSError
    or ValueError) is as unsafe as a hot or invalid one: it resets the count
    and calls on_unsafe, which callers use to reassert Boost at once. The
    result is not cool as soon as the remaining samples can no longer fit
    before the deadline, so the wait never exceeds the timeout plus one
    sample. heartbeat, if given, receives progress after every sample.
    Exceptions from sample (other than OSError/ValueError), heartbeat and
    on_unsafe propagate, and every caller then keeps Boost on.
    """
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    if samples < 1 or interval <= 0 or timeout < (samples - 1) * interval:
        raise ValueError("cool confirmation window cannot hold its samples")
    started = monotonic()
    deadline = started + timeout
    consecutive = taken = 0
    unsafe: CoolReading | None = None
    while True:
        sampled_at = monotonic()
        try:
            reading = classify_cool_reading(*sample())
        except (OSError, ValueError) as error:
            reading = CoolReading("unavailable", f"telemetry unavailable: {error}")
        taken += 1
        if reading.state == "cool":
            consecutive += 1
        else:
            consecutive = 0
            unsafe = reading
            if on_unsafe is not None:
                on_unsafe(reading)
        if heartbeat is not None:
            heartbeat(f"confirming cool before Cooler Boost release: {consecutive}/"
                      f"{samples} consecutive valid cool samples; {reading.detail}")
        elapsed = monotonic() - started
        if consecutive >= samples:
            return CoolConfirmation(
                True, taken, elapsed, reading, unsafe,
                f"{samples} consecutive valid cool samples {interval:g} s apart "
                f"after {elapsed:.1f} s; last: {reading.detail}")
        # A slow read can push the next start past its schedule; never start a
        # sample after the deadline or a window that can no longer complete.
        next_sample = max(sampled_at + interval, monotonic())
        if next_sample + (samples - consecutive - 1) * interval > deadline:
            last = (f"most recent unsafe sample ({unsafe.state}): {unsafe.detail}"
                    if unsafe else f"samples too slow; last: {reading.detail}")
            return CoolConfirmation(
                False, taken, elapsed, reading, unsafe,
                f"no {samples} consecutive valid cool samples {interval:g} s apart "
                f"within {timeout:g} s ({taken} samples in {elapsed:.1f} s); {last}")
        delay = next_sample - monotonic()
        if delay > 0:
            sleep(delay)


def read_attribute(path: pathlib.Path) -> str:
    with path.open("r", encoding="ascii") as stream:
        return stream.read().strip()


def main(argv: list[str]) -> int:
    """Read-only uninstall check: exit 0 only once coolness is confirmed.

    Usage: msi_fan_control.py confirm-cool HWMON_ROOT EC_BASE. Exactly one
    coretemp hwmon must exist below HWMON_ROOT, as every controller requires.
    """
    if len(argv) != 3 or argv[0] != "confirm-cool":
        print("usage: msi_fan_control.py confirm-cool HWMON_ROOT EC_BASE",
              file=sys.stderr)
        return 2
    cores = []
    for path in sorted(pathlib.Path(argv[1]).glob("hwmon*")):
        try:
            if read_attribute(path / "name") == "coretemp":
                cores.append(path)
        except (OSError, ValueError):
            continue
    if len(cores) != 1:
        print(f"Cooler Boost release not confirmed: expected one coretemp hwmon; "
              f"found {len(cores)}", file=sys.stderr)
        return 1
    confirmation = confirm_cool(cool_sampler(read_attribute, cores[0],
                                             pathlib.Path(argv[2])))
    print(("Cooler Boost release confirmed: " if confirmation.cool else
           "Cooler Boost release not confirmed: ") + confirmation.reason,
          file=sys.stderr)
    return 0 if confirmation.cool else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
