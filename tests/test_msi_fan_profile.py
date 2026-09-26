#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for manager activation failure boundaries."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


def load_manager():
    path = pathlib.Path(__file__).parents[1] / "src" / "msi-fan-profile"
    loader = importlib.machinery.SourceFileLoader("msi_fan_profile_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("unable to load msi-fan-profile")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


MANAGER = load_manager()


class ApplyDefaultFailureTests(unittest.TestCase):
    def test_apply_default_stops_the_keeper_before_starting_the_daemon(self):
        calls = []
        healthy = {
            "service_enabled": True,
            "service_active_state": "active",
            "service_sub_state": "running",
            "service_main_pid": 17,
            "service_job": "",
            "keeper_active_state": "inactive",
            "keeper_sub_state": "dead",
            "keeper_main_pid": 0,
            "keeper_job": "",
            "factory_override": False,
            "curve": "candidate14",
            "fan_mode": "advanced",
        }

        def fake_ctl(action, *args, **_kwargs):
            calls.append((action, *args))
            return mock.Mock(stdout="", returncode=0)

        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", return_value=9), \
                mock.patch.object(MANAGER, "marker", return_value=False), \
                mock.patch.object(MANAGER, "stop_keeper", side_effect=lambda: calls.append(("stop_keeper",))), \
                mock.patch.object(MANAGER, "ctl", side_effect=fake_ctl), \
                mock.patch.object(MANAGER, "full_status", return_value=healthy), \
                mock.patch.object(MANAGER.os, "close"):
            MANAGER.apply_default()

        self.assertLess(calls.index(("stop_keeper",)),
                        calls.index(("start", MANAGER.SERVICE)))

    def test_start_failure_does_not_overwrite_runtime_recovery(self):
        def fake_ctl(action, *_args, **_kwargs):
            if action == "start":
                raise MANAGER.ManagerError("start failed")
            return None

        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", return_value=9), \
                mock.patch.object(MANAGER, "marker", return_value=False), \
                mock.patch.object(MANAGER, "stop_keeper"), \
                mock.patch.object(MANAGER, "ctl", side_effect=fake_ctl), \
                mock.patch.object(MANAGER, "disable_and_safe_factory") as factory, \
                mock.patch.object(MANAGER.os, "close"):
            with self.assertRaisesRegex(
                    MANAGER.ManagerError, "runtime full-cooling recovery untouched"):
                MANAGER.apply_default()
        factory.assert_not_called()

    def test_post_start_invariant_failure_does_not_overwrite_runtime_recovery(self):
        invalid = {
            "service_enabled": True,
            "service_active_state": "failed",
            "service_sub_state": "failed",
            "service_main_pid": 0,
            "service_job": "",
            "keeper_active_state": "inactive",
            "keeper_sub_state": "dead",
            "keeper_main_pid": 0,
            "keeper_job": "",
            "factory_override": False,
            "curve": "candidate14",
            "fan_mode": "advanced",
        }
        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", return_value=9), \
                mock.patch.object(MANAGER, "marker", return_value=False), \
                mock.patch.object(MANAGER, "stop_keeper"), \
                mock.patch.object(MANAGER, "ctl"), \
                mock.patch.object(MANAGER, "full_status", return_value=invalid), \
                mock.patch.object(MANAGER, "disable_and_safe_factory") as factory, \
                mock.patch.object(MANAGER.os, "close"):
            with self.assertRaisesRegex(
                    MANAGER.ManagerError, "runtime full-cooling recovery untouched"):
                MANAGER.apply_default()
        factory.assert_not_called()

    def test_pre_start_setup_failure_keeps_the_existing_factory_fail_safe(self):
        def fake_ctl(action, *_args, **_kwargs):
            if action == "enable":
                raise MANAGER.ManagerError("enable failed")
            return None

        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", return_value=9), \
                mock.patch.object(MANAGER, "marker", return_value=False), \
                mock.patch.object(MANAGER, "stop_keeper"), \
                mock.patch.object(MANAGER, "ctl", side_effect=fake_ctl), \
                mock.patch.object(MANAGER, "disable_and_safe_factory") as factory, \
                mock.patch.object(MANAGER.os, "close"):
            with self.assertRaisesRegex(MANAGER.ManagerError, "service disabled"):
                MANAGER.apply_default()
        factory.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
