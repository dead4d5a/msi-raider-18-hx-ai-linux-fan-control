#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for runtime full-cooling recovery."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
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
REAL_RECOVERY_WRITE = RECOVER.write


class HardwareIsolatedTestCase(unittest.TestCase):
    """Fail before unmocked recovery I/O, even when release checks run as root.

    Local test mocks override these guards. Tests exercising the real write
    identity check opt in to REAL_RECOVERY_WRITE and still mock Path.open.
    Caught best-effort exceptions cannot conceal a missing mock: cleanup fails
    if any unmocked I/O was attempted.
    """

    def setUp(self):
        super().setUp()
        self.unmocked_io = []

        def blocked(name):
            def reject(*args, **kwargs):
                self.unmocked_io.append((name, args, kwargs))
                raise AssertionError(f"unmocked recovery I/O blocked: {name}")
            return reject

        for name in ("read", "write", "one_hwmon", "notify", "mark_recovery_latch",
                     "recovery_latch_present", "acquire_lock"):
            patcher = mock.patch.object(RECOVER, name, side_effect=blocked(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        # Defense in depth for the real write function and future direct opens.
        patcher = mock.patch.object(pathlib.Path, "open", side_effect=blocked("Path.open"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertEqual(
            self.unmocked_io, [], "test attempted unmocked recovery I/O"))


class FullCoolingRecoveryTests(HardwareIsolatedTestCase):
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
                mock.patch.object(RECOVER, "verify_boost", side_effect=fake_verify), \
                mock.patch.object(RECOVER, "mark_recovery_latch") as latch:
            self.assertTrue(RECOVER.attempt_full_cooling("candidate14"))

        latch.assert_called_once_with("candidate14")
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
                mock.patch.object(RECOVER, "request_boost_best_effort"), \
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
                mock.patch.object(RECOVER, "verify_boost", return_value=False), \
                mock.patch.object(RECOVER, "mark_recovery_latch") as latch:
            self.assertFalse(RECOVER.attempt_full_cooling("candidate14"))

        latch.assert_called_once_with("candidate14")
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
                    RECOVER, "one_hwmon", side_effect=RuntimeError("WMI absent")), \
                mock.patch.object(RECOVER, "mark_recovery_latch") as latch:
            self.assertFalse(RECOVER.attempt_full_cooling("candidate14"))

        latch.assert_called_once_with("candidate14")
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
                    RECOVER, "recovery_identity", return_value={
                        RECOVER.fan_control.SOURCE_PATH: RECOVER.fan_control.LEGACY_SRCVERSION}):
            self.assertEqual(
                RECOVER.keeper_repair("candidate14"), "Cooler Boost reasserted")

        self.assertEqual(writes, [("cooler_boost", "on")])
        self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
        self.assertEqual(state["fan_mode"], "advanced")
        self.assertEqual(state["cooler_boost"], "on")

    def test_failed_curve_staging_never_marks_a_recovery_handoff(self):
        with mock.patch.object(
                RECOVER, "stage_full_cooling", side_effect=RuntimeError("stage failed")), \
                mock.patch.object(RECOVER, "mark_recovery_latch") as latch, \
                mock.patch.object(RECOVER, "request_boost_best_effort"):
            self.assertFalse(RECOVER.attempt_full_cooling("candidate14"))

        latch.assert_not_called()

    def test_keeper_marks_the_handoff_after_initial_full_cooling(self):
        calls = []
        original_stop = RECOVER.STOP
        RECOVER.STOP = False

        def repair(_profile):
            RECOVER.STOP = True
            return "Candidate14/advanced with Cooler Boost on"

        try:
            with mock.patch.object(
                    RECOVER, "stage_full_cooling",
                    side_effect=lambda _profile: calls.append("stage")), \
                    mock.patch.object(
                        RECOVER, "mark_recovery_latch",
                        side_effect=lambda _profile: calls.append("latch")), \
                    mock.patch.object(RECOVER, "keeper_heartbeat"), \
                    mock.patch.object(RECOVER, "notify"), \
                    mock.patch.object(RECOVER, "keeper_repair", side_effect=repair), \
                    mock.patch.object(
                        RECOVER, "one_hwmon", side_effect=RuntimeError("WMI absent")), \
                    mock.patch.dict(RECOVER.os.environ, {"MSI_FAN_TIMING_METRICS": "0"}), \
                    mock.patch.object(RECOVER.time, "sleep"):
                self.assertEqual(RECOVER.run_keeper("candidate14"), 0)
        finally:
            RECOVER.STOP = original_stop

        self.assertEqual(calls, ["stage", "latch"])


class FanResponseTests(HardwareIsolatedTestCase):
    def test_response_floor_is_distinct_from_invalid_and_low_rpm(self):
        cases = (
            ((3000, 3000), "verified"),
            ((9500, 10000), "verified"),
            ((2999, 5000), "low"),
            ((5000, 0), "low"),
            ((65535, 5000), "invalid"),
            ((5000, 10001), "invalid"),
        )
        for values, wanted in cases:
            with self.subTest(values=values), \
                    mock.patch.object(RECOVER, "read_uint", side_effect=values):
                response = RECOVER.read_fan_response(pathlib.Path("/fake/wmi"))
                self.assertEqual(response.classification, wanted)
                self.assertEqual(response.verified, wanted == "verified")
                self.assertNotIn("maximum", response.status)

    def test_nonnumeric_and_unavailable_telemetry_are_not_low_response(self):
        for error, wanted in (
                (RuntimeError("nonnumeric RPM"), "invalid"),
                (OSError("WMI channel disappeared"), "unavailable")):
            with self.subTest(wanted=wanted), \
                    mock.patch.object(RECOVER, "read_uint", side_effect=error):
                response = RECOVER.read_fan_response(pathlib.Path("/fake/wmi"))
                self.assertEqual(response.classification, wanted)
                self.assertFalse(response.verified)

    def test_low_response_allowance_uses_elapsed_time_and_invalid_resets_it(self):
        verifier = RECOVER.KeeperFanVerification()
        low = RECOVER.FanResponse("low", "1000/1000 RPM")
        invalid = RECOVER.FanResponse("invalid", "65535/65535 RPM")
        status, report = verifier.observe(low, 50)
        self.assertTrue(report)
        self.assertIn("awaiting fan response", status)
        status, report = verifier.observe(low, 61.99)
        self.assertFalse(report)
        self.assertIn("awaiting fan response", status)
        status, report = verifier.observe(low, 62)
        self.assertTrue(report)
        self.assertIn("below response floor for 12s", status)
        verifier.observe(invalid, 63)
        status, _ = verifier.observe(low, 64)
        self.assertIn("awaiting fan response for 0s", status)

    def test_invalid_response_is_rate_limited_and_recovery_clears_age(self):
        verifier = RECOVER.KeeperFanVerification()
        invalid = RECOVER.FanResponse("invalid", "65535/65535 RPM")
        good = RECOVER.FanResponse("verified", "5000/5000 RPM")
        self.assertTrue(verifier.observe(invalid, 10)[1])
        self.assertFalse(verifier.observe(invalid, 69.99)[1])
        status, report = verifier.observe(invalid, 70)
        self.assertTrue(report)
        self.assertIn("unverified for 60s", status)
        status, report = verifier.observe(good, 71)
        self.assertTrue(report)
        self.assertIn("fan response verified", status)
        self.assertNotIn("unverified", status)
        self.assertIsNone(verifier.unverified_since)
        status, _ = verifier.observe(invalid, 72)
        self.assertIn("unverified for 0s", status)

    def test_one_shot_verification_still_has_a_bounded_retry_budget(self):
        with mock.patch.object(RECOVER, "read_uint", return_value=1000), \
                mock.patch.object(RECOVER, "request_boost_best_effort") as boosting, \
                mock.patch.object(RECOVER.time, "sleep") as sleeping:
            self.assertFalse(RECOVER.verify_boost(pathlib.Path("/fake/wmi")))
        self.assertEqual(sleeping.call_count, 12)
        self.assertEqual(boosting.call_count, 13)
        self.assertTrue(all(call.args == (1,) for call in sleeping.call_args_list))

    def test_one_shot_reasserts_external_boost_off_during_each_retry(self):
        for rpm in (1000, 65535):
            with self.subTest(rpm=rpm):
                state = {"boost": "on"}
                requests = []

                def read(_path):
                    return state["boost"]

                def write(_path, value):
                    requests.append(value)
                    state["boost"] = value

                def sample(_path):
                    self.assertEqual(state["boost"], "on")
                    return rpm

                def sleep(_seconds):
                    state["boost"] = "off"

                with mock.patch.object(RECOVER, "read", side_effect=read), \
                        mock.patch.object(RECOVER, "write", side_effect=write), \
                        mock.patch.object(RECOVER, "read_uint", side_effect=sample), \
                        mock.patch.object(RECOVER.time, "sleep", side_effect=sleep):
                    self.assertFalse(RECOVER.verify_boost(pathlib.Path("/fake/wmi")))
                self.assertEqual(requests, ["on"] * 12)
                self.assertEqual(state["boost"], "on")

    def test_best_effort_reassertion_does_not_bypass_write_identity_guard(self):
        with mock.patch.object(RECOVER, "read", return_value="off"), \
                mock.patch.object(RECOVER, "write", side_effect=REAL_RECOVERY_WRITE), \
                mock.patch.object(RECOVER, "identity_safe_for_curve_write", return_value=False), \
                mock.patch.object(pathlib.Path, "open") as opened, \
                mock.patch.object(RECOVER.sys, "stderr", io.StringIO()):
            RECOVER.request_boost_best_effort()
        opened.assert_not_called()

    def test_missing_best_effort_mock_is_blocked_before_any_root_hardware_access(self):
        """The suite guard catches the new fallback even if a test omits its mock."""
        with mock.patch.object(RECOVER.os, "geteuid", return_value=0), \
                mock.patch.object(RECOVER, "read_uint", return_value=1000), \
                mock.patch.object(RECOVER.time, "sleep"), \
                mock.patch.object(pathlib.Path, "open") as opened, \
                mock.patch.object(RECOVER.sys, "stderr", io.StringIO()):
            self.assertFalse(RECOVER.verify_boost(pathlib.Path("/fake/wmi")))
            with self.assertRaisesRegex(AssertionError, "unmocked recovery I/O blocked: write"):
                RECOVER.write(RECOVER.BASE / "cooler_boost", "on")
        opened.assert_not_called()
        self.assertEqual([(name, args) for name, args, _kwargs in self.unmocked_io],
                         [("read", (RECOVER.BASE / "cooler_boost",))] * 13 +
                         [("write", (RECOVER.BASE / "cooler_boost", "on"))])
        # These exact blocked attempts are the subject of this regression only.
        self.unmocked_io.clear()


class IncrementalKeeperTests(HardwareIsolatedTestCase):
    def run_keeper_cycles(self, rpm, unavailable=False, *, duration=14,
                          resume_at=None, timing_setting="0", rpm_delay=0.0):
        """Mock sysfs and clock; switch Boost off after every repair cycle."""
        clock = {"now": 0.0, "offset": 0.0}
        state = {
            "fan_mode": "advanced",
            "fan_curve": RECOVER.DEFAULT,
            "cooler_boost": "on",
        }
        writes = []
        sleeps = []
        heartbeats = []
        stderr = io.StringIO()
        original_stop = RECOVER.STOP
        RECOVER.STOP = False

        def fake_write(path, value):
            writes.append((clock["now"], path.name, value))
            state[path.name] = value

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
            if clock["now"] >= duration:
                RECOVER.STOP = True
            else:
                state["cooler_boost"] = "off"

        def fake_rpm(_path):
            value = rpm(clock["now"]) if callable(rpm) else rpm
            clock["now"] += rpm_delay
            if clock["now"] >= duration:
                RECOVER.STOP = True
            if isinstance(value, Exception):
                raise value
            return value

        def boottime(_clock_id):
            if resume_at is not None and clock["now"] >= resume_at:
                clock["offset"] = 10.0
            return clock["now"] + clock["offset"]

        def diagnostic_clock():
            if timing_setting != "1":
                raise AssertionError("disabled diagnostics read perf_counter_ns")
            return int(clock["now"] * 1_000_000_000)

        discovery = (mock.Mock(side_effect=RuntimeError("WMI missing")) if unavailable
                     else mock.Mock(return_value=pathlib.Path("/fake/wmi")))
        try:
            with mock.patch.object(RECOVER, "stage_full_cooling"), \
                    mock.patch.object(RECOVER, "mark_recovery_latch"), \
                    mock.patch.object(RECOVER, "notify"), \
                    mock.patch.object(RECOVER, "recovery_identity", return_value={
                        RECOVER.fan_control.SOURCE_PATH: RECOVER.fan_control.LEGACY_SRCVERSION}), \
                    mock.patch.object(RECOVER, "read", side_effect=lambda path: state[path.name]), \
                    mock.patch.object(RECOVER, "write", side_effect=fake_write), \
                    mock.patch.object(RECOVER, "read_uint", side_effect=fake_rpm) as reading_rpm, \
                    mock.patch.object(RECOVER, "one_hwmon", discovery), \
                    mock.patch.object(RECOVER, "keeper_heartbeat", side_effect=heartbeats.append), \
                    mock.patch.object(RECOVER, "verify_boost", side_effect=AssertionError("blocking verification")), \
                    mock.patch.object(RECOVER.time, "monotonic", side_effect=lambda: clock["now"]), \
                    mock.patch.object(RECOVER.time, "clock_gettime", side_effect=boottime), \
                    mock.patch.object(RECOVER.time, "perf_counter_ns", side_effect=diagnostic_clock) as diagnostic, \
                    mock.patch.dict(RECOVER.os.environ, {} if timing_setting is None else
                                    {"MSI_FAN_TIMING_METRICS": timing_setting}, clear=True), \
                    mock.patch.object(RECOVER.sys, "stderr", stderr), \
                    mock.patch.object(RECOVER.time, "sleep", side_effect=fake_sleep):
                self.assertEqual(RECOVER.run_keeper("candidate14"), 0)
        finally:
            RECOVER.STOP = original_stop
        self.discovery_calls = discovery.call_count
        self.rpm_read_calls = reading_rpm.call_count
        self.diagnostic_reads = diagnostic.call_count
        self.stderr = stderr.getvalue()
        self.timing_records = [json.loads(line.removeprefix("TIMING keeper "))
                               for line in self.stderr.splitlines()
                               if line.startswith("TIMING keeper ")]
        return state, writes, sleeps, heartbeats

    def test_external_boost_off_is_repaired_each_second_during_invalid_or_low_rpm(self):
        for rpm, classification in ((65535, "invalid"), (1000, "low")):
            with self.subTest(classification=classification):
                state, writes, sleeps, heartbeats = self.run_keeper_cycles(rpm)
                self.assertEqual(writes, [(float(now), "cooler_boost", "on")
                                          for now in range(1, 14)])
                self.assertEqual(sleeps, [1.0] * 14)
                self.assertEqual(state["cooler_boost"], "on")
                self.assertEqual(state["fan_curve"], RECOVER.DEFAULT)
                self.assertEqual(state["fan_mode"], "advanced")
                self.assertTrue(all("fan response unverified" in status
                                    for status in heartbeats))
                self.assertIn(classification, heartbeats[-1])

    def test_missing_wmi_does_not_interrupt_repair_or_claim_verified_response(self):
        state, writes, sleeps, heartbeats = self.run_keeper_cycles(0, unavailable=True)
        self.assertEqual(len(writes), 13)
        self.assertEqual(sleeps, [1.0] * 14)
        self.assertEqual(state["cooler_boost"], "on")
        self.assertIn("fan response unverified: unavailable", heartbeats[-1])

    def test_response_recovery_and_loss_are_visible_in_current_status(self):
        _, _, _, heartbeats = self.run_keeper_cycles(
            lambda now: 5000 if 2 <= now < 4 else 65535)
        self.assertIn("fan response unverified: awaiting telemetry", heartbeats[0])
        self.assertIn("fan response unverified: invalid", heartbeats[2])
        self.assertIn("fan response verified", heartbeats[3])
        self.assertIn("fan response verified", heartbeats[4])
        self.assertIn("fan response unverified: invalid", heartbeats[5])

    def test_healthy_loops_cache_one_wmi_discovery(self):
        self.run_keeper_cycles(5000)
        self.assertEqual(self.discovery_calls, 1)

    def test_read_failure_or_disappeared_path_invalidates_wmi_cache(self):
        for failure in (OSError("read failed"), FileNotFoundError("path disappeared")):
            with self.subTest(failure=failure):
                _, _, _, heartbeats = self.run_keeper_cycles(
                    lambda now: failure if now == 4 else 5000)
                self.assertEqual(self.discovery_calls, 2)
                self.assertIn("unavailable", heartbeats[5])
                self.assertIn("fan response verified", heartbeats[6])

    def test_resume_invalidates_wmi_cache_once(self):
        self.run_keeper_cycles(5000, resume_at=4)
        self.assertEqual(self.discovery_calls, 2)

    def test_malformed_rpm_does_not_cause_repeated_discovery(self):
        for malformed in (65535, RuntimeError("nonnumeric RPM")):
            with self.subTest(malformed=malformed):
                self.run_keeper_cycles(malformed)
                self.assertEqual(self.discovery_calls, 1)

    def test_unset_zero_and_invalid_diagnostics_never_read_diagnostic_clock(self):
        for value in (None, "0", "unexpected", "", "true"):
            with self.subTest(value=value):
                self.run_keeper_cycles(5000, timing_setting=value)
                self.assertEqual(self.diagnostic_reads, 0)
                self.assertEqual(self.timing_records, [])
                self.assertEqual("timing metrics disabled" in self.stderr,
                                 value not in (None, "0"))

    def test_enabled_diagnostics_are_json_and_rate_limited_to_one_minute(self):
        self.run_keeper_cycles(5000, duration=125, timing_setting="1")
        self.assertEqual(self.discovery_calls, 1)
        self.assertEqual(len(self.timing_records), 3)
        for record, count in zip(self.timing_records, (1, 61, 121)):
            self.assertEqual(record["schema"], 1)
            self.assertEqual(record["pid"], RECOVER.os.getpid())
            self.assertEqual(record["completed_loops"], count)
            self.assertEqual(record["rpm_reads"], count)
            self.assertEqual(record["deadline_misses"], 0)
            self.assertEqual(record["slow_rpm_reads"], 0)
            self.assertEqual(record["observation_gap_misses"], 0)
            self.assertEqual(record["max_observation_gap_seconds"],
                             0.0 if count == 1 else 1.0)
        self.assertEqual(self.diagnostic_reads, 125 * 4)
        self.assertEqual(self.rpm_read_calls, 125 * 2)

    def test_enabled_diagnostics_count_slow_rpm_and_completed_loop_deadline(self):
        self.run_keeper_cycles(5000, duration=8, timing_setting="1", rpm_delay=1.25)
        self.assertEqual(len(self.timing_records), 1)
        record = self.timing_records[0]
        self.assertEqual(record["rpm_reads"], 1)
        self.assertEqual(record["last_rpm_read_duration_ms"], 2500.0)
        self.assertEqual(record["last_loop_duration_ms"], 2500.0)
        self.assertEqual(record["deadline_misses"], 1)
        self.assertEqual(record["slow_rpm_reads"], 1)

    def test_diagnostic_observation_gap_is_bounded_and_counts_missed_intervals(self):
        timing = RECOVER.KeeperTimingMetrics()
        with mock.patch.object(RECOVER.time, "perf_counter_ns", side_effect=
                               (0, 100_000_000, 2_500_000_000, 2_600_000_000)):
            timing.begin_rpm(10.0)
            timing.finish_rpm()
            timing.begin_rpm(12.5)
            timing.finish_rpm()
        self.assertEqual(timing.rpm_reads, 2)
        self.assertEqual(timing.observation_gap_misses, 1)
        self.assertEqual(timing.last_observation_gap_seconds, 2.5)
        self.assertEqual(timing.max_observation_gap_seconds, 2.5)
        self.assertEqual(timing.max_rpm_read_duration_ms, 100.0)


class IdentitySnapshotTests(HardwareIsolatedTestCase):
    def values(self, source):
        return {
            pathlib.Path("/sys/class/dmi/id/product_name"): "Raider 18 HX AI A2XWIG",
            pathlib.Path("/sys/class/dmi/id/board_name"): "MS-1824",
            pathlib.Path("/sys/class/dmi/id/bios_version"): "E1824IMS.310",
            pathlib.Path("/sys/module/msi_ec/version"): "0.13.1",
            RECOVER.fan_control.SOURCE_PATH: source,
            RECOVER.BASE / "fw_version": RECOVER.fan_control.FIRMWARE,
            RECOVER.BASE / "fan_curve": RECOVER.DEFAULT,
            RECOVER.BASE / "fan_mode": "advanced",
            RECOVER.BASE / "cooler_boost": "on",
            RECOVER.BASE / "fan_control_snapshot":
                f"1 {RECOVER.fan_control.FIRMWARE} comfort advanced on 70 60 {RECOVER.DEFAULT}",
        }

    def test_new_keeper_monitoring_uses_one_snapshot_and_no_separate_ec_controls(self):
        values = self.values(RECOVER.fan_control.SNAPSHOT_SRCVERSION)
        with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__) as read, \
                mock.patch.object(pathlib.Path, "exists", side_effect=[False, True]), \
                mock.patch.object(pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(RECOVER, "write") as write:
            self.assertEqual(RECOVER.keeper_repair("candidate14"),
                             "Candidate14/advanced with Cooler Boost on")
        sampled = [call.args[0] for call in read.call_args_list]
        self.assertEqual(sampled.count(RECOVER.BASE / "fan_control_snapshot"), 1)
        self.assertTrue(all(RECOVER.BASE / name not in sampled for name in
                            ("fw_version", "fan_curve", "fan_mode", "cooler_boost")))
        write.assert_not_called()

    def test_snapshot_boost_repair_uses_fresh_firmware_write_guard(self):
        values = self.values(RECOVER.fan_control.SNAPSHOT_SRCVERSION)
        values[RECOVER.BASE / "fan_control_snapshot"] = (
            f"1 {RECOVER.fan_control.FIRMWARE} comfort advanced off 70 60 {RECOVER.DEFAULT}")
        with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__) as read, \
                mock.patch.object(RECOVER, "write", side_effect=REAL_RECOVERY_WRITE), \
                mock.patch.object(pathlib.Path, "exists", side_effect=[False, True, False, True]), \
                mock.patch.object(pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pathlib.Path, "open") as opened:
            self.assertEqual(RECOVER.keeper_repair("candidate14"), "Cooler Boost reasserted")
        opened.assert_called_once_with("w", encoding="ascii")
        self.assertEqual(read.call_args_list.count(mock.call(RECOVER.BASE / "fw_version")), 1)
        self.assertEqual(read.call_args_list.count(mock.call(RECOVER.BASE / "fan_control_snapshot")), 1)

    def test_malformed_snapshot_cannot_fall_back_to_legacy_controls(self):
        for raw in ("", "1 wrong-firmware comfort advanced on 70 60 " + RECOVER.DEFAULT):
            with self.subTest(raw=raw):
                values = self.values(RECOVER.fan_control.SNAPSHOT_SRCVERSION)
                values[RECOVER.BASE / "fan_control_snapshot"] = raw
                with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__) as read, \
                        mock.patch.object(pathlib.Path, "exists", side_effect=[False, True]), \
                        mock.patch.object(pathlib.Path, "is_file", return_value=True), \
                        mock.patch.object(RECOVER, "stage_full_cooling") as stage, \
                        mock.patch.object(RECOVER, "write") as write:
                    with self.assertRaisesRegex(RuntimeError, "identity/ABI"):
                        RECOVER.keeper_repair("candidate14")
                stage.assert_not_called()
                write.assert_not_called()
                self.assertNotIn(mock.call(RECOVER.BASE / "fan_curve"), read.call_args_list)

    def test_legacy_keeper_retains_fresh_firmware_and_separate_control_reads(self):
        values = self.values(RECOVER.fan_control.LEGACY_SRCVERSION)
        with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__) as read, \
                mock.patch.object(pathlib.Path, "exists", side_effect=[False, True]):
            RECOVER.keeper_repair("candidate14")
        for name in ("fw_version", "fan_curve", "fan_mode", "cooler_boost"):
            self.assertIn(mock.call(RECOVER.BASE / name), read.call_args_list)
        self.assertNotIn(mock.call(RECOVER.BASE / "fan_control_snapshot"), read.call_args_list)

    def test_legacy_keeper_firmware_mismatch_rejects_all_control_actions(self):
        values = self.values(RECOVER.fan_control.LEGACY_SRCVERSION)
        values[RECOVER.BASE / "fw_version"] = "changed-firmware"
        with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__), \
                mock.patch.object(pathlib.Path, "exists", return_value=False), \
                mock.patch.object(RECOVER, "write") as write, \
                mock.patch.object(RECOVER, "stage_full_cooling") as stage:
            with self.assertRaisesRegex(RuntimeError, "identity/ABI"):
                RECOVER.keeper_repair("candidate14")
        write.assert_not_called()
        stage.assert_not_called()

    def test_each_new_driver_write_requires_fresh_firmware(self):
        values = self.values(RECOVER.fan_control.SNAPSHOT_SRCVERSION)
        firmware = iter((RECOVER.fan_control.FIRMWARE, "changed-firmware"))

        def read(path):
            return next(firmware) if path.name == "fw_version" else values[path]

        with mock.patch.object(RECOVER, "read", side_effect=read) as reading, \
                mock.patch.object(RECOVER, "write", side_effect=REAL_RECOVERY_WRITE), \
                mock.patch.object(pathlib.Path, "exists", side_effect=[False, True, False]), \
                mock.patch.object(pathlib.Path, "is_file", return_value=True), \
                mock.patch.object(pathlib.Path, "open") as opened:
            RECOVER.write(RECOVER.BASE / "cooler_boost", "on")
            with self.assertRaisesRegex(RuntimeError, "identity invalid"):
                RECOVER.write(RECOVER.BASE / "cooler_boost", "on")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(reading.call_args_list.count(mock.call(RECOVER.BASE / "fw_version")), 2)

    def test_unknown_hash_or_missing_new_snapshot_is_rejected(self):
        for source, present in (("unrecognized-build", True),
                                (RECOVER.fan_control.SNAPSHOT_SRCVERSION, False)):
            with self.subTest(source=source):
                values = self.values(source)
                with mock.patch.object(RECOVER, "read", side_effect=values.__getitem__), \
                        mock.patch.object(pathlib.Path, "exists", return_value=False), \
                        mock.patch.object(pathlib.Path, "is_file", return_value=present):
                    self.assertFalse(RECOVER.identity_safe_for_curve_write())


if __name__ == "__main__":
    unittest.main()
