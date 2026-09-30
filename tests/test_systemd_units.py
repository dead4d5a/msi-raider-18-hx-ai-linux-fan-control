#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Static lifecycle invariants for the main controller and failure keeper."""

from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class FailureKeeperUnitTests(unittest.TestCase):
    def test_diagnostic_timing_defaults_off_for_both_services(self):
        for filename in ('msi-fan-profile.service', 'msi-fan-profile-keeper.service'):
            with self.subTest(filename=filename):
                unit = (ROOT / 'systemd' / filename).read_text()
                self.assertIn('Environment=MSI_FAN_TIMING_METRICS=0', unit)
                self.assertNotIn('Environment=MSI_FAN_TIMING_METRICS=1', unit)

    def test_failed_controller_starts_the_separate_full_cooling_keeper(self):
        primary = (ROOT / "systemd" / "msi-fan-profile.service").read_text(
            encoding="utf-8")
        self.assertIn("OnFailure=msi-fan-profile-keeper.service", primary)
        self.assertIn("Restart=no", primary)

    def test_keeper_yields_to_a_managed_controller_restart(self):
        keeper = (ROOT / "systemd" / "msi-fan-profile-keeper.service").read_text(
            encoding="utf-8")
        for line in (
            "Conflicts=msi-fan-profile.service",
            "Before=msi-fan-profile.service",
            "ExecStart=/usr/local/libexec/msi-gpu-recover --keeper candidate14",
            "Restart=on-failure",
            "WatchdogSec=5s",
        ):
            self.assertIn(line, keeper)
        self.assertNotIn("[Install]", keeper)


if __name__ == "__main__":
    unittest.main()
