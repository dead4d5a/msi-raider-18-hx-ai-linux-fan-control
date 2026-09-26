#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the daemon's non-hardware thermal state machine."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import types
import unittest
from unittest import mock


def load_daemon():
    path = pathlib.Path(__file__).parents[1] / "src" / "msi-fan-profiled"
    loader = importlib.machinery.SourceFileLoader("msi_fan_profiled_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("unable to load msi-fan-profiled")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


DAEMON = load_daemon()


class AlarmHoldTests(unittest.TestCase):
    def test_sustained_verified_alarm_stays_an_active_state(self):
        alarm = DAEMON.AlarmHold()

        self.assertEqual(alarm.record_verification(0.0), "established")
        self.assertIsNone(alarm.record_verification(14.9))
        self.assertEqual(alarm.record_verification(15.0), "sustained")
        self.assertTrue(alarm.sustained)
        self.assertEqual(alarm.record_verification(75.0), "ongoing")

        self.assertTrue(alarm.clear())
        self.assertFalse(alarm.sustained)
        self.assertEqual(alarm.record_verification(76.0), "established")

    def test_report_is_rate_limited_after_the_sustained_transition(self):
        alarm = DAEMON.AlarmHold()
        alarm.record_verification(0.0)
        self.assertEqual(alarm.record_verification(15.0), "sustained")
        self.assertIsNone(alarm.record_verification(74.9))
        self.assertEqual(alarm.record_verification(75.0), "ongoing")

    def test_safety_alarm_thresholds_remain_unchanged(self):
        safety = DAEMON.Safety()
        self.assertIsNone(safety.update(98000, 40, 4000, 4000, 0.0))
        self.assertIsNone(safety.update(98000, 40, 4000, 4000, 8.9))
        self.assertEqual(
            safety.update(98000, 40, 4000, 4000, 9.0),
            "CPU >=98 C for 9 seconds")


class SensorHoldTests(unittest.TestCase):
    def test_degraded_sensor_latch_never_expires_and_rate_limits_reports(self):
        hold = DAEMON.SensorHold()

        self.assertEqual(hold.record(0.0), "established")
        self.assertIsNone(hold.record(4.9))
        self.assertIsNone(hold.record(59.9))
        self.assertEqual(hold.record(60.0), "ongoing")
        self.assertEqual(hold.record(300.0), "ongoing")
        self.assertEqual(hold.elapsed(300.0), 300.0)
        self.assertTrue(hold.clear())
        self.assertFalse(hold.clear())

    def test_runtime_sample_identifies_the_ec_cpu_sentinel(self):
        values = iter((50000, 255, 40, 4300, 4300))
        with mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")), \
                mock.patch.object(DAEMON, "read_uint", side_effect=lambda _path: next(values)):
            with self.assertRaisesRegex(
                    DAEMON.SensorSampleError, "EC CPU temperature sentinel 255C"):
                DAEMON.read_runtime_sample(
                    pathlib.Path("/fake/core"), pathlib.Path("/fake/wmi"))

    def test_runtime_sample_reports_the_actual_implausible_channel(self):
        values = iter((50000, 50, 40, 65535, 4300))
        with mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")), \
                mock.patch.object(DAEMON, "read_uint", side_effect=lambda _path: next(values)):
            with self.assertRaisesRegex(DAEMON.SensorSampleError, "fan1=65535RPM"):
                DAEMON.read_runtime_sample(
                    pathlib.Path("/fake/core"), pathlib.Path("/fake/wmi"))

    def test_impossible_fan_value_is_not_a_boost_verification(self):
        with mock.patch.object(DAEMON, "write"), \
                mock.patch.object(DAEMON, "read_uint", return_value=65535), \
                mock.patch.object(DAEMON.time, "sleep"):
            with self.assertRaisesRegex(DAEMON.SensorSampleError, "verification deferred"):
                DAEMON.verify_boost(pathlib.Path("/fake/wmi"), lambda _status: None)

    def test_plausible_low_fans_remain_a_physical_boost_failure(self):
        with mock.patch.object(DAEMON, "write"), \
                mock.patch.object(DAEMON, "read_uint", return_value=1200), \
                mock.patch.object(DAEMON.time, "sleep"):
            with self.assertRaisesRegex(DAEMON.ProfileError, "did not verify"):
                DAEMON.verify_boost(pathlib.Path("/fake/wmi"), lambda _status: None)

    def test_boost_verification_retries_one_malformed_wmi_sample(self):
        heartbeat = []
        values = iter((DAEMON.SensorSampleError("nonnumeric fan1"), 4500, 4600))
        with mock.patch.object(DAEMON, "write"), \
                mock.patch.object(DAEMON, "read_uint", side_effect=values), \
                mock.patch.object(DAEMON.time, "sleep"):
            DAEMON.verify_boost(pathlib.Path("/fake/wmi"), heartbeat.append)
        self.assertTrue(any("invalid fan telemetry" in item for item in heartbeat))


class SustainedAlarmMainLoopTests(unittest.TestCase):
    def test_verified_high_heat_does_not_exit_the_daemon(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def boottime(self, _clock_id):
                return self.now

            def sleep(self, seconds):
                self.now += seconds
                if self.now >= 26.0:
                    DAEMON.STOP = True

        class Lock:
            @staticmethod
            def stat():
                return types.SimpleNamespace(
                    st_mode=DAEMON.stat.S_IFREG | 0o600, st_uid=0, st_gid=0)

        class MissingOverride:
            @staticmethod
            def lstat():
                raise FileNotFoundError

        clock = Clock()
        state = {"curve": DAEMON.FACTORY, "mode": "auto", "boost": "off"}
        notifications = []

        def fake_read(path):
            text = str(path)
            if text == "/proc/self/cgroup":
                return "0::/system.slice/msi-fan-profile.service"
            if text.endswith("fan_curve"):
                return state["curve"]
            if text.endswith("fan_mode"):
                return state["mode"]
            if text.endswith("cooler_boost"):
                return state["boost"]
            raise AssertionError(f"unexpected text read: {text}")

        def fake_read_uint(path):
            text = str(path)
            values = {
                "/fake/core/temp1_input": 99000,
                "/fake/ec/cpu/realtime_temperature": 99,
                "/fake/ec/gpu/realtime_temperature": 40,
                "/fake/wmi/fan1_input": 5000,
                "/fake/wmi/fan2_input": 5000,
            }
            return values[text]

        def fake_write(path, value):
            if str(path).endswith("cooler_boost"):
                state["boost"] = value
            else:
                raise AssertionError(f"unexpected write: {path}")

        def fake_apply(_wmi, preserve_boost, _heartbeat):
            state["curve"] = DAEMON.DEFAULT
            state["mode"] = "advanced"
            state["boost"] = "on" if preserve_boost else "off"

        def fake_verify(_wmi, _heartbeat):
            state["boost"] = "on"

        original_stop = DAEMON.STOP
        original_boost_request = DAEMON.BOOST_REQUEST
        original_release_request = DAEMON.RELEASE_REQUEST
        DAEMON.STOP = False
        DAEMON.BOOST_REQUEST = False
        DAEMON.RELEASE_REQUEST = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")))
                stack.enter_context(mock.patch.object(DAEMON, "LOCK", Lock()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "OVERRIDE", MissingOverride()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read", side_effect=fake_read))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read_uint", side_effect=fake_read_uint))
                stack.enter_context(
                    mock.patch.object(DAEMON, "write", side_effect=fake_write))
                stack.enter_context(mock.patch.object(DAEMON, "require_policy_identity"))
                stack.enter_context(mock.patch.object(
                    DAEMON, "one_hwmon", side_effect=(
                        pathlib.Path("/fake/wmi"), pathlib.Path("/fake/core"))))
                stack.enter_context(mock.patch.object(
                    DAEMON, "force_apply_default", side_effect=fake_apply))
                stack.enter_context(mock.patch.object(
                    DAEMON, "verify_boost", side_effect=fake_verify))
                stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "release_factory_if_cool", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "notify", side_effect=notifications.append))
                stack.enter_context(mock.patch.object(
                    DAEMON.os, "geteuid", return_value=0))
                stack.enter_context(mock.patch.object(DAEMON.os, "open", return_value=9))
                stack.enter_context(mock.patch.object(DAEMON.os, "close"))
                stack.enter_context(mock.patch.object(DAEMON.fcntl, "flock"))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "monotonic", side_effect=clock.monotonic))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "clock_gettime", side_effect=clock.boottime))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "sleep", side_effect=clock.sleep))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DAEMON.main(), 0)
        finally:
            DAEMON.STOP = original_stop
            DAEMON.BOOST_REQUEST = original_boost_request
            DAEMON.RELEASE_REQUEST = original_release_request

        self.assertEqual(state["curve"], DAEMON.DEFAULT)
        self.assertEqual(state["mode"], "advanced")
        self.assertEqual(state["boost"], "on")
        self.assertTrue(any("Sustained safety alarm" in item for item in notifications))


class SensorHoldMainLoopTests(unittest.TestCase):
    def test_transient_bad_sample_latches_boost_until_valid_cooldown(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def boottime(self, _clock_id):
                return self.now

            def sleep(self, seconds):
                self.now += seconds
                if self.now >= 33.0:
                    DAEMON.STOP = True

        class Lock:
            @staticmethod
            def stat():
                return types.SimpleNamespace(
                    st_mode=DAEMON.stat.S_IFREG | 0o600, st_uid=0, st_gid=0)

        class MissingOverride:
            @staticmethod
            def lstat():
                raise FileNotFoundError

        clock = Clock()
        state = {"curve": DAEMON.FACTORY, "mode": "auto", "boost": "off"}
        notifications = []
        writes = []
        samples = 0

        def fake_read(path):
            text = str(path)
            if text == "/proc/self/cgroup":
                return "0::/system.slice/msi-fan-profile.service"
            if text.endswith("fan_curve"):
                return state["curve"]
            if text.endswith("fan_mode"):
                return state["mode"]
            if text.endswith("cooler_boost"):
                return state["boost"]
            raise AssertionError(f"unexpected text read: {text}")

        def fake_write(path, value):
            self.assertTrue(str(path).endswith("cooler_boost"))
            writes.append(value)
            state["boost"] = value

        def fake_apply(_wmi, preserve_boost, _heartbeat):
            state["curve"] = DAEMON.DEFAULT
            state["mode"] = "advanced"
            state["boost"] = "on" if preserve_boost else "off"

        def fake_sample(_core, _wmi):
            nonlocal samples
            samples += 1
            if samples == 1:
                raise DAEMON.SensorSampleError("fan1=65535RPM")
            return DAEMON.RuntimeSample(50000, 50, 40, 4500, 4500)

        def fake_verify(_wmi, _heartbeat):
            state["boost"] = "on"

        original_stop = DAEMON.STOP
        original_boost_request = DAEMON.BOOST_REQUEST
        original_release_request = DAEMON.RELEASE_REQUEST
        DAEMON.STOP = False
        DAEMON.BOOST_REQUEST = False
        DAEMON.RELEASE_REQUEST = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")))
                stack.enter_context(mock.patch.object(DAEMON, "LOCK", Lock()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "OVERRIDE", MissingOverride()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read", side_effect=fake_read))
                stack.enter_context(
                    mock.patch.object(DAEMON, "write", side_effect=fake_write))
                stack.enter_context(mock.patch.object(DAEMON, "require_policy_identity"))
                stack.enter_context(mock.patch.object(
                    DAEMON, "one_hwmon", side_effect=(
                        pathlib.Path("/fake/wmi"), pathlib.Path("/fake/core"))))
                stack.enter_context(mock.patch.object(
                    DAEMON, "force_apply_default", side_effect=fake_apply))
                stack.enter_context(mock.patch.object(
                    DAEMON, "read_runtime_sample", side_effect=fake_sample))
                verify = stack.enter_context(mock.patch.object(
                    DAEMON, "verify_boost", side_effect=fake_verify))
                restore = stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "release_factory_if_cool", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "notify", side_effect=notifications.append))
                stack.enter_context(mock.patch.object(
                    DAEMON.os, "geteuid", return_value=0))
                stack.enter_context(mock.patch.object(DAEMON.os, "open", return_value=9))
                stack.enter_context(mock.patch.object(DAEMON.os, "close"))
                stack.enter_context(mock.patch.object(DAEMON.fcntl, "flock"))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "monotonic", side_effect=clock.monotonic))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "clock_gettime", side_effect=clock.boottime))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "sleep", side_effect=clock.sleep))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DAEMON.main(), 0)
        finally:
            DAEMON.STOP = original_stop
            DAEMON.BOOST_REQUEST = original_boost_request
            DAEMON.RELEASE_REQUEST = original_release_request

        self.assertEqual(state["curve"], DAEMON.DEFAULT)
        self.assertEqual(state["mode"], "advanced")
        self.assertEqual(state["boost"], "off")
        self.assertEqual(writes, ["on", "off"])
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(restore.call_count, 1)  # Only the requested clean stop.
        self.assertTrue(any("Sensor telemetry degraded" in item for item in notifications))

    def test_persistent_bad_samples_remain_active_and_reassert_boost(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def boottime(self, _clock_id):
                return self.now

            def sleep(self, seconds):
                self.now += seconds
                if self.now == 10.0:
                    state["boost"] = "off"  # Simulate an external off toggle.
                if self.now >= 70.0:
                    DAEMON.STOP = True

        class Lock:
            @staticmethod
            def stat():
                return types.SimpleNamespace(
                    st_mode=DAEMON.stat.S_IFREG | 0o600, st_uid=0, st_gid=0)

        class MissingOverride:
            @staticmethod
            def lstat():
                raise FileNotFoundError

        clock = Clock()
        state = {"curve": DAEMON.FACTORY, "mode": "auto", "boost": "off"}
        notifications = []
        writes = []

        def fake_read(path):
            text = str(path)
            if text == "/proc/self/cgroup":
                return "0::/system.slice/msi-fan-profile.service"
            if text.endswith("fan_curve"):
                return state["curve"]
            if text.endswith("fan_mode"):
                return state["mode"]
            if text.endswith("cooler_boost"):
                return state["boost"]
            raise AssertionError(f"unexpected text read: {text}")

        def fake_write(path, value):
            self.assertTrue(str(path).endswith("cooler_boost"))
            writes.append(value)
            state["boost"] = value

        def fake_apply(_wmi, preserve_boost, _heartbeat):
            state["curve"] = DAEMON.DEFAULT
            state["mode"] = "advanced"
            state["boost"] = "on" if preserve_boost else "off"

        original_stop = DAEMON.STOP
        original_boost_request = DAEMON.BOOST_REQUEST
        original_release_request = DAEMON.RELEASE_REQUEST
        DAEMON.STOP = False
        DAEMON.BOOST_REQUEST = False
        DAEMON.RELEASE_REQUEST = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")))
                stack.enter_context(mock.patch.object(DAEMON, "LOCK", Lock()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "OVERRIDE", MissingOverride()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read", side_effect=fake_read))
                stack.enter_context(
                    mock.patch.object(DAEMON, "write", side_effect=fake_write))
                stack.enter_context(mock.patch.object(DAEMON, "require_policy_identity"))
                stack.enter_context(mock.patch.object(
                    DAEMON, "one_hwmon", side_effect=(
                        pathlib.Path("/fake/wmi"), pathlib.Path("/fake/core"))))
                stack.enter_context(mock.patch.object(
                    DAEMON, "force_apply_default", side_effect=fake_apply))
                stack.enter_context(mock.patch.object(
                    DAEMON, "read_runtime_sample", side_effect=
                        DAEMON.SensorSampleError("EC CPU temperature sentinel 255C")))
                verify = stack.enter_context(mock.patch.object(DAEMON, "verify_boost"))
                restore = stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "release_factory_if_cool", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "notify", side_effect=notifications.append))
                stack.enter_context(mock.patch.object(
                    DAEMON.os, "geteuid", return_value=0))
                stack.enter_context(mock.patch.object(DAEMON.os, "open", return_value=9))
                stack.enter_context(mock.patch.object(DAEMON.os, "close"))
                stack.enter_context(mock.patch.object(DAEMON.fcntl, "flock"))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "monotonic", side_effect=clock.monotonic))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "clock_gettime", side_effect=clock.boottime))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "sleep", side_effect=clock.sleep))
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(DAEMON.main(), 0)
        finally:
            DAEMON.STOP = original_stop
            DAEMON.BOOST_REQUEST = original_boost_request
            DAEMON.RELEASE_REQUEST = original_release_request

        self.assertEqual(state["curve"], DAEMON.DEFAULT)
        self.assertEqual(state["mode"], "advanced")
        self.assertEqual(state["boost"], "on")
        self.assertGreaterEqual(writes.count("on"), 2)
        self.assertNotIn("off", writes)
        verify.assert_not_called()
        restore.assert_called_once()  # Only the requested clean stop.
        self.assertTrue(any("Sensor telemetry degraded 60s" in item
                            for item in notifications))


if __name__ == "__main__":
    unittest.main()
