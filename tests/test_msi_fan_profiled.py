#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the daemon's non-hardware thermal state machine."""

from __future__ import annotations

import contextlib
import errno
import gc
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
# Captured before any test relocates it: a keeper handoff leaves a root-owned
# latch here exactly when an operator upgrades, so tests must never read it.
HOST_RECOVERY_LATCH = DAEMON.RECOVERY_LATCH
HOST_PATH_PREFIXES = ("/run/", "/sys/", "/proc/", "/dev/", "/etc/", "/var/")
RELOCATED_PATHS = ("BASE", "LOCK", "OVERRIDE", "ACK", "HEALTH", "RECOVERY_LATCH")


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


class LegacyMonitoringReadTests(unittest.TestCase):
    """The legacy 1 Hz pass relies on the pinned fan_curve read's firmware check."""

    def controls(self, values):
        reads = []

        def fake_read(path):
            reads.append(path.name)
            value = values[path.name]
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")), \
                mock.patch.object(DAEMON, "read", fake_read):
            try:
                return DAEMON.legacy_monitoring_controls(), reads
            except DAEMON.ProfileError as error:
                return error, reads

    def test_curve_is_read_before_every_other_control(self):
        result, reads = self.controls({"fan_curve": DAEMON.DEFAULT, "shift_mode": "comfort",
                                       "fan_mode": "advanced", "cooler_boost": "off"})
        self.assertEqual(result, (DAEMON.DEFAULT, "advanced", "off"))
        self.assertEqual(reads, ["fan_curve", "shift_mode", "fan_mode", "cooler_boost"])

    def test_in_kernel_firmware_mismatch_fails_before_any_other_control(self):
        refused = OSError(errno.EOPNOTSUPP, "Operation not supported", "/fake/ec/fan_curve")
        result, reads = self.controls({"fan_curve": refused})
        self.assertIsInstance(result, DAEMON.ProfileError)
        self.assertRegex(str(result), "validation failed by the in-kernel DMI/EC firmware "
                                      "check: .*Operation not supported")
        self.assertEqual(reads, ["fan_curve"])
        result, _ = self.controls({"fan_curve": OSError(errno.EIO, "I/O error")})
        self.assertRegex(str(result), "^monitoring identity/ABI validation failed: .*I/O error")

    def test_shift_mode_is_still_checked_before_mode_and_boost(self):
        result, reads = self.controls({"fan_curve": DAEMON.DEFAULT, "shift_mode": "turbo"})
        self.assertRegex(str(result), "no longer comfort")
        self.assertEqual(reads, ["fan_curve", "shift_mode"])

    def test_monitoring_identity_opts_in_only_for_the_legacy_pass(self):
        options_seen = []

        def identity(_read, _base, **options):
            options_seen.append(options)
            return {DAEMON.fan_control.SOURCE_PATH: DAEMON.fan_control.LEGACY_SRCVERSION}

        def no_ec_read(path):
            raise AssertionError(f"legacy identity read an EC attribute: {path}")

        with mock.patch.object(DAEMON.fan_control, "identity", identity), \
                mock.patch.object(DAEMON, "read", no_ec_read):
            self.assertIsNone(DAEMON.require_policy_identity(monitoring=True))
        self.assertEqual(options_seen, [{"monitoring": True,
                                         "firmware_checked_by_curve_read": True}])


class HardwareFreeMainLoopTests(unittest.TestCase):
    """Run main() against a private runtime directory, never the live host.

    Every host path constant is relocated beneath a per-test temporary
    directory, and helpers with fixed host paths or sockets fail unless a test
    mocks them. Local test mocks stack on top of these defaults.
    """

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="msi-fan-profiled-test-")
        self.addCleanup(directory.cleanup)
        self.runtime = pathlib.Path(directory.name)
        for name in RELOCATED_PATHS:
            patcher = mock.patch.object(
                DAEMON, name, self.runtime / getattr(DAEMON, name).name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assert_host_paths_relocated()

        self.unmocked_io = []

        def blocked(name):
            def reject(*args, **kwargs):
                self.unmocked_io.append((name, args, kwargs))
                raise AssertionError(f"unmocked host I/O blocked: {name}")
            return reject

        for name in ("acknowledge", "one_hwmon", "notify"):
            patcher = mock.patch.object(DAEMON, name, side_effect=blocked(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual(
            self.unmocked_io, [], "test attempted unmocked host I/O"))
        self.real_health_publisher = DAEMON.publish_health
        publisher = mock.patch.object(DAEMON, "publish_health")
        self.health_publisher = publisher.start()
        self.addCleanup(publisher.stop)

    def assert_host_paths_relocated(self):
        leaked = sorted(
            name for name, value in vars(DAEMON).items()
            if isinstance(value, pathlib.PurePath) and
            str(value).startswith(HOST_PATH_PREFIXES) and
            not value.is_relative_to(self.runtime))
        self.assertEqual(leaked, [], "relocate new host path constants in setUp")


class MainLoopIsolationTests(HardwareFreeMainLoopTests):
    def test_runtime_paths_are_private_and_absent(self):
        for name in RELOCATED_PATHS:
            with self.subTest(name=name):
                path = getattr(DAEMON, name)
                self.assertTrue(path.is_relative_to(self.runtime))
                self.assertFalse(path.exists())
        self.assertFalse(DAEMON.recovery_latch_present())

    def test_new_unrelocated_host_path_constant_is_detected(self):
        with mock.patch.object(DAEMON, "FUTURE_MARKER",
                               pathlib.Path("/run/msi-fan-profile.future"), create=True):
            with self.assertRaisesRegex(AssertionError, "FUTURE_MARKER"):
                self.assert_host_paths_relocated()


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
            if text.endswith("shift_mode"):
                # Legacy monitoring checks it after the curve read.
                return "comfort"
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
            if text.endswith("shift_mode"):
                # Legacy monitoring checks it after the curve read.
                return "comfort"
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
            if text.endswith("shift_mode"):
                # Legacy monitoring checks it after the curve read.
                return "comfort"
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


class HostRecoveryLatchMainLoopTests(SustainedAlarmMainLoopTests,
                                     SensorHoldMainLoopTests):
    """Re-run the main loops that once read the host latch while one is present.

    After a keeper takeover the live /run holds a root-owned latch, and the
    upgrade's release checks must still pass. Only that pathname's stat is
    virtualized; reading it would hit the mocked descriptor and fail.
    """

    def setUp(self):
        super().setUp()
        original_lstat = pathlib.Path.lstat

        def host_latch_present(path):
            if path == HOST_RECOVERY_LATCH:
                return types.SimpleNamespace(
                    st_mode=DAEMON.stat.S_IFREG | 0o600, st_uid=0, st_gid=0,
                    st_nlink=1, st_dev=0, st_ino=0)
            return original_lstat(path)

        patcher = mock.patch.object(pathlib.Path, "lstat", host_latch_present)
        patcher.start()
        self.addCleanup(patcher.stop)


class RecoveryHandoffMainLoopTests(HardwareFreeMainLoopTests):
    def run_startup(self, recovery_latched, *, duration=33.0, sample_at=None,
                    event_at=None, suspend_offset_at=None, initial_verified=True,
                    clock_gap_at=None, sensor_delay_at=None, timing_setting=None,
                    control_at=None, override_at=None, factory_released=True,
                    initial_boost="on", stop_readings_at=None):
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
            # factory-auto may create the marker while the loop is running.
            @staticmethod
            def lstat():
                if override_at is not None and clock.now >= override_at:
                    return types.SimpleNamespace(
                        st_mode=DAEMON.stat.S_IFREG | 0o600, st_uid=0, st_gid=0)
                raise FileNotFoundError

        clock = Clock()
        state = {"curve": DAEMON.DEFAULT, "mode": "advanced", "boost": initial_boost}
        writes = []
        preserves = []
        events = []
        self.control_reads = []
        self.ec_arguments = []
        self.release_samples = []
        self.notifications = []

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
            if text.endswith("shift_mode"):
                # Legacy monitoring checks it after the curve read.
                return "comfort"
            if stop_readings_at is not None:
                # Only the real clean-stop release confirmation reads these.
                package, ec_cpu, ec_gpu = stop_readings_at(clock.now)
                if text == "/fake/core/temp1_input":
                    self.release_samples.append(clock.now)
                    return package
                if text == "/fake/ec/cpu/realtime_temperature":
                    return ec_cpu
                if text == "/fake/ec/gpu/realtime_temperature":
                    return ec_gpu
            raise AssertionError(f"unexpected text read: {text}")

        def fake_write(path, value):
            if stop_readings_at is not None and str(path).endswith("fan_mode"):
                writes.append((clock.now, "fan_mode=" + value))
                state["mode"] = value
                return
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
                self.factory_restore = stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                if stop_readings_at is None:
                    self.factory_release = stack.enter_context(mock.patch.object(
                        DAEMON, "release_factory_if_cool", return_value=factory_released))
                stack.enter_context(mock.patch.object(
                    DAEMON, "notify",
                    side_effect=lambda message: self.notifications.append(
                        (clock.now, message))))
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

    def test_appearing_factory_override_is_a_clean_stop_request(self):
        """factory-auto creates its marker just before it stops the daemon."""
        state, writes, _, _, events = self.run_startup(
            recovery_latched=False, duration=60.0, override_at=5.0)
        self.assertEqual(events, ["apply"])
        self.factory_restore.assert_called_once_with()
        self.factory_release.assert_called_once()
        self.assertIn("factory-auto runtime override pathname appeared; "
                      "treating it as a clean stop request", self.stderr.getvalue())
        self.assertIn("clean stop requested; restoring factory auto", self.stderr.getvalue())
        self.assertNotIn("ProfileError", self.stderr.getvalue())
        # The loop stopped at the marker, long before the run's own deadline.
        last_sample = max(call.args[0]["sampled_monotonic_ns"]
                          for call in self.health_publisher.call_args_list)
        self.assertLess(last_sample, 5_000_000_000)
        self.assertEqual(writes, [])

    def test_appearing_override_while_hot_fails_exactly_like_a_hot_sigterm(self):
        with self.assertRaisesRegex(DAEMON.ProfileError,
                                    "factory restored; Boost release unsafe"):
            self.run_startup(recovery_latched=False, duration=60.0, override_at=5.0,
                             factory_released=False)
        self.factory_restore.assert_called_once_with()
        self.assertIn("treating it as a clean stop request", self.stderr.getvalue())

    HOT_RELEASE = ("90000", "60", "44")
    COOL_RELEASE = ("59000", "60", "44")

    def stop_heartbeats(self, stop_at):
        beats = [at for at, message in self.notifications
                 if "WATCHDOG=1" in message and at >= stop_at]
        return beats, [later - earlier for earlier, later in zip(beats, beats[1:])]

    def test_clean_stop_spike_right_after_the_stop_request_still_releases_when_cool(self):
        """2026-10-11: one sample >=85 C just after the stop no longer refuses."""
        state, writes, *_ = self.run_startup(
            recovery_latched=False, duration=60.0, override_at=5.0,
            stop_readings_at=lambda now: (
                self.HOT_RELEASE if now == 5.0 else self.COOL_RELEASE))
        self.factory_restore.assert_called_once_with()
        # The spike, then six consecutive valid cool samples 0.5 s apart.
        self.assertEqual(self.release_samples, [5.0 + index * 0.5 for index in range(7)])
        self.assertEqual(writes, [(8.0, "fan_mode=auto"), (8.0, "off")])
        self.assertEqual(state["boost"], "off")
        self.assertIn("Cooler Boost release confirmed: 6 consecutive valid cool samples",
                      self.stderr.getvalue())
        self.assertNotIn("ProfileError", self.stderr.getvalue())
        self.assertIn("STOPPING=1", self.notifications[-1][1])

    def test_clean_stop_junk_right_after_the_stop_request_still_releases_when_cool(self):
        # The EC CPU 255 sentinel, an empty package and a non-numeric EC GPU.
        junk = {5.0: ("59000", "255", "44"), 5.5: ("", "60", "44"), 6.0: ("59000", "60", "x")}
        state, writes, *_ = self.run_startup(
            recovery_latched=False, duration=60.0, override_at=5.0,
            stop_readings_at=lambda now: junk.get(now, self.COOL_RELEASE))
        self.assertEqual(writes, [(9.0, "fan_mode=auto"), (9.0, "off")])
        self.assertEqual(state["boost"], "off")

    def test_clean_stop_refuses_release_while_heat_persists(self):
        with self.assertRaisesRegex(DAEMON.ProfileError,
                                    "factory restored; Boost release unsafe"):
            self.run_startup(recovery_latched=False, duration=60.0, override_at=5.0,
                             stop_readings_at=lambda now: self.HOT_RELEASE)
        # Exactly today's outcome: Boost stays on and nothing writes it off.
        self.assertEqual(self.release_samples, [5.0 + index * 0.5 for index in range(16)])
        self.assertIn("WARNING Cooler Boost release refused: no 6 consecutive valid cool "
                      "samples", self.stderr.getvalue())
        self.assertFalse(any("STOPPING=1" in message for _, message in self.notifications))

    def test_clean_stop_keeps_the_watchdog_fed_throughout_the_confirmation(self):
        # The factory-auto marker starts this stop while the unit still runs, so
        # WatchdogSec=5s is enforced; the refused wait lasts longer than that.
        with self.assertRaises(DAEMON.ProfileError):
            self.run_startup(recovery_latched=False, duration=60.0, override_at=5.0,
                             stop_readings_at=lambda now: self.HOT_RELEASE)
        beats, gaps = self.stop_heartbeats(5.0)
        self.assertGreaterEqual(beats[-1] - beats[0], 7.5)
        self.assertLessEqual(max(gaps), 0.5)
        statuses = [message for at, message in self.notifications if at >= 5.0]
        self.assertTrue(statuses[0].startswith("WATCHDOG=1\nSTATUS=Restoring factory auto"))
        self.assertTrue(all("Factory auto restored; confirming cool before Cooler Boost "
                            "release" in message for message in statuses[1:]))
        self.assertEqual(len(statuses), 1 + 16)

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

    def test_legacy_pass_reads_curve_first_and_reuses_the_response_boost_read(self):
        for latched, per_pass in (
                (False, ["fan_curve", "shift_mode", "fan_mode", "cooler_boost",
                         "cooler_boost"]),
                # The latch's reassertion check keeps its own fresh read.
                (True, ["fan_curve", "shift_mode", "fan_mode", "cooler_boost",
                        "cooler_boost", "cooler_boost"])):
            with self.subTest(latched=latched):
                self.health_publisher.reset_mock()
                # Hot enough to hold a latch, below every alarm threshold.
                self.run_startup(latched, duration=20, initial_boost="off",
                                 sample_at=lambda _now: DAEMON.RuntimeSample(
                                     90000, 50, 40, 4400, 4400))
                passes = len(self.health_publisher.call_args_list)
                names = [pathlib.PurePath(path).name for path in self.control_reads
                         if path != "/proc/self/cgroup"]
                # One startup Boost read, then no fw_version read in any pass.
                self.assertEqual(names, ["cooler_boost"] + per_pass * passes)

    def health_at(self, now):
        return next(call.args[0] for call in self.health_publisher.call_args_list
                    if call.args[0]["sampled_monotonic_ns"] == int(now * 1_000_000_000))

    def test_manual_request_keeps_boost_after_the_automatic_cooldown(self):
        def event(now, _state):
            if now == 29:
                DAEMON.BOOST_REQUEST = True
        state, writes, *_ = self.run_startup(True, duration=65, event_at=event)
        # The request adds a manual owner without cancelling the latch. The
        # latch's cooldown then completes without switching Boost off.
        self.assertEqual(writes, [(29.0, "on")])
        self.assertEqual(state["boost"], "on")
        self.acks.assert_called_once_with("boost-on")
        both = self.health_at(40)
        self.assertTrue(both["manual_boost"])
        self.assertTrue(both["automatic_boost"])
        self.assertEqual(both["state"], "cooling")
        health = self.health_publisher.call_args.args[0]
        self.assertTrue(health["manual_boost"])
        self.assertFalse(health["automatic_boost"])
        self.assertIn("automatic Cooler Boost latch cleared after 30-second cooldown; "
                      "manual Boost remains on", self.stderr.getvalue())

    # Regression scenarios B1, B2 and B4 from the Boost ownership audit: an
    # external off must not defeat an automatic latch, whatever manual Boost
    # was held when it began. Each has a control without manual Boost.
    @staticmethod
    def keyboard_off_at(when):
        def event(now, state):
            if now == when:
                state["boost"] = "off"
        return event

    def test_b1_alarm_during_manual_boost_survives_a_keyboard_off_while_hot(self):
        def sample(now):
            package = 99000 if 10 <= now < 25 else 95000 if now < 40 else 50000
            return DAEMON.RuntimeSample(package, 50, 40, 4400, 4400)
        for initial, expected in (
                ("on", [(30.0, "on"), (70.0, "off")]),
                ("off", [(19.0, "on"), (30.0, "on"), (70.0, "off")])):
            with self.subTest(initial_boost=initial):
                self.health_publisher.reset_mock()
                state, writes, *_ = self.run_startup(
                    False, duration=75, sample_at=sample, initial_boost=initial,
                    event_at=self.keyboard_off_at(30))
                self.assertEqual(writes, expected)
                self.assertEqual(state["boost"], "off")
                self.assertIn("Cooler Boost established for alarm", self.stderr.getvalue())
                hot = self.health_at(35)
                self.assertFalse(hot["manual_boost"])
                self.assertTrue(hot["automatic_boost"])
                if initial == "on":
                    self.assertTrue(self.health_at(29)["manual_boost"])
                    self.assertTrue(self.health_at(29)["automatic_boost"])
                    self.assertIn("manual Boost withdrawn", self.stderr.getvalue())

    def test_b2_boost_on_during_automatic_latch_survives_a_keyboard_off_while_hot(self):
        def sample(now):
            return DAEMON.RuntimeSample(95000 if now < 40 else 50000, 50, 40, 4400, 4400)
        for request, expected in (
                (True, [(28.0, "on"), (30.0, "on"), (70.0, "off")]),
                (False, [(30.0, "on"), (70.0, "off")])):
            def event(now, state):
                if request and now == 28:
                    DAEMON.BOOST_REQUEST = True
                self.keyboard_off_at(30)(now, state)
            with self.subTest(request=request):
                self.health_publisher.reset_mock()
                state, writes, *_ = self.run_startup(
                    True, duration=75, sample_at=sample, event_at=event)
                self.assertEqual(writes, expected)
                self.assertEqual(state["boost"], "off")
                self.assertEqual(self.acks.call_args_list,
                                 [mock.call("boost-on")] if request else [])
                if request:
                    self.assertTrue(self.health_at(29)["manual_boost"])
                    self.assertTrue(self.health_at(29)["automatic_boost"])
                after = self.health_at(31)
                self.assertFalse(after["manual_boost"])
                self.assertTrue(after["automatic_boost"])
                self.assertEqual(self.health_at(69)["state"], "cooling")
                self.assertEqual(self.health_at(70)["state"], "normal")

    def test_b4_degraded_telemetry_during_manual_boost_survives_a_keyboard_off(self):
        def sample(now):
            if 10 <= now < 20:
                raise DAEMON.SensorSampleError(
                    "EC CPU temperature sentinel 255C",
                    DAEMON.RuntimeSample(90000, None, 40, 4400, 4400),
                    {"ec_cpu": "EC CPU temperature sentinel 255C"})
            return DAEMON.RuntimeSample(90000 if now < 40 else 50000, 50, 40, 4400, 4400)
        for initial, expected in (
                ("on", [(30.0, "on"), (70.0, "off")]),
                ("off", [(10.0, "on"), (30.0, "on"), (70.0, "off")])):
            with self.subTest(initial_boost=initial):
                self.health_publisher.reset_mock()
                state, writes, *_ = self.run_startup(
                    False, duration=75, sample_at=sample, initial_boost=initial,
                    event_at=self.keyboard_off_at(30))
                self.assertEqual(writes, expected)
                self.assertEqual(state["boost"], "off")
                degraded = self.health_at(15)
                self.assertEqual(degraded["state"], "degraded")
                self.assertTrue(degraded["automatic_boost"])
                self.assertEqual(degraded["manual_boost"], initial == "on")
                self.assertFalse(self.health_at(35)["manual_boost"])
                self.assertTrue(self.health_at(35)["automatic_boost"])

    def test_queued_manual_release_under_automatic_latch_writes_off_once(self):
        def event(now, _state):
            if now == 5:
                DAEMON.BOOST_REQUEST = True
            if now == 10:
                DAEMON.RELEASE_REQUEST = True
        state, writes, *_ = self.run_startup(True, duration=45, event_at=event)
        # The latch's cooldown (from the request at 5) ends first and leaves
        # Boost to the manual owner; the queued release (from 10) then ends it.
        self.assertEqual(writes, [(5.0, "on"), (40.0, "off")])
        self.assertEqual(state["boost"], "off")
        self.assertEqual(self.acks.call_args_list, [
            mock.call("boost-on"), mock.call("boost-release-queued"),
            mock.call("boost-release")])
        for now, automatic in ((34, True), (36, False)):
            health = self.health_at(now)
            self.assertTrue(health["manual_boost"])
            self.assertEqual(health["automatic_boost"], automatic)
            self.assertTrue(health["release_pending"])
        self.assertFalse(self.health_at(40)["manual_boost"])
        self.assertFalse(self.health_at(40)["release_pending"])

    def test_keyboard_off_after_the_automatic_latch_ends_is_a_manual_release(self):
        def event(now, state):
            if now == 5:
                DAEMON.BOOST_REQUEST = True
            self.keyboard_off_at(40)(now, state)
        state, writes, *_ = self.run_startup(True, duration=45, event_at=event)
        # With no automatic latch left, the keyboard remains a manual override.
        self.assertEqual(writes, [(5.0, "on")])
        self.assertEqual(state["boost"], "off")
        self.assertFalse(self.health_at(40)["manual_boost"])
        self.assertFalse(self.health_at(40)["automatic_boost"])

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


class CleanStopReleaseTests(HardwareFreeMainLoopTests):
    """release_factory_if_cool() against a fake EC, clock and heartbeat."""

    COOL = ("59000", "60", "44")
    HOT = ("90000", "60", "44")

    def release(self, readings, *, boost_off_at=None, heartbeat=None, start=0.0,
                stop_requested_at=None):
        """Release at clock time start; stop_requested_at is when SIGTERM arrived."""
        clock = {"now": start}
        state = {"fan_mode": "auto", "fan_curve": DAEMON.FACTORY, "cooler_boost": "on"}
        self.writes = []
        self.samples = []
        self.beats = []

        def fake_read(path):
            text = str(path)
            if text == "/fake/core/temp1_input":
                self.samples.append(clock["now"])
            index = min(len(self.samples), len(readings)) - 1
            for position, suffix in enumerate(("/fake/core/temp1_input",
                                               "/fake/ec/cpu/realtime_temperature",
                                               "/fake/ec/gpu/realtime_temperature")):
                if text == suffix:
                    return readings[index][position]
            return state[path.name]

        def fake_write(path, value):
            self.writes.append((clock["now"], path.name, value))
            state[path.name] = value

        def sleep(seconds):
            clock["now"] += seconds
            if boost_off_at is not None and clock["now"] == boost_off_at:
                state["cooler_boost"] = "off"  # A keyboard toggle during the wait.

        with mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")), \
                mock.patch.object(DAEMON, "STOP_REQUESTED_AT", stop_requested_at), \
                mock.patch.object(DAEMON, "read", side_effect=fake_read), \
                mock.patch.object(DAEMON, "write", side_effect=fake_write), \
                mock.patch.object(DAEMON.time, "monotonic", side_effect=lambda: clock["now"]), \
                mock.patch.object(DAEMON.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = DAEMON.release_factory_if_cool(
                pathlib.Path("/fake/core"), heartbeat or self.beats.append)
        self.stderr = stderr.getvalue()
        self.finished = clock["now"]
        return result, state

    def test_spike_at_the_first_sample_still_releases_once_cool(self):
        released, state = self.release([self.HOT, self.COOL])
        self.assertTrue(released)
        self.assertEqual(self.samples, [index * 0.5 for index in range(7)])
        self.assertEqual(self.writes, [(3.0, "fan_mode", "auto"),
                                       (3.0, "cooler_boost", "off")])
        self.assertEqual(state["cooler_boost"], "off")
        self.assertEqual(len(self.beats), 7)
        self.assertTrue(self.beats[0].startswith(
            "Factory auto restored; confirming cool before Cooler Boost release: 0/6"))

    def test_persistent_heat_keeps_boost_on_and_writes_nothing(self):
        released, state = self.release([self.HOT])
        self.assertFalse(released)
        self.assertEqual(self.writes, [])
        self.assertEqual(state["cooler_boost"], "on")
        self.assertEqual(len(self.samples), 16)
        self.assertIn("WARNING Cooler Boost release refused", self.stderr)

    def test_spikes_every_few_samples_refuse_release(self):
        released, state = self.release([self.COOL, self.COOL, self.COOL, self.HOT] * 10)
        self.assertFalse(released)
        self.assertNotIn("off", [value for *_, value in self.writes])
        self.assertEqual(state["cooler_boost"], "on")

    def test_external_boost_off_during_an_unsafe_wait_is_reasserted_at_once(self):
        released, state = self.release([self.HOT], boost_off_at=1.0)
        self.assertFalse(released)
        self.assertEqual(self.writes, [(1.0, "cooler_boost", "on")])
        self.assertEqual(state["cooler_boost"], "on")

    def test_failed_heartbeat_refuses_release_and_keeps_boost_on(self):
        def heartbeat(_status):
            raise DAEMON.ProfileError("NOTIFY_SOCKET is absent")

        released, state = self.release([self.COOL], heartbeat=heartbeat)
        self.assertFalse(released)
        self.assertEqual(self.writes, [(0.0, "cooler_boost", "on")])
        self.assertEqual(state["cooler_boost"], "on")

    def test_stop_timeout_constants_match_the_unit(self):
        unit = (pathlib.Path(__file__).parents[1] / "systemd/msi-fan-profile.service").read_text()
        self.assertIn(f"\nTimeoutStopSec={DAEMON.STOP_TIMEOUT_SECONDS:.0f}s\n", unit)
        self.assertEqual(DAEMON.STOP_EXIT_MARGIN_SECONDS, 5.0)

    def test_stop_signal_records_when_the_stop_timer_started(self):
        original = DAEMON.STOP, DAEMON.STOP_REQUESTED_AT
        try:
            DAEMON.STOP, DAEMON.STOP_REQUESTED_AT = False, None
            with mock.patch.object(DAEMON.time, "monotonic", side_effect=[4.0, 9.0]):
                DAEMON.on_stop(DAEMON.signal.SIGTERM, None)
                # A later signal, such as a second stop request, keeps the first time.
                DAEMON.on_stop(DAEMON.signal.SIGINT, None)
            self.assertEqual((DAEMON.STOP, DAEMON.STOP_REQUESTED_AT), (True, 4.0))
        finally:
            DAEMON.STOP, DAEMON.STOP_REQUESTED_AT = original

    def test_a_stop_without_a_signal_or_early_in_the_timeout_gets_the_full_window(self):
        # The factory-auto marker starts the stop before SIGTERM arms the timer.
        for stop_requested_at in (None, 0.0):
            with self.subTest(stop_requested_at=stop_requested_at):
                released, _ = self.release([self.HOT, self.COOL], start=1.5,
                                           stop_requested_at=stop_requested_at)
                self.assertTrue(released)
                self.assertEqual(self.finished, 4.5)
                released, _ = self.release([self.HOT], start=1.5,
                                           stop_requested_at=stop_requested_at)
                self.assertFalse(released)
                self.assertEqual(self.samples[-1], 1.5 + 7.5)

    def test_a_stop_during_startup_never_confirms_past_the_stop_timeout(self):
        # SIGTERM at 0 s during startup; the hwmon wait (10 s) and the Boost
        # verification (12 s) then ran before the loop exit and the restore.
        # Only 3 s of the 30 s timeout remain before the 5 s exit margin.
        released, state = self.release([self.HOT], start=22.0, stop_requested_at=0.0)
        self.assertFalse(released)
        self.assertEqual(self.samples, [22.0, 22.5])
        self.assertLessEqual(self.finished, 25.0)
        self.assertEqual(state["cooler_boost"], "on")
        # A cool streak that can still finish by 25 s releases; a later one,
        # which the full window would wait for, can no longer run past the margin.
        released, _ = self.release([self.HOT, self.COOL], start=22.0, stop_requested_at=0.0)
        self.assertTrue(released)
        self.assertEqual(self.finished, 25.0)
        released, state = self.release([self.HOT, self.HOT, self.COOL], start=22.0,
                                       stop_requested_at=0.0)
        self.assertFalse(released)
        self.assertEqual(self.samples, [22.0, 22.5])
        self.assertEqual(state["cooler_boost"], "on")
        # A machine that is cool from the first sample still releases in time.
        released, state = self.release([self.COOL], start=22.0, stop_requested_at=0.0)
        self.assertTrue(released)
        self.assertEqual(self.finished, 24.5)
        self.assertEqual(state["cooler_boost"], "off")

    def test_too_little_stop_timeout_for_one_window_refuses_without_sampling(self):
        for start in (22.6, 25.0, 40.0):
            with self.subTest(start=start):
                released, state = self.release([self.COOL], start=start, stop_requested_at=0.0)
                self.assertFalse(released)
                self.assertEqual((self.samples, self.writes, self.beats), ([], [], []))
                self.assertEqual(state["cooler_boost"], "on")
                self.assertIn("WARNING Cooler Boost release refused: ", self.stderr)
                self.assertIn(" s of the 30 s stop timeout remain for the cool confirmation, "
                              "which needs 2.5 s", self.stderr)


class StartupGuardMainTests(HardwareFreeMainLoopTests):
    """Startup before READY: exact identity first, then bounded hwmon discovery."""

    KERNEL = "7.0.0-99-generic"

    def run_entrypoint(self, *, driver_loaded=True, hwmon=(), real_identity=True):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                sleeps.append(seconds)
                self.now += seconds

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
        sleeps = []
        writes = []
        notifications = []
        host = {
            "/proc/self/cgroup": "0::/system.slice/msi-fan-profile.service",
            "/sys/class/dmi/id/product_name": "Raider 18 HX AI A2XWIG",
            "/sys/class/dmi/id/board_name": "MS-1824",
            "/sys/class/dmi/id/bios_version": "E1824IMS.310",
            "/sys/module/msi_ec/version": "0.13.1",
            "/sys/module/msi_ec/srcversion": DAEMON.fan_control.LEGACY_SRCVERSION,
            "/fake/ec/fw_version": DAEMON.fan_control.FIRMWARE,
            "/fake/ec/shift_mode": "comfort",
            "/fake/ec/cooler_boost": "off",
        }

        def fake_read(path):
            text = str(path)
            if text == "/sys/module/msi_ec/version" and not driver_loaded:
                raise FileNotFoundError(2, "No such file or directory", text)
            return host[text]

        def fake_apply(_wmi, _preserve_boost, _heartbeat):
            # Stop after the startup transaction; the loop is covered elsewhere.
            DAEMON.STOP = True
            return True

        original_stop = DAEMON.STOP
        DAEMON.STOP = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(DAEMON, "BASE", pathlib.Path("/fake/ec")))
                stack.enter_context(mock.patch.object(DAEMON, "LOCK", Lock()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "OVERRIDE", MissingOverride()))
                stack.enter_context(
                    mock.patch.object(DAEMON, "read", side_effect=fake_read))
                stack.enter_context(mock.patch.object(
                    DAEMON, "write", side_effect=lambda path, value: writes.append(
                        (path, value))))
                stack.enter_context(mock.patch.object(
                    DAEMON.pathlib.Path, "exists", return_value=False))
                stack.enter_context(mock.patch.object(
                    DAEMON.fan_control.os, "uname",
                    return_value=types.SimpleNamespace(release=self.KERNEL)))
                if not real_identity:
                    stack.enter_context(mock.patch.object(DAEMON, "require_policy_identity"))
                self.discovery = stack.enter_context(mock.patch.object(
                    DAEMON, "one_hwmon", side_effect=hwmon))
                self.apply = stack.enter_context(mock.patch.object(
                    DAEMON, "force_apply_default", side_effect=fake_apply))
                stack.enter_context(mock.patch.object(
                    DAEMON, "recovery_latch_present", return_value=False))
                stack.enter_context(mock.patch.object(
                    DAEMON, "restore_factory_envelope", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "release_factory_if_cool", return_value=True))
                stack.enter_context(mock.patch.object(
                    DAEMON, "notify", side_effect=notifications.append))
                stack.enter_context(mock.patch.object(DAEMON.signal, "signal"))
                stack.enter_context(mock.patch.object(DAEMON.os, "geteuid", return_value=0))
                stack.enter_context(mock.patch.object(DAEMON.os, "open", return_value=9))
                stack.enter_context(mock.patch.object(DAEMON.os, "close"))
                stack.enter_context(mock.patch.object(DAEMON.fcntl, "flock"))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "monotonic", side_effect=clock.monotonic))
                stack.enter_context(mock.patch.object(
                    DAEMON.time, "sleep", side_effect=clock.sleep))
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = DAEMON.entrypoint()
        finally:
            DAEMON.STOP = original_stop
        return result, stderr.getvalue(), writes, notifications, sleeps

    def test_missing_driver_fails_at_identity_with_no_ec_write(self):
        result, stderr, writes, notifications, _ = self.run_entrypoint(
            driver_loaded=False)
        self.assertEqual(result, 1)
        self.assertIn("ERROR ProfileError: immutable hardware/driver/EC identity "
                      f"validation failed: msi_ec driver not loaded for kernel {self.KERNEL}",
                      stderr)
        self.assertNotIn("Errno", stderr)
        self.assertEqual(writes, [])
        self.assertEqual(notifications, [])
        self.discovery.assert_not_called()
        self.apply.assert_not_called()

    def test_late_hwmon_registration_is_awaited_before_the_first_ec_write(self):
        missing = DAEMON.ProfileError("expected one msi_wmi_platform hwmon; found 0")
        result, stderr, writes, notifications, sleeps = self.run_entrypoint(
            hwmon=(missing,) * 4 + (pathlib.Path("/fake/wmi"), pathlib.Path("/fake/core")),
            real_identity=False)
        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [DAEMON.HWMON_STARTUP_POLL_SECONDS] * 4)
        self.assertEqual([call.args for call in self.discovery.call_args_list],
                         [("msi_wmi_platform",)] * 5 + [("coretemp",)])
        self.apply.assert_called_once()
        self.assertEqual(self.apply.call_args.args[0], pathlib.Path("/fake/wmi"))
        waiting = [item for item in notifications if "Waiting for msi_wmi_platform" in item]
        self.assertEqual(len(waiting), 4)
        self.assertTrue(all(item.startswith("WATCHDOG=1\n") for item in waiting))
        self.assertEqual(stderr.count("waiting up to 10s for registration"), 1)
        # The mocked clean stop is the only remaining writer; none ran early.
        self.assertEqual(writes, [])

    def test_hwmon_wait_is_bounded_and_then_fails_as_before(self):
        missing = DAEMON.ProfileError("expected one coretemp hwmon; found 0")
        result, stderr, writes, notifications, sleeps = self.run_entrypoint(
            hwmon=(pathlib.Path("/fake/wmi"),) + (missing,) * 40, real_identity=False)
        self.assertEqual(result, 1)
        self.assertLessEqual(DAEMON.HWMON_STARTUP_WAIT_SECONDS, 10.0)
        self.assertEqual(sum(sleeps), DAEMON.HWMON_STARTUP_WAIT_SECONDS)
        self.assertEqual(self.discovery.call_count, 1 + 21)
        self.assertTrue(stderr.endswith(
            "ERROR ProfileError: expected one coretemp hwmon; found 0\n"))
        self.assertFalse(any(item.startswith("READY=1") for item in notifications))
        self.apply.assert_not_called()
        self.assertEqual(writes, [])

    def test_duplicate_hwmon_is_never_accepted_while_waiting(self):
        duplicate = DAEMON.ProfileError("expected one msi_wmi_platform hwmon; found 2")
        result, stderr, writes, _, _ = self.run_entrypoint(
            hwmon=(duplicate,) * 21, real_identity=False)
        self.assertEqual(result, 1)
        self.assertIn("found 2", stderr)
        self.apply.assert_not_called()
        self.assertEqual(writes, [])


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

    def test_failed_snapshot_removes_its_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "health.json"
            with mock.patch.object(DAEMON, "HEALTH", target), \
                    mock.patch.object(DAEMON.os, "fchown"):
                with self.assertRaises(ValueError):
                    DAEMON.publish_health({"schema": 1, "cooldown": float("nan")})
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_repeated_publication_retains_no_memory_per_snapshot(self):
        # The daemon publishes once per second for days under MemoryMax=64M.
        # Python 3.12 pathlib interns path components as immortal strings, so a
        # Path built from each random temporary name leaked one block per call.
        publications = 5000
        original_lstat = pathlib.Path.lstat

        def virtual_root(path):
            # Replace the previous root-owned snapshot as production does.
            info = original_lstat(path)
            return types.SimpleNamespace(st_uid=0, st_gid=0, st_mode=info.st_mode,
                                         st_nlink=info.st_nlink)

        def publish(count):
            for _ in range(count):
                DAEMON.publish_health({"schema": 1, "pid": 123, "state": "normal"})

        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "health.json"
            # Plain functions, not mocks: a Mock records every call and would
            # itself grow by far more than the leak being measured.
            with mock.patch.object(DAEMON, "HEALTH", target), \
                    mock.patch.object(DAEMON.os, "fchown", lambda _fd, _uid, _gid: None), \
                    mock.patch.object(pathlib.Path, "lstat", virtual_root):
                # Let interpreter free lists and caches reach steady state first.
                publish(1000)
                gc.collect()
                before = sys.getallocatedblocks()
                publish(publications)
                gc.collect()
                growth = sys.getallocatedblocks() - before
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [target])
        self.assertLess(growth, publications // 10)


class RootOwnedPath(pathlib.PosixPath):
    """A private temporary path whose metadata reports root ownership."""

    @staticmethod
    def as_root(info):
        return types.SimpleNamespace(
            st_mode=info.st_mode, st_uid=0, st_gid=0, st_nlink=info.st_nlink,
            st_dev=info.st_dev, st_ino=info.st_ino)

    def lstat(self):
        return self.as_root(DAEMON.os.lstat(self))

    def stat(self, *, follow_symlinks=True):
        return self.as_root(DAEMON.os.stat(self, follow_symlinks=follow_symlinks))


class MainLoopMemoryTests(HardwareFreeMainLoopTests):
    """Bound allocator growth across the whole 1 Hz loop, not only publication.

    The real main() runs thousands of passes against a private file-backed
    sysfs tree: real control reads, identity-guarded writes with readback,
    sampling, timing metrics and health publication, through a repeating
    cycle of a manual request and queued release, a sustained alarm and its
    automatic latch, a keyboard off, degraded telemetry, a resume reapply and
    an observation gap. Only the clock, notification, acknowledgement, hwmon
    discovery, host identity and root ownership are virtual. Every fake is a
    plain function: a Mock records each call and would itself grow by far
    more than the leak being measured.
    """

    # One-time allocator and cache growth settles over thousands of passes;
    # after 1000 it still measured up to ~450 blocks, unrelated to PASSES.
    WARMUP = 5000
    PASSES = 4000
    CYCLE = 120

    def test_whole_main_loop_retains_no_memory_per_pass(self):
        ec, wmi, core = DAEMON.BASE, self.runtime / "wmi", self.runtime / "core"
        for path, value in {
            ec / "fw_version": DAEMON.fan_control.FIRMWARE,
            ec / "shift_mode": "comfort",
            ec / "fan_curve": DAEMON.DEFAULT,
            ec / "fan_mode": "advanced",
            ec / "cooler_boost": "off",
            ec / "cpu/realtime_temperature": "50",
            ec / "gpu/realtime_temperature": "40",
            wmi / "fan1_input": "4400",
            wmi / "fan2_input": "4400",
            core / "temp1_input": "50000",
        }.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        lock = RootOwnedPath(DAEMON.LOCK)
        lock.touch()
        lock.chmod(0o600)
        private = str(self.runtime) + "/"
        cgroup = pathlib.Path("/proc/self/cgroup")
        real_read = DAEMON.read
        real_open = DAEMON.os.open
        legacy = {DAEMON.fan_control.SOURCE_PATH: DAEMON.fan_control.LEGACY_SRCVERSION}
        clock = {"now": 0.0, "offset": 0.0, "passes": 0}
        measured = {}
        lock_descriptors = []
        states = dict.fromkeys(("normal", "cooling", "degraded"), 0)

        def private_read(path):
            if path == cgroup:
                return "0::/system.slice/msi-fan-profile.service"
            if not str(path).startswith(private):
                raise AssertionError(f"host read blocked: {path}")
            return real_read(path)

        def host_identity(_read, _base):
            return dict(legacy)

        def lock_tracking_open(path, flags, mode=0o777, *, dir_fd=None):
            # main() keeps its lock descriptor open until the process exits.
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path is lock:
                lock_descriptors.append(descriptor)
            return descriptor

        def publish(payload):
            states[payload["state"]] += 1
            self.real_health_publisher(payload)

        def scenario(phase):
            if phase == 20:
                DAEMON.on_boost(0, None)
            elif phase == 25:
                DAEMON.on_release(0, None)
            elif phase == 30:
                (core / "temp1_input").write_text("99000\n")
            elif phase == 56:
                (core / "temp1_input").write_text("90000\n")
            elif phase == 60:
                (ec / "cooler_boost").write_text("off\n")
            elif phase == 62:
                (ec / "cpu/realtime_temperature").write_text("255\n")
            elif phase == 70:
                (ec / "cpu/realtime_temperature").write_text("50\n")
                (core / "temp1_input").write_text("50000\n")
            elif phase == 110:
                clock["offset"] += 100.0
            elif phase == 115:
                clock["now"] += 3.0

        def sleep(seconds):
            clock["now"] += seconds
            clock["passes"] += 1
            if clock["passes"] == self.WARMUP:
                gc.collect()
                measured["before"] = sys.getallocatedblocks()
            elif clock["passes"] == self.WARMUP + self.PASSES:
                gc.collect()
                measured["after"] = sys.getallocatedblocks()
                DAEMON.STOP = True
            scenario(clock["passes"] % self.CYCLE)

        original_stop = DAEMON.STOP
        original_boost_request = DAEMON.BOOST_REQUEST
        original_release_request = DAEMON.RELEASE_REQUEST
        DAEMON.STOP = False
        DAEMON.BOOST_REQUEST = False
        DAEMON.RELEASE_REQUEST = False
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(
                    DAEMON.os.environ, {"MSI_FAN_TIMING_METRICS": "1"}))
                for name, value in (
                        ("read", private_read), ("LOCK", lock),
                        ("HEALTH", RootOwnedPath(DAEMON.HEALTH)),
                        ("publish_health", publish),
                        ("one_hwmon", lambda name: wmi if name == "msi_wmi_platform" else core),
                        ("notify", lambda _message: None),
                        ("acknowledge", lambda _action: None)):
                    stack.enter_context(mock.patch.object(DAEMON, name, value))
                stack.enter_context(mock.patch.object(
                    DAEMON.fan_control, "host_identity", host_identity))
                for name, value in (("geteuid", lambda: 0),
                                    ("fchown", lambda _fd, _uid, _gid: None),
                                    ("open", lock_tracking_open)):
                    stack.enter_context(mock.patch.object(DAEMON.os, name, value))
                for name, value in (
                        ("monotonic", lambda: clock["now"]),
                        ("clock_gettime", lambda _clock: clock["now"] + clock["offset"]),
                        ("sleep", sleep)):
                    stack.enter_context(mock.patch.object(DAEMON.time, name, value))
                sink = stack.enter_context(open(DAEMON.os.devnull, "w", encoding="ascii"))
                stack.enter_context(contextlib.redirect_stderr(sink))
                self.assertEqual(DAEMON.main(), 0)
        finally:
            for descriptor in lock_descriptors:
                DAEMON.os.close(descriptor)
            DAEMON.STOP = original_stop
            DAEMON.BOOST_REQUEST = original_boost_request
            DAEMON.RELEASE_REQUEST = original_release_request

        self.assertEqual(len(lock_descriptors), 1)
        self.assertTrue(all(count > self.PASSES // 20 for count in states.values()), states)
        self.assertEqual((ec / "cooler_boost").read_text(), "off\n")
        self.assertLess(measured["after"] - measured["before"], self.PASSES // 10)


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
