#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for runtime full-cooling recovery."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


def load_recovery_helper():
    path = pathlib.Path(__file__).parents[1] / "src" / "msi-gpu-recover"
    loader = importlib.machinery.SourceFileLoader("msi_gpu_recover_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("unable to load msi-gpu-recover")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


RECOVER = load_recovery_helper()


class FullCoolingRecoveryTests(unittest.TestCase):
    def test_candidate14_recovery_finishes_advanced_with_boost_on(self):
        """A runtime fault must not replace the selected curve with factory auto."""
        state = {
            "fan_mode": "auto",
            "fan_curve": RECOVER.FACTORY,
            "cooler_boost": "off",
        }
        writes = []

        def fake_read(path):
            return state[path.name]

        def fake_write(path, value):
            writes.append((path.name, value))
            state[path.name] = value

        def fake_verify(wmi):
            self.assertEqual(wmi, pathlib.Path("/fake/wmi"))
            return True

        with mock.patch.object(RECOVER, "read", side_effect=fake_read), \
                mock.patch.object(RECOVER, "write", side_effect=fake_write), \
                mock.patch.object(
                    RECOVER, "identity_safe_for_curve_write", return_value=True), \
                mock.patch.object(
                    RECOVER, "one_hwmon", return_value=pathlib.Path("/fake/wmi")), \
                mock.patch.object(RECOVER, "verify_boost", side_effect=fake_verify):
            self.assertTrue(RECOVER.attempt_full_cooling("candidate14"))

        self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
        self.assertEqual(state["fan_mode"], "advanced")
        self.assertEqual(state["cooler_boost"], "on")
        self.assertEqual(
            writes[:4],
            [
                ("cooler_boost", "on"),
                ("fan_mode", "auto"),
                ("fan_curve", RECOVER.DEFAULT),
                ("fan_mode", "advanced"),
            ],
        )

    def test_impossible_fan_value_is_not_a_physical_boost_verification(self):
        """EC's 65535 sentinel must never be mistaken for a fast fan."""
        with mock.patch.object(RECOVER, "read_uint", return_value=65535), \
                mock.patch.object(RECOVER.time, "sleep"):
            self.assertFalse(RECOVER.verify_boost(pathlib.Path("/fake/wmi")))

    def test_failed_rpm_verification_leaves_full_cooling_requested(self):
        """A bad WMI reading may fail recovery, but may not reset to factory mode."""
        state = {
            "fan_mode": "auto",
            "fan_curve": RECOVER.FACTORY,
            "cooler_boost": "off",
        }

        def fake_read(path):
            return state[path.name]

        def fake_write(path, value):
            state[path.name] = value

        with mock.patch.object(RECOVER, "read", side_effect=fake_read), \
                mock.patch.object(RECOVER, "write", side_effect=fake_write), \
                mock.patch.object(
                    RECOVER, "identity_safe_for_curve_write", return_value=True), \
                mock.patch.object(
                    RECOVER, "one_hwmon", return_value=pathlib.Path("/fake/wmi")), \
                mock.patch.object(RECOVER, "verify_boost", return_value=False):
            self.assertFalse(RECOVER.attempt_full_cooling("candidate14"))

        self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
        self.assertEqual(state["fan_mode"], "advanced")
        self.assertEqual(state["cooler_boost"], "on")

    def test_wmi_discovery_failure_still_stages_full_cooling(self):
        """A missing RPM channel cannot delay the recovery Boost request."""
        state = {
            "fan_mode": "auto",
            "fan_curve": RECOVER.FACTORY,
            "cooler_boost": "off",
        }

        def fake_read(path):
            return state[path.name]

        def fake_write(path, value):
            state[path.name] = value

        with mock.patch.object(RECOVER, "read", side_effect=fake_read), \
                mock.patch.object(RECOVER, "write", side_effect=fake_write), \
                mock.patch.object(
                    RECOVER, "identity_safe_for_curve_write", return_value=True), \
                mock.patch.object(
                    RECOVER, "one_hwmon", side_effect=RuntimeError("WMI absent")):
            self.assertFalse(RECOVER.attempt_full_cooling("candidate14"))

        self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
        self.assertEqual(state["fan_mode"], "advanced")
        self.assertEqual(state["cooler_boost"], "on")

    def test_keeper_reasserts_boost_without_rewriting_a_healthy_curve(self):
        state = {
            "fan_mode": "advanced",
            "fan_curve": RECOVER.DEFAULT,
            "cooler_boost": "off",
        }
        writes = []

        def fake_read(path):
            return state[path.name]

        def fake_write(path, value):
            writes.append((path.name, value))
            state[path.name] = value

        with mock.patch.object(RECOVER, "read", side_effect=fake_read), \
                mock.patch.object(RECOVER, "write", side_effect=fake_write), \
                mock.patch.object(
                    RECOVER, "identity_safe_for_curve_write", return_value=True):
            self.assertEqual(
                RECOVER.keeper_repair("candidate14"), "Cooler Boost reasserted")

        self.assertEqual(writes, [("cooler_boost", "on")])
        self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
        self.assertEqual(state["fan_mode"], "advanced")
        self.assertEqual(state["cooler_boost"], "on")


if __name__ == "__main__":
    unittest.main()
