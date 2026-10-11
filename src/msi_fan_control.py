# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact driver ABI validation and fresh, read-only control snapshots.

The legacy and snapshot drivers are separate, explicitly pinned builds. A
snapshot-capable build must provide its ABI; a broken ABI never falls back to
less expensive checks or to an unvalidated module.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from typing import Callable

LEGACY_SRCVERSION = "AB0BFAE2391B5ADD66E01BD"
SNAPSHOT_SRCVERSION = "9086C45007CBB7FA1264430"
SUPPORTED_SRCVERSIONS = (LEGACY_SRCVERSION, SNAPSHOT_SRCVERSION)
FIRMWARE = "1824EMS1.108"
MODULE_PATH = pathlib.Path("/sys/module/msi_ec")
VERSION_PATH = MODULE_PATH / "version"
SOURCE_PATH = MODULE_PATH / "srcversion"


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
