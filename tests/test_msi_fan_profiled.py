#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the daemon's non-hardware thermal state machine."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
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
    def test_each_alarm_threshold_and_interruption(self):
        scenarios = (
            ((100000, 40, 4000, 4000), 2, "CPU >=100 C for 2 seconds"),
            ((98000, 40, 4000, 4000), 9, "CPU >=98 C for 9 seconds"),
            ((50000, 82, 4000, 4000), 2, "GPU >=82 C for 2 seconds"),
            ((50000, 80, 4000, 4000), 9, "GPU >=80 C for 9 seconds"),
            ((80000, 40, 0, 4000), 4, "fan 1 stalled for 4 seconds"),
            ((50000, 70, 4000, 0), 4, "fan 2 stalled for 4 seconds"),
        )
        for hot, duration, expected in scenarios:
            with self.subTest(expected=expected):
                safety = DAEMON.Safety()
                self.assertIsNone(safety.update(*hot, 0))
                self.assertIsNone(safety.update(*hot, duration - 0.1))
                self.assertEqual(safety.update(*hot, duration), expected)
                safety = DAEMON.Safety()
                self.assertIsNone(safety.update(*hot, 0))
                self.assertIsNone(safety.update(50000, 40, 4000, 4000, duration - 0.1))
                self.assertIsNone(safety.update(*hot, duration))
                self.assertEqual(safety.update(*hot, duration * 2), expected)
        self.assertIn("immediate", DAEMON.Safety().update(103000, 40, 4000, 4000, 0))
        self.assertIn("immediate", DAEMON.Safety().update(50000, 85, 4000, 4000, 0))
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
    def test_low_spinup_then_missing_rpm_is_degraded_not_a_physical_failure(self):
        values = iter([1200, 1200] + [65535, 65535] * 11)
        with mock.patch.object(DAEMON, "write"), \
                mock.patch.object(DAEMON, "read_uint", side_effect=values), \
                mock.patch.object(DAEMON.time, "sleep"):
            with self.assertRaisesRegex(DAEMON.SensorSampleError, "verification deferred"):
                DAEMON.verify_boost(pathlib.Path("/fake/wmi"), lambda _status: None)

    def test_missing_then_two_low_samples_is_not_continuous_failure_evidence(self):
        values = iter([65535, 65535] * 10 + [1200, 1200] * 2)
        with mock.patch.object(DAEMON, "write"), \
                mock.patch.object(DAEMON, "read_uint", side_effect=values), \
                mock.patch.object(DAEMON.time, "sleep"):
            with self.assertRaises(DAEMON.SensorSampleError):
                DAEMON.verify_boost(pathlib.Path("/fake/wmi"), lambda _status: None)

    def test_sensor_io_failure_keeps_other_channels_available(self):
        values = iter([OSError("coretemp unavailable"), 255, 40, 0, 4500])
        with mock.patch.object(DAEMON, "read_uint", side_effect=values):
            with self.assertRaises(DAEMON.SensorSampleError) as raised:
                DAEMON.read_runtime_sample(pathlib.Path("/fake/core"), pathlib.Path("/fake/wmi"))
        self.assertEqual(raised.exception.partial,
                         DAEMON.RuntimeSample(None, None, 40, 0, 4500))
        self.assertEqual(set(raised.exception.channel_errors), {"package", "ec_cpu"})

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

    def test_snapshot_temperatures_do_not_trigger_individual_ec_reads(self):
        with mock.patch.object(DAEMON, "read_uint", side_effect=[50000, 4300, 4400]) as read:
            sample = DAEMON.read_runtime_sample(pathlib.Path("/fake/core"),
                pathlib.Path("/fake/wmi"), ec_temperatures=(51, 42))
        self.assertEqual(sample, DAEMON.RuntimeSample(50000, 51, 42, 4300, 4400))
        self.assertEqual(read.call_args_list, [mock.call(pathlib.Path("/fake/core/temp1_input")),
            mock.call(pathlib.Path("/fake/wmi/fan1_input")),
            mock.call(pathlib.Path("/fake/wmi/fan2_input"))])

    def test_snapshot_sentinel_remains_degraded_with_valid_rpm_retained(self):
        with mock.patch.object(DAEMON, "read_uint", side_effect=[50000, 4300, 4400]):
            with self.assertRaises(DAEMON.SensorSampleError) as raised:
                DAEMON.read_runtime_sample(pathlib.Path("/fake/core"),
                    pathlib.Path("/fake/wmi"), ec_temperatures=(255, 42))
        self.assertEqual(raised.exception.partial,
                         DAEMON.RuntimeSample(50000, None, 42, 4300, 4400))
        self.assertEqual(set(raised.exception.channel_errors), {"ec_cpu"})

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


class HardwareFreeMainLoopTests(unittest.TestCase):
    def setUp(self):
        self.real_health_publisher = DAEMON.publish_health
        publisher = mock.patch.object(DAEMON, "publish_health")
        self.health_publisher = publisher.start()
        self.addCleanup(publisher.stop)


class BoostResponseTests(unittest.TestCase):
    def test_large_observation_gap_cannot_prove_continuously_low_response(self):
        response = DAEMON.BoostResponse(0)
        self.assertFalse(response.observe(1200, 1200, 0))
        self.assertFalse(response.observe(1200, 1200, 12))
        self.assertFalse(response.observe(1200, 1200, 13))
        with self.assertRaises(DAEMON.ProfileError):
            response.observe(1200, 1200, 14)
    def test_one_invalid_channel_does_not_hide_a_known_nonresponding_fan(self):
        response = DAEMON.BoostResponse(0)
        for now in range(12):
            self.assertFalse(response.observe(0, None, now))
        with self.assertRaisesRegex(DAEMON.ProfileError, "fan1 did not respond"):
            response.observe(0, None, 12)

    def test_missing_rpm_resets_low_evidence_instead_of_proving_failure(self):
        response = DAEMON.BoostResponse(0)
        self.assertFalse(response.observe(1200, 1200, 0))
        for now in range(1, 100):
            self.assertFalse(response.observe(None, None, now))
        self.assertFalse(response.observe(1200, 1200, 100))
        self.assertFalse(response.observe(1200, 1200, 101))
        self.assertTrue(response.observe(4500, 4600, 102))

    def test_valid_response_floor_does_not_accept_implausible_overspeed(self):
        self.assertFalse(DAEMON.BoostResponse(0).observe(240000, 4500, 12))


class SustainedAlarmMainLoopTests(HardwareFreeMainLoopTests):
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


class SensorHoldMainLoopTests(HardwareFreeMainLoopTests):
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
        verify.assert_not_called()  # Runtime response checks are incremental.
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


class RecoveryHandoffMainLoopTests(HardwareFreeMainLoopTests):
    def run_startup(self, recovery_latched, *, duration=33.0, sample_at=None,
                    event_at=None, suspend_offset_at=None, initial_verified=True,
                    clock_gap_at=None, sensor_delay_at=None, timing_setting=None,
                    control_at=None):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def boottime(self, _clock_id):
                return self.now + (suspend_offset_at(self.now) if suspend_offset_at else 0)

            def sleep(self, seconds):
                self.now += seconds
                if clock_gap_at:
                    self.now += clock_gap_at(self.now)
                if event_at:
                    event_at(self.now, state)
                if self.now >= duration:
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
        state = {"curve": DAEMON.DEFAULT, "mode": "advanced", "boost": "on"}
        writes = []
        preserves = []
        events = []
        self.control_reads = []
        self.ec_arguments = []

        def fake_read(path):
            text = str(path)
            self.control_reads.append(text)
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
            writes.append((clock.now, value))
            state["boost"] = value

        def fake_apply(_wmi, preserve_boost, _heartbeat):
            events.append("apply")
            preserves.append(preserve_boost)
            state["curve"] = DAEMON.DEFAULT
            state["mode"] = "advanced"
            state["boost"] = "on" if preserve_boost else "off"
            return initial_verified

        def fake_sample(_core, _wmi, *, ec_temperatures=None):
            self.ec_arguments.append(ec_temperatures)
            observed_at = clock.now
            try:
                return (sample_at(observed_at) if sample_at else
                        DAEMON.RuntimeSample(50000, 50, 40, 4400, 4400))
            finally:
                if sensor_delay_at:
                    clock.now += sensor_delay_at(observed_at)

        original_stop = DAEMON.STOP
        original_boost_request = DAEMON.BOOST_REQUEST
        original_release_request = DAEMON.RELEASE_REQUEST
        DAEMON.STOP = False
        DAEMON.BOOST_REQUEST = False
        DAEMON.RELEASE_REQUEST = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(DAEMON.os.environ))
                DAEMON.os.environ.pop("MSI_FAN_TIMING_METRICS", None)
                if timing_setting is not None:
                    DAEMON.os.environ["MSI_FAN_TIMING_METRICS"] = timing_setting
                stack.enter_context(
                    mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")))
                stack.enter_context(mock.patch.object(DAEMON, "LOCK", Lock()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "OVERRIDE", MissingOverride()))
                stack.enter_context(
                    mock.patch.object(
                        DAEMON, "recovery_latch_present", return_value=recovery_latched))
                def fake_consume():
                    events.append("consume")
                    return recovery_latched

                consume = stack.enter_context(mock.patch.object(
                    DAEMON, "consume_recovery_latch", side_effect=fake_consume))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read", side_effect=fake_read))
                stack.enter_context(
                    mock.patch.object(DAEMON, "write", side_effect=fake_write))
                def policy(*, monitoring=False, timing=None):
                    if monitoring and control_at:
                        if timing is not None:
                            timing.begin_sensor(clock.now)
                        return control_at(clock.now, state)
                    return None
                stack.enter_context(mock.patch.object(
                    DAEMON, "require_policy_identity", side_effect=policy))
                stack.enter_context(mock.patch.object(
                    DAEMON, "one_hwmon", side_effect=(
                        pathlib.Path("/fake/wmi"), pathlib.Path("/fake/core"))))
                stack.enter_context(mock.patch.object(
                    DAEMON, "force_apply_default", side_effect=fake_apply))
                stack.enter_context(mock.patch.object(
                    DAEMON, "read_runtime_sample", side_effect=fake_sample))
                self.acks = stack.enter_context(mock.patch.object(DAEMON, "acknowledge"))
                stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "release_factory_if_cool", return_value=True))
                stack.enter_context(mock.patch.object(DAEMON, "notify"))
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
                self.stderr = io.StringIO()
                with contextlib.redirect_stderr(self.stderr):
                    self.assertEqual(DAEMON.main(), 0)
        finally:
            DAEMON.STOP = original_stop
            DAEMON.BOOST_REQUEST = original_boost_request
            DAEMON.RELEASE_REQUEST = original_release_request

        return state, writes, preserves, consume, events

    def test_recovery_handoff_becomes_an_automatic_cooldown_latch(self):
        state, writes, preserves, consume, events = self.run_startup(recovery_latched=True)

        self.assertEqual(preserves, [True])
        consume.assert_called_once_with()
        self.assertEqual(events[:2], ["apply", "consume"])
        self.assertEqual(writes, [(30.0, "off")])
        self.assertEqual(state["boost"], "off")

    @staticmethod
    def snapshot_control(now, state):
        return DAEMON.MonitorControl(DAEMON.fan_control.ControlSnapshot(
            "1824EMS1.108", "comfort", state["mode"], state["boost"],
            50, 40, state["curve"]), now)

    def test_snapshot_monitoring_has_no_legacy_curve_mode_or_temperature_reads(self):
        _, writes, *_ = self.run_startup(True, control_at=self.snapshot_control)
        self.assertEqual(writes, [(30.0, "off")])
        self.assertTrue(all(values == (50, 40) for values in self.ec_arguments))
        self.assertFalse(any(path.endswith(("fan_curve", "fan_mode", "fw_version",
                                            "realtime_temperature"))
                             for path in self.control_reads))

    def test_slow_snapshot_is_inside_cooldown_and_health_freshness_envelope(self):
        def control(now, state):
            result = self.snapshot_control(now, state)
            if now == 29:
                DAEMON.time.sleep(3)
            return result
        with mock.patch.object(DAEMON.time, "perf_counter_ns",
                               side_effect=lambda: int(DAEMON.time.monotonic() * 1e9)):
            _, writes, *_ = self.run_startup(True, duration=66, control_at=control,
                                            timing_setting="1")
        self.assertEqual(writes, [(62.0, "off")])
        slow = next(call.args[0] for call in self.health_publisher.call_args_list
                    if "acquisition" in call.args[0]["channel_errors"])
        self.assertEqual(slow["sampled_monotonic_ns"], 29_000_000_000)
        self.assertEqual(slow["last_valid_monotonic_ns"], 28_000_000_000)
        self.assertEqual(slow["timing_metrics"]["last_sensor_read_duration_ms"], 3000)
        self.assertFalse(slow["physical_response_verified"])

    def test_reapply_discards_pretransaction_snapshot_temperatures(self):
        controls = []
        def control(now, state):
            result = self.snapshot_control(now, state)
            if not controls:
                result = DAEMON.MonitorControl(DAEMON.fan_control.ControlSnapshot(
                    "1824EMS1.108", "comfort", "auto", "on", 99, 85,
                    DAEMON.FACTORY), now)
            controls.append(result)
            return result
        self.run_startup(True, duration=2, control_at=control)
        self.assertGreaterEqual(len(controls), 3)
        self.assertEqual(self.ec_arguments, [(50, 40), (50, 40)])

    def test_existing_manual_boost_is_not_released_without_the_recovery_marker(self):
        state, writes, preserves, consume, events = self.run_startup(recovery_latched=False)

        self.assertEqual(preserves, [True])
        consume.assert_not_called()
        self.assertEqual(events, ["apply"])
        self.assertEqual(writes, [])
        self.assertEqual(state["boost"], "on")

    def test_heat_at_29_seconds_requires_a_new_complete_cooldown(self):
        def sample(now):
            return DAEMON.RuntimeSample(90000 if now == 29 else 50000, 50, 40, 4400, 4400)
        state, writes, *_ = self.run_startup(True, duration=62, sample_at=sample)
        self.assertEqual(writes, [(60.0, "off")])
        self.assertEqual(state["boost"], "off")

    def test_invalid_sample_at_29_seconds_resets_cooldown(self):
        def sample(now):
            if now == 29:
                raise DAEMON.SensorSampleError("EC CPU unavailable",
                    DAEMON.RuntimeSample(50000, None, 40, 4400, 4400),
                    {"ec_cpu": "unavailable"})
            return DAEMON.RuntimeSample(50000, 50, 40, 4400, 4400)
        _, writes, *_ = self.run_startup(True, duration=62, sample_at=sample)
        self.assertEqual(writes, [(60.0, "off")])
        degraded = [call.args[0] for call in self.health_publisher.call_args_list
                    if call.args[0]["state"] == "degraded"]
        self.assertTrue(degraded[0]["physical_response_verified"])
        self.assertEqual(degraded[0]["cooldown_elapsed_seconds"], 0)
        self.assertIsNone(degraded[0]["telemetry"]["ec_cpu_celsius"])

    def test_resume_resets_automatic_cooldown_and_preserves_boost(self):
        _, writes, preserves, _, events = self.run_startup(
            True, duration=61, suspend_offset_at=lambda now: 1000 if now >= 29 else 0)
        self.assertEqual(preserves, [True, True])
        self.assertEqual(events.count("apply"), 2)
        self.assertEqual(writes, [(59.0, "off")])

    def test_resume_preserves_manual_boost_without_automatic_release(self):
        state, writes, preserves, *_ = self.run_startup(
            False, duration=65, suspend_offset_at=lambda now: 1000 if now >= 29 else 0)
        self.assertEqual(preserves, [True, True])
        self.assertEqual(writes, [])
        self.assertEqual(state["boost"], "on")

    def test_resume_during_degraded_telemetry_keeps_valid_fan_monitoring(self):
        def sample(now):
            if now < 35:
                raise DAEMON.SensorSampleError("EC CPU unavailable",
                    DAEMON.RuntimeSample(50000, None, 40, 4400, 4400),
                    {"ec_cpu": "unavailable"})
            return DAEMON.RuntimeSample(50000, 50, 40, 4400, 4400)
        _, writes, preserves, *_ = self.run_startup(
            True, duration=67, sample_at=sample,
            suspend_offset_at=lambda now: 1000 if now >= 29 else 0)
        self.assertEqual(preserves, [True, True])
        self.assertEqual(writes, [(65.0, "off")])

    def test_known_stopped_fan_is_detected_despite_invalid_temperature(self):
        def sample(_now):
            raise DAEMON.SensorSampleError("EC CPU unavailable",
                DAEMON.RuntimeSample(50000, None, 40, 0, 4400),
                {"ec_cpu": "unavailable"})
        with self.assertRaisesRegex(DAEMON.ProfileError, "fan1 did not respond"):
            self.run_startup(True, duration=20, sample_at=sample)

    def test_manual_request_cancels_automatic_cooldown(self):
        def event(now, _state):
            if now == 29:
                DAEMON.BOOST_REQUEST = True
        state, writes, *_ = self.run_startup(True, duration=65, event_at=event)
        self.assertEqual(writes, [(29.0, "on")])
        self.assertEqual(state["boost"], "on")
        self.acks.assert_called_once_with("boost-on")
        health = self.health_publisher.call_args.args[0]
        self.assertTrue(health["manual_boost"])
        self.assertFalse(health["automatic_boost"])

    def test_requests_are_processed_while_temperatures_are_degraded(self):
        def sample(_now):
            raise DAEMON.SensorSampleError("EC CPU unavailable",
                DAEMON.RuntimeSample(50000, None, 40, 4400, 4400),
                {"ec_cpu": "unavailable"})
        def event(now, _state):
            if now == 1:
                DAEMON.BOOST_REQUEST = True
            if now == 2:
                DAEMON.RELEASE_REQUEST = True
        state, writes, *_ = self.run_startup(True, duration=35, sample_at=sample, event_at=event)
        self.assertEqual(state["boost"], "on")
        self.assertNotIn("off", [value for _, value in writes])
        self.assertEqual(self.acks.call_args_list,
                         [mock.call("boost-on"), mock.call("boost-release-queued")])
        self.assertTrue(self.health_publisher.call_args.args[0]["release_pending"])

    def test_resume_preserves_queued_manual_release_but_resets_timer(self):
        def event(now, _state):
            if now == 1:
                DAEMON.RELEASE_REQUEST = True
        _, writes, *_ = self.run_startup(
            False, duration=62, event_at=event,
            suspend_offset_at=lambda now: 1000 if now >= 29 else 0)
        self.assertEqual(writes, [(59.0, "off")])
        self.assertEqual(self.acks.call_args_list,
                         [mock.call("boost-release-queued"), mock.call("boost-release")])

    def test_monitoring_gap_below_watchdog_deadline_resets_cooldown(self):
        _, writes, *_ = self.run_startup(
            True, duration=65, clock_gap_at=lambda now: 2 if now == 29 else 0)
        self.assertEqual(writes, [(61.0, "off")])

    def test_slow_current_poll_cannot_release_automatic_boost_at_cooldown_boundary(self):
        for delay in (2.5, 3.0):
            with self.subTest(delay=delay):
                self.health_publisher.reset_mock()
                _, writes, *_ = self.run_startup(
                    True, duration=66,
                    sensor_delay_at=lambda now: delay if now == 29 else 0)
                self.assertEqual(writes, [(59.0 + delay, "off")])
                slow = [call.args[0] for call in self.health_publisher.call_args_list
                        if "acquisition" in call.args[0]["channel_errors"]]
                self.assertEqual(len(slow), 1)
                self.assertEqual(slow[0]["state"], "degraded")
                self.assertTrue(slow[0]["automatic_boost"])
                self.assertFalse(slow[0]["physical_response_verified"])
                self.assertEqual(slow[0]["cooldown_elapsed_seconds"], 0)

    def test_slow_current_poll_resets_queued_manual_cooldown(self):
        def event(now, _state):
            if now == 1:
                DAEMON.RELEASE_REQUEST = True
        _, writes, *_ = self.run_startup(
            False, duration=66, event_at=event,
            sensor_delay_at=lambda now: 3 if now == 29 else 0)
        self.assertEqual(writes, [(62.0, "off")])
        slow = next(call.args[0] for call in self.health_publisher.call_args_list
                    if "acquisition" in call.args[0]["channel_errors"])
        self.assertTrue(slow["manual_boost"])
        self.assertTrue(slow["release_pending"])
        self.assertEqual(slow["cooldown_elapsed_seconds"], 0)
        self.assertEqual(self.acks.call_args_list,
                         [mock.call("boost-release-queued"), mock.call("boost-release")])

    def test_slow_partial_sample_keeps_original_errors_and_honest_observation_age(self):
        published = []
        self.health_publisher.side_effect = lambda payload: published.append(
            (DAEMON.time.monotonic(), payload))
        def sample(now):
            if now == 29:
                raise DAEMON.SensorSampleError(
                    "package unavailable", DAEMON.RuntimeSample(None, 50, 40, 4400, 4400),
                    {"package": "package unavailable"})
            return DAEMON.RuntimeSample(50000, 50, 40, 4400, 4400)
        _, writes, *_ = self.run_startup(
            True, duration=66, sample_at=sample,
            sensor_delay_at=lambda now: 3 if now == 29 else 0)
        self.assertEqual(writes, [(62.0, "off")])
        completed_at, slow = next((now, payload) for now, payload in published
                                  if "acquisition" in payload["channel_errors"])
        self.assertEqual(completed_at, 32)
        self.assertEqual(slow["sampled_monotonic_ns"], 29_000_000_000)
        self.assertEqual(slow["last_valid_monotonic_ns"], 28_000_000_000)
        self.assertEqual(set(slow["channel_errors"]), {"acquisition", "package"})
        self.assertIsNone(slow["telemetry"]["package_millidegrees"])
        self.assertFalse(slow["physical_response_verified"])

    def test_slow_valid_sample_keeps_observation_time_and_does_not_refresh_last_valid(self):
        self.run_startup(True, duration=35,
                         sensor_delay_at=lambda now: 3 if now == 29 else 0)
        slow = next(call.args[0] for call in self.health_publisher.call_args_list
                    if "acquisition" in call.args[0]["channel_errors"])
        self.assertEqual(slow["sampled_monotonic_ns"], 29_000_000_000)
        self.assertEqual(slow["last_valid_monotonic_ns"], 28_000_000_000)
        self.assertEqual(slow["telemetry"]["package_millidegrees"], 50000)
        self.assertFalse(slow["physical_response_verified"])

    def test_slow_low_rpm_sample_resets_nonresponse_evidence(self):
        def sample(now):
            rpm = 1200 if 9 <= now <= 12 else 4400
            return DAEMON.RuntimeSample(50000, 50, 40, rpm, rpm)
        _, writes, *_ = self.run_startup(
            True, duration=48, sample_at=sample,
            sensor_delay_at=lambda now: 3 if now == 12 else 0)
        self.assertEqual(writes, [(45.0, "off")])
        slow = next(call.args[0] for call in self.health_publisher.call_args_list
                    if "acquisition" in call.args[0]["channel_errors"])
        self.assertEqual(slow["state"], "degraded")
        self.assertFalse(slow["physical_response_verified"])

    def test_timing_metrics_unset_or_zero_never_reads_diagnostic_clock(self):
        for setting in (None, "0"):
            with self.subTest(setting=setting):
                self.health_publisher.reset_mock()
                with mock.patch.object(DAEMON.time, "perf_counter_ns",
                                       side_effect=AssertionError("diagnostics disabled")):
                    self.run_startup(True, duration=35, timing_setting=setting,
                                     sensor_delay_at=lambda now: 3 if now == 29 else 0)
                self.assertTrue(all("timing_metrics" not in call.args[0]
                                    for call in self.health_publisher.call_args_list))

    def test_invalid_timing_setting_warns_and_keeps_cooling_without_diagnostic_reads(self):
        with mock.patch.object(DAEMON.time, "perf_counter_ns",
                               side_effect=AssertionError("invalid diagnostics disabled")):
            state, _, *_ = self.run_startup(True, duration=3, timing_setting="unexpected")
        self.assertEqual(state["boost"], "on")
        self.assertIn("invalid MSI_FAN_TIMING_METRICS", self.stderr.getvalue())
        self.assertTrue(all("timing_metrics" not in call.args[0]
                            for call in self.health_publisher.call_args_list))

    def test_enabled_timing_metrics_are_bounded_and_do_not_change_cooling(self):
        with mock.patch.object(DAEMON.time, "perf_counter_ns",
                               side_effect=lambda: int(DAEMON.time.monotonic() * 1_000_000_000)):
            _, writes, *_ = self.run_startup(
                True, duration=66, timing_setting="1",
                sensor_delay_at=lambda now: 3 if now == 29 else 0)
        self.assertEqual(writes, [(62.0, "off")])
        snapshots = [call.args[0]["timing_metrics"]
                     for call in self.health_publisher.call_args_list]
        final = snapshots[-1]
        self.assertEqual(final["schema"], 1)
        self.assertEqual(final["slow_sensor_reads"], 1)
        self.assertEqual(final["deadline_misses"], 1)
        self.assertEqual(final["max_sensor_read_duration_ms"], 3000)
        self.assertEqual(final["max_loop_duration_ms"], 3000)
        self.assertEqual(final["max_observation_gap_seconds"], 3)
        self.assertGreater(final["completed_loops"], 30)
        self.assertGreater(final["sensor_reads"], 30)
        self.assertTrue(all(not isinstance(value, (dict, list)) for value in final.values()))

    def test_timing_metrics_setting_is_read_only_at_startup(self):
        def event(now, _state):
            if now == 1:
                DAEMON.os.environ["MSI_FAN_TIMING_METRICS"] = "1"
        with mock.patch.object(DAEMON.time, "perf_counter_ns",
                               side_effect=AssertionError("startup diagnostics disabled")):
            self.run_startup(True, duration=3, timing_setting="0", event_at=event)
        self.assertTrue(all("timing_metrics" not in call.args[0]
                            for call in self.health_publisher.call_args_list))

    def test_newer_manual_on_cancels_older_unprocessed_release(self):
        def event(now, _state):
            if now == 1:
                DAEMON.on_release(0, None)
                DAEMON.on_boost(0, None)
        state, writes, *_ = self.run_startup(False, duration=65, event_at=event)
        self.assertEqual(writes, [(1.0, "on")])
        self.assertEqual(state["boost"], "on")
        self.acks.assert_called_once_with("boost-on")

    def test_newer_release_after_manual_on_still_queues_cooldown(self):
        def event(now, _state):
            if now == 1:
                DAEMON.on_boost(0, None)
                DAEMON.on_release(0, None)
        _, writes, *_ = self.run_startup(False, duration=35, event_at=event)
        self.assertEqual(writes, [(1.0, "on"), (31.0, "off")])
        self.assertEqual(self.acks.call_args_list,
            [mock.call("boost-release-queued"), mock.call("boost-on"), mock.call("boost-release")])

    def test_real_publisher_and_manager_agree_on_mode_schema_and_nullable_reason(self):
        path = pathlib.Path(__file__).parents[1] / "src/msi-fan-profile"
        loader = importlib.machinery.SourceFileLoader("manager_health_contract", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        manager = importlib.util.module_from_spec(spec)
        loader.exec_module(manager)
        self.run_startup(False)
        payload = self.health_publisher.call_args.args[0]
        self.assertIsNone(payload["reason"])
        original_lstat = pathlib.Path.lstat
        original_fstat = DAEMON.os.fstat

        def virtual_root(info):
            # Exercise actual mode, inode pinning, file IO, and schema as a
            # non-root test user; only ownership is virtualized.
            return types.SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode,
                st_nlink=info.st_nlink, st_dev=info.st_dev, st_ino=info.st_ino)

        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "health.json"
            with mock.patch.object(DAEMON, "HEALTH", target), \
                    mock.patch.object(manager, "HEALTH", target), \
                    mock.patch.object(DAEMON.os, "fchown"), \
                    mock.patch.object(manager.time, "monotonic_ns", return_value=
                                      payload["sampled_monotonic_ns"] + 100_000_000), \
                    mock.patch.object(pathlib.Path, "lstat", lambda p: virtual_root(original_lstat(p))), \
                    mock.patch.object(DAEMON.os, "fstat", lambda fd: virtual_root(original_fstat(fd))):
                self.real_health_publisher(payload)
                health = manager.health_snapshot(payload["pid"])
            self.assertEqual(health["status"], "fresh")
            self.assertEqual(health["state"], "cooling")
            self.assertTrue(health["physical_response_verified"])

    def test_failed_health_publication_does_not_stop_working_cooling(self):
        self.health_publisher.side_effect = OSError("publication unavailable")
        state, _, *_ = self.run_startup(False)
        self.assertEqual(state["boost"], "on")


class HealthPublicationTests(unittest.TestCase):
    def test_snapshot_is_atomic_json_without_leftover_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "health.json"
            with mock.patch.object(DAEMON, "HEALTH", target), \
                    mock.patch.object(DAEMON.os, "fchown"):
                DAEMON.publish_health({"schema": 1, "pid": 123, "state": "degraded"})
            self.assertEqual(json.loads(target.read_text())["state"], "degraded")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [target])

    def test_existing_symlink_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            victim = pathlib.Path(directory) / "victim"
            victim.write_text("unchanged")
            target = pathlib.Path(directory) / "health.json"
            target.symlink_to(victim)
            with mock.patch.object(DAEMON, "HEALTH", target):
                with self.assertRaisesRegex(DAEMON.ProfileError, "unsafe runtime health"):
                    DAEMON.publish_health({"schema": 1})
            self.assertEqual(victim.read_text(), "unchanged")


class RecoveryLatchValidationTests(unittest.TestCase):
    def test_unsafe_recovery_marker_fails_closed_before_opening_it(self):
        unsafe = mock.Mock()
        unsafe.lstat.return_value = types.SimpleNamespace(
            st_mode=DAEMON.stat.S_IFLNK | 0o777, st_uid=0, st_gid=0, st_nlink=1)

        with mock.patch.object(DAEMON, "RECOVERY_LATCH", unsafe), \
                mock.patch.object(DAEMON.os, "open") as open_latch:
            with self.assertRaisesRegex(DAEMON.ProfileError, "unsafe recovery Boost latch"):
                DAEMON.recovery_latch_present()

        open_latch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
