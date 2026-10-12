#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only exact driver ABI checks with fully mocked host and EC channels."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('msi_fan_control', ROOT / 'src/msi_fan_control.py')
CONTROL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTROL
SPEC.loader.exec_module(CONTROL)
BASE = pathlib.Path('/fake/msi-ec')
CURVE = '50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120'


def identity_values(source=CONTROL.LEGACY_SRCVERSION):
    return {
        pathlib.Path('/sys/class/dmi/id/product_name'): 'Raider 18 HX AI A2XWIG',
        pathlib.Path('/sys/class/dmi/id/board_name'): 'MS-1824',
        pathlib.Path('/sys/class/dmi/id/bios_version'): 'E1824IMS.310',
        pathlib.Path('/sys/module/msi_ec/version'): '0.13.1',
        CONTROL.SOURCE_PATH: source,
        BASE / 'fw_version': CONTROL.FIRMWARE,
    }


def snapshot_text(cpu=50, gpu=40):
    return f'1 {CONTROL.FIRMWARE} comfort advanced on {cpu} {gpu} {CURVE}\n'


class ExactIdentityTests(unittest.TestCase):
    def identity(self, values, *, monitoring=False, abi_present=True, **options):
        read = mock.Mock(side_effect=lambda path: values[path])
        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=False), \
                mock.patch.object(CONTROL.pathlib.Path, 'is_file', return_value=abi_present):
            result = CONTROL.identity(read, BASE, monitoring=monitoring, **options)
        return result, read

    def test_legacy_identity_checks_firmware_even_when_monitoring(self):
        # The manager's status reports this value from the same validated read.
        values = identity_values()
        result, read = self.identity(values, monitoring=True, abi_present=False)
        self.assertEqual(result[BASE / 'fw_version'], CONTROL.FIRMWARE)
        self.assertEqual(read.call_args_list.count(mock.call(BASE / 'fw_version')), 1)
        self.assertNotIn(mock.call(BASE / 'fan_control_snapshot'), read.call_args_list)

    def test_curve_read_opt_in_skips_only_the_legacy_monitoring_firmware_read(self):
        values = identity_values()
        result, read = self.identity(values, monitoring=True, abi_present=False,
                                     firmware_checked_by_curve_read=True)
        self.assertEqual(result[CONTROL.SOURCE_PATH], CONTROL.LEGACY_SRCVERSION)
        self.assertNotIn(BASE / 'fw_version', result)
        self.assertNotIn(mock.call(BASE / 'fw_version'), read.call_args_list)
        # The opt-in never weakens a mutation's own fresh firmware check.
        values[BASE / 'fw_version'] = '1824EMS1.999'
        with self.assertRaisesRegex(CONTROL.ControlABIError, 'EC firmware mismatch'):
            self.identity(values, abi_present=False, firmware_checked_by_curve_read=True)
        # Host and driver identity are still checked before the curve read.
        values = identity_values('unknown-build')
        with self.assertRaisesRegex(CONTROL.ControlABIError, 'unsupported exact driver build'):
            self.identity(values, monitoring=True, firmware_checked_by_curve_read=True)

    def test_new_monitoring_defers_firmware_to_snapshot_but_mutations_check_it(self):
        values = identity_values(CONTROL.SNAPSHOT_SRCVERSION)
        result, read = self.identity(values, monitoring=True)
        self.assertEqual(result[CONTROL.SOURCE_PATH], CONTROL.SNAPSHOT_SRCVERSION)
        self.assertNotIn(BASE / 'fw_version', result)
        self.assertNotIn(mock.call(BASE / 'fw_version'), read.call_args_list)
        result, read = self.identity(values)
        self.assertEqual(result[BASE / 'fw_version'], CONTROL.FIRMWARE)
        self.assertEqual(read.call_args_list.count(mock.call(BASE / 'fw_version')), 1)

    def test_unknown_driver_hash_is_rejected_before_firmware_read(self):
        values = identity_values('unknown-build')
        read = mock.Mock(side_effect=lambda path: values[path])
        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=False):
            with self.assertRaisesRegex(CONTROL.ControlABIError, 'unsupported exact driver build'):
                CONTROL.identity(read, BASE)
        self.assertNotIn(mock.call(BASE / 'fw_version'), read.call_args_list)

    def test_new_driver_missing_snapshot_abi_never_uses_legacy_fallback(self):
        values = identity_values(CONTROL.SNAPSHOT_SRCVERSION)
        read = mock.Mock(side_effect=lambda path: values[path])
        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=False), \
                mock.patch.object(CONTROL.pathlib.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(CONTROL.ControlABIError, 'missing its required ABI'):
                CONTROL.identity(read, BASE, monitoring=True)
        self.assertNotIn(mock.call(BASE / 'fw_version'), read.call_args_list)

    def test_mutations_reject_wrong_firmware_on_either_known_driver(self):
        for source in CONTROL.SUPPORTED_SRCVERSIONS:
            values = identity_values(source)
            values[BASE / 'fw_version'] = '1824EMS1.999'
            with self.subTest(source=source):
                with self.assertRaisesRegex(CONTROL.ControlABIError, 'EC firmware mismatch'):
                    self.identity(values)

    def test_dmi_bios_and_module_version_mismatches_are_rejected(self):
        for path in (pathlib.Path('/sys/class/dmi/id/product_name'),
                     pathlib.Path('/sys/class/dmi/id/board_name'),
                     pathlib.Path('/sys/class/dmi/id/bios_version'),
                     pathlib.Path('/sys/module/msi_ec/version')):
            values = identity_values()
            values[path] = 'wrong'
            with self.subTest(path=path):
                with self.assertRaisesRegex(CONTROL.ControlABIError, 'identity mismatch'):
                    self.identity(values)

    def test_absent_or_versionless_driver_names_the_running_kernel(self):
        version = pathlib.Path('/sys/module/msi_ec/version')
        for module_present, wanted in (
                (False, 'msi_ec driver not loaded for kernel 7.0.0-99-generic$'),
                (True, 'msi_ec driver loaded for kernel 7.0.0-99-generic is not the '
                       'pinned 0.13.1 build')):
            values = identity_values()
            del values[version]

            def read(path):
                if path == version:
                    raise FileNotFoundError(2, 'No such file or directory', str(path))
                return values[path]

            reader = mock.Mock(side_effect=read)
            with self.subTest(module_present=module_present), \
                    mock.patch.object(CONTROL.pathlib.Path, 'exists', autospec=True,
                                      side_effect=lambda path: (
                                          module_present and path == CONTROL.MODULE_PATH)), \
                    mock.patch.object(CONTROL.os, 'uname', return_value=mock.Mock(
                        release='7.0.0-99-generic')):
                with self.assertRaisesRegex(CONTROL.ControlABIError, wanted):
                    CONTROL.identity(reader, BASE)
            self.assertNotIn(mock.call(CONTROL.SOURCE_PATH), reader.call_args_list)
            self.assertNotIn(mock.call(BASE / 'fw_version'), reader.call_args_list)

    def test_other_missing_identity_files_keep_their_original_error(self):
        values = identity_values()
        missing = pathlib.Path('/sys/class/dmi/id/bios_version')

        def read(path):
            if path == missing:
                raise FileNotFoundError(2, 'No such file or directory', str(path))
            return values[path]

        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=False):
            with self.assertRaises(FileNotFoundError):
                CONTROL.identity(read, BASE)

    def test_loaded_ec_sys_is_rejected_before_reading_host_or_ec(self):
        read = mock.Mock()
        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=True):
            with self.assertRaisesRegex(CONTROL.ControlABIError, 'ec_sys is loaded'):
                CONTROL.identity(read, BASE)
        read.assert_not_called()


class SnapshotParserTests(unittest.TestCase):
    def test_known_snapshot_preserves_complete_curve_and_control_state(self):
        snapshot = CONTROL.parse_snapshot(snapshot_text())
        self.assertEqual(snapshot.firmware, CONTROL.FIRMWARE)
        self.assertEqual((snapshot.shift, snapshot.mode, snapshot.boost), ('comfort', 'advanced', 'on'))
        self.assertEqual((snapshot.ec_cpu, snapshot.ec_gpu), (50, 40))
        self.assertEqual(snapshot.curve, CURVE)

    def test_raw_temperature_sentinel_is_preserved_for_daemon_classification(self):
        snapshot = CONTROL.parse_snapshot(snapshot_text(cpu=255, gpu=255))
        self.assertEqual((snapshot.ec_cpu, snapshot.ec_gpu), (255, 255))

    def test_partial_schema_wrong_firmware_and_extra_tokens_are_rejected(self):
        valid = snapshot_text().split()
        samples = [' '.join(valid[:-1]), ' '.join(valid + ['0']),
                   snapshot_text().replace('1 ', '2 ', 1),
                   snapshot_text().replace(CONTROL.FIRMWARE, '1824EMS1.999')]
        for raw in samples:
            with self.subTest(raw=raw):
                with self.assertRaises(CONTROL.ControlABIError):
                    CONTROL.parse_snapshot(raw)

    def test_unknown_control_states_and_non_ascii_are_rejected(self):
        for old, new in (('comfort', 'unknown'), ('advanced', 'unknown'),
                         ('on', 'unknown'), ('comfort', 'comfört')):
            with self.subTest(old=old, new=new):
                with self.assertRaises(CONTROL.ControlABIError):
                    CONTROL.parse_snapshot(snapshot_text().replace(old, new, 1))

    def test_bytes_outside_unsigned_u8_or_nondecimal_are_rejected(self):
        for token in ('-1', '256', '+50', '0xff', '50.0', 'NaN'):
            raw = snapshot_text().split()
            raw[5] = token
            with self.subTest(token=token):
                with self.assertRaisesRegex(CONTROL.ControlABIError, 'invalid control snapshot byte'):
                    CONTROL.parse_snapshot(' '.join(raw))

    def test_snapshot_rejects_embedded_line_breaks_and_control_whitespace(self):
        for separator in ('\n', '\r', '\t', '\v', '\x00'):
            raw = snapshot_text().replace(' comfort ', separator + 'comfort ', 1)
            with self.subTest(separator=repr(separator)):
                with self.assertRaises(CONTROL.ControlABIError):
                    CONTROL.parse_snapshot(raw)

    def test_snapshot_uses_one_fresh_read_and_no_separate_firmware_channel(self):
        read = mock.Mock(return_value=snapshot_text())
        self.assertEqual(CONTROL.snapshot(read, BASE).curve, CURVE)
        read.assert_called_once_with(BASE / 'fan_control_snapshot')

    def test_malformed_snapshot_or_read_error_is_not_retried(self):
        for value in ('partial', OSError('snapshot read failed')):
            read = (mock.Mock(side_effect=value) if isinstance(value, Exception)
                    else mock.Mock(return_value=value))
            with self.subTest(value=value):
                with self.assertRaises((CONTROL.ControlABIError, OSError)):
                    CONTROL.snapshot(read, BASE)
                read.assert_called_once_with(BASE / 'fan_control_snapshot')


COOL = ('59000', '60', '44')
HOT = ('90000', '60', '44')
JUNK = ('59000', '255', '44')


class FakeClock:
    """Monotonic time that only sleeping advances, plus an optional read cost."""

    def __init__(self, read_seconds=0.0):
        self.now = 0.0
        self.read_seconds = read_seconds
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def scripted(clock, readings):
    """Return a sampler replaying readings (the last one repeats) and its sample times."""
    times = []

    def sample():
        reading = readings[min(len(times), len(readings) - 1)]
        times.append(clock.now)
        clock.now += clock.read_seconds
        if isinstance(reading, BaseException):
            raise reading
        return reading
    return sample, times


class CoolConfirmationTests(unittest.TestCase):
    def confirm(self, readings, *, clock=None, **options):
        clock = clock or FakeClock()
        sample, times = scripted(clock, readings)
        result = CONTROL.confirm_cool(sample, monotonic=clock.monotonic,
                                      sleep=clock.sleep, **options)
        return result, times, clock

    def test_window_constants_are_six_samples_half_a_second_apart_within_ten_seconds(self):
        self.assertEqual((CONTROL.COOL_CONFIRM_SAMPLES, CONTROL.COOL_CONFIRM_INTERVAL_SECONDS,
                          CONTROL.COOL_CONFIRM_TIMEOUT_SECONDS), (6, 0.5, 10.0))
        # The release thresholds themselves are unchanged.
        self.assertEqual((CONTROL.COOL_PACKAGE_LIMIT_MILLIDEGREES,
                          CONTROL.COOL_EC_CPU_LIMIT_CELSIUS,
                          CONTROL.COOL_EC_GPU_LIMIT_CELSIUS), (85000, 85, 70))

    def test_all_cool_confirms_after_exactly_six_samples_half_a_second_apart(self):
        result, times, clock = self.confirm([COOL])
        self.assertTrue(result.cool)
        self.assertEqual(result.samples, 6)
        self.assertEqual(times, [0.0, 0.5, 1.0, 1.5, 2.0, 2.5])
        self.assertEqual(clock.sleeps, [0.5] * 5)
        self.assertEqual(result.elapsed, 2.5)
        self.assertIsNone(result.unsafe)
        self.assertIn('6 consecutive valid cool samples 0.5 s apart after 2.5 s', result.reason)

    def test_one_spike_mid_window_restarts_the_count_and_still_confirms(self):
        unsafe = []
        result, times, _ = self.confirm([COOL, COOL, HOT, COOL], on_unsafe=unsafe.append)
        self.assertTrue(result.cool)
        # Two cool samples, the spike, then six fresh consecutive cool samples.
        self.assertEqual(result.samples, 9)
        self.assertEqual(times[-1], 4.0)
        self.assertEqual([reading.state for reading in unsafe], ['hot'])
        self.assertEqual(result.unsafe.state, 'hot')

    def test_a_spike_right_at_the_start_is_the_incident_and_no_longer_refuses(self):
        # 2026-10-11: the package read 59 C, then >=85 C 0.26 s later, then cool.
        result, times, _ = self.confirm([HOT, COOL])
        self.assertTrue(result.cool)
        self.assertEqual(result.samples, 7)
        self.assertEqual(times[-1], 3.0)

    def test_spikes_every_few_samples_never_confirm(self):
        unsafe = []
        result, times, clock = self.confirm([COOL, COOL, COOL, COOL, COOL, HOT] * 10,
                                            on_unsafe=unsafe.append)
        self.assertFalse(result.cool)
        self.assertEqual(result.unsafe.state, 'hot')
        self.assertLessEqual(times[-1], 10.0)
        self.assertLessEqual(sum(clock.sleeps), 10.0)
        self.assertGreaterEqual(len(unsafe), 2)
        self.assertIn('no 6 consecutive valid cool samples 0.5 s apart within 10 s',
                      result.reason)
        self.assertIn('most recent unsafe sample (hot): CPU package 90.0 C >= 85 C',
                      result.reason)

    def test_one_junk_or_low_reading_can_never_release_alone(self):
        # The converse of a spike: a single plausible-looking cool value among hot
        # samples is not agreement.
        result, *_ = self.confirm([HOT, COOL, HOT, COOL, COOL, HOT] * 10)
        self.assertFalse(result.cool)

    def test_junk_values_are_invalid_and_reset_the_count(self):
        junk = ('', ' ', 'hot', '-5', '+50', '0x3b', '59.5', 'nan', '1234567',
                '\u0665\u0660')
        # The EC's 255 sentinel is junk in degrees and 0 is junk for either CPU
        # channel; EC GPU 0 is a powered-off dGPU (see the dedicated test).
        # More than six digits is junk even when the value is in range.
        for label, position, extra in (('package', 0, ('0', '110001', '0000059000',
                                                       '0059000')),
                                       ('EC CPU', 1, ('255', '0', '111', '0000060',
                                                      '0000001')),
                                       ('EC GPU', 2, ('255', '111', '0000044', '0000000'))):
            for value in junk + extra:
                reading = list(COOL)
                reading[position] = value
                with self.subTest(label=label, value=value):
                    classified = CONTROL.classify_cool_reading(*reading)
                    self.assertEqual(classified.state, 'invalid')
                    result, times, _ = self.confirm([COOL, COOL, tuple(reading), COOL])
                    self.assertTrue(result.cool)
                    self.assertEqual(result.samples, 9)
                    self.assertEqual(result.unsafe.state, 'invalid')
        for reading in (('110001', '60', '44'), ('59000', '111', '44'),
                        ('59000', '60', '111'), JUNK, (59000, '60', '44'), (None, '60', '44')):
            with self.subTest(reading=reading):
                self.assertEqual(CONTROL.classify_cool_reading(*reading).state, 'invalid')

    def test_persistent_junk_never_confirms(self):
        result, times, _ = self.confirm([JUNK])
        self.assertFalse(result.cool)
        self.assertEqual(result.unsafe.state, 'invalid')
        self.assertIn("EC CPU invalid ('255')", result.reason)

    def test_unreadable_samples_are_unavailable_and_reset_the_count(self):
        for error in (OSError(5, 'Input/output error'),
                      UnicodeDecodeError('ascii', b'\xff', 0, 1, 'junk')):
            with self.subTest(error=type(error).__name__):
                result, times, _ = self.confirm([COOL, error, COOL])
                self.assertTrue(result.cool)
                self.assertEqual(result.samples, 8)
                self.assertEqual(result.unsafe.state, 'unavailable')
                result, *_ = self.confirm([error])
                self.assertFalse(result.cool)
                self.assertIn('telemetry unavailable', result.reason)

    def test_other_sample_errors_propagate_to_the_caller(self):
        with self.assertRaises(RuntimeError):
            self.confirm([COOL, RuntimeError('unexpected')])

    def test_powered_off_gpu_and_thresholds_are_exactly_the_existing_limits(self):
        for reading, state in (
                (('59000', '60', '0'), 'cool'), (('1', '1', '0'), 'cool'),
                # Up to six digits, leading zeros included, is a valid reading.
                (('059000', '000060', '000044'), 'cool'),
                (('84999', '84', '69'), 'cool'), (('85000', '60', '44'), 'hot'),
                (('59000', '85', '44'), 'hot'), (('59000', '60', '70'), 'hot'),
                (('110000', '110', '110'), 'hot')):
            with self.subTest(reading=reading):
                self.assertEqual(CONTROL.classify_cool_reading(*reading).state, state)
        result, *_ = self.confirm([('59000', '60', '0')])
        self.assertTrue(result.cool)

    def test_persistent_heat_stops_once_six_samples_can_no_longer_fit(self):
        result, times, clock = self.confirm([HOT])
        self.assertFalse(result.cool)
        # The last start that leaves room for six samples is 7.5 s.
        self.assertEqual(times, [index * 0.5 for index in range(16)])
        self.assertEqual(clock.sleeps, [0.5] * 15)
        self.assertEqual(result.elapsed, 7.5)

    def test_the_window_edge_is_inclusive_and_bounded_by_the_timeout(self):
        result, times, _ = self.confirm([HOT] * 15 + [COOL])
        self.assertTrue(result.cool)
        self.assertEqual(times[-1], 10.0)
        result, times, clock = self.confirm([HOT] * 16 + [COOL])
        self.assertFalse(result.cool)
        self.assertEqual(times[-1], 7.5)
        for options in ({'samples': 3, 'interval': 1.0, 'timeout': 4.0},
                        {'samples': 2, 'interval': 0.25, 'timeout': 1.0}):
            with self.subTest(options=options):
                result, times, clock = self.confirm([HOT], **options)
                self.assertFalse(result.cool)
                self.assertLessEqual(times[-1], options['timeout'])
                self.assertLessEqual(sum(clock.sleeps), options['timeout'])

    def test_slow_reads_keep_sample_starts_an_interval_apart(self):
        result, times, clock = self.confirm([COOL], clock=FakeClock(read_seconds=0.2))
        self.assertTrue(result.cool)
        self.assertEqual(len(clock.sleeps), 5)
        for delay in clock.sleeps:
            self.assertAlmostEqual(delay, 0.3)
        self.assertTrue(all(abs(later - earlier - 0.5) < 1e-9
                            for earlier, later in zip(times, times[1:])))
        result, times, clock = self.confirm([COOL], clock=FakeClock(read_seconds=0.8))
        self.assertTrue(result.cool)
        self.assertEqual(clock.sleeps, [])
        result, times, clock = self.confirm([HOT], clock=FakeClock(read_seconds=0.8))
        self.assertFalse(result.cool)
        self.assertLessEqual(times[-1], 10.0)

    def test_reads_slower_than_the_interval_never_start_after_the_deadline(self):
        # Five 2.1 s cool samples end at 10.5 s; a sixth would start too late.
        result, times, clock = self.confirm([COOL], clock=FakeClock(read_seconds=2.1))
        self.assertFalse(result.cool)
        self.assertEqual(result.samples, 5)
        self.assertLessEqual(times[-1], 10.0)
        self.assertLessEqual(result.elapsed, 10.0 + 2.1)
        self.assertIsNone(result.unsafe)
        self.assertIn('samples too slow; last: CPU package 59.0 C', result.reason)

    def test_heartbeat_reports_progress_after_every_sample(self):
        beats = []
        result, *_ = self.confirm([HOT, COOL], heartbeat=beats.append)
        self.assertTrue(result.cool)
        self.assertEqual(len(beats), 7)
        self.assertIn('0/6 consecutive valid cool samples; CPU package 90.0 C >= 85 C', beats[0])
        self.assertIn('6/6 consecutive valid cool samples; CPU package 59.0 C, EC CPU 60 C, '
                      'EC GPU 44 C', beats[-1])
        failing = mock.Mock(side_effect=OSError('notify failed'))
        with self.assertRaises(OSError):
            self.confirm([COOL], heartbeat=failing)
        failing.assert_called_once()

    def test_unsafe_callback_runs_immediately_for_every_unsafe_sample(self):
        # An unreadable sample is as unsafe as a hot or junk one: callers
        # reassert Boost from this callback, so it must run for each of them.
        clock = FakeClock()
        calls = []
        sample, _ = scripted(clock, [HOT, JUNK, OSError(5, 'I/O error'),
                                     UnicodeDecodeError('ascii', b'\xff', 0, 1, 'junk'), COOL])
        result = CONTROL.confirm_cool(
            sample, monotonic=clock.monotonic, sleep=clock.sleep,
            on_unsafe=lambda reading: calls.append((clock.now, reading.state)))
        self.assertTrue(result.cool)
        self.assertEqual(calls, [(0.0, 'hot'), (0.5, 'invalid'), (1.0, 'unavailable'),
                                 (1.5, 'unavailable')])

    def test_window_that_cannot_hold_its_samples_is_rejected(self):
        for options in ({'samples': 0}, {'interval': 0}, {'timeout': 2.0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.confirm([COOL], **options)

    def test_sampler_reads_package_and_ec_temperatures_through_the_callers_reader(self):
        core = pathlib.Path('/fake/core')
        read = mock.Mock(side_effect=['59000', '60', '44'])
        self.assertEqual(CONTROL.cool_sampler(read, core, BASE)(), COOL)
        self.assertEqual(read.call_args_list, [
            mock.call(core / 'temp1_input'), mock.call(BASE / 'cpu/realtime_temperature'),
            mock.call(BASE / 'gpu/realtime_temperature')])

    def test_default_clock_is_resolved_at_call_time(self):
        clock = FakeClock()
        sample, times = scripted(clock, [COOL])
        with mock.patch.object(CONTROL.time, 'monotonic', clock.monotonic), \
                mock.patch.object(CONTROL.time, 'sleep', clock.sleep):
            self.assertTrue(CONTROL.confirm_cool(sample).cool)
        self.assertEqual(clock.sleeps, [0.5] * 5)


class UninstallCoolConfirmationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='msi-fan-control-test-')
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self.hwmon = root / 'hwmon'
        self.base = root / 'msi-ec'
        for name, value in (('hwmon0', 'acpitz'), ('hwmon3', 'coretemp')):
            (self.hwmon / name).mkdir(parents=True)
            (self.hwmon / name / 'name').write_text(value + '\n')
        for channel in ('cpu', 'gpu'):
            (self.base / channel).mkdir(parents=True)
        self.write(*COOL)
        self.clock = FakeClock()

    def write(self, package, ec_cpu, ec_gpu):
        (self.hwmon / 'hwmon3' / 'temp1_input').write_text(package + '\n')
        (self.base / 'cpu' / 'realtime_temperature').write_text(ec_cpu + '\n')
        (self.base / 'gpu' / 'realtime_temperature').write_text(ec_gpu + '\n')

    def run_main(self, argv=None, on_sleep=None):
        def sleep(seconds):
            self.clock.sleep(seconds)
            if on_sleep:
                on_sleep(self.clock.now)

        stderr = io.StringIO()
        with mock.patch.object(CONTROL.time, 'monotonic', self.clock.monotonic), \
                mock.patch.object(CONTROL.time, 'sleep', sleep), \
                contextlib.redirect_stderr(stderr):
            code = CONTROL.main(argv or ['confirm-cool', str(self.hwmon), str(self.base)])
        return code, stderr.getvalue()

    def test_cool_files_confirm_after_six_samples(self):
        code, output = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('Cooler Boost release confirmed: 6 consecutive', output)
        self.assertEqual(self.clock.sleeps, [0.5] * 5)

    def test_spike_at_the_first_sample_still_confirms_when_cool_afterwards(self):
        self.write(*HOT)
        code, _ = self.run_main(on_sleep=lambda now: self.write(*COOL))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.clock.sleeps), 6)

    def test_heat_empty_or_sentinel_values_are_refused_within_the_bound(self):
        for reading in (HOT, ('', '60', '44'), JUNK):
            with self.subTest(reading=reading):
                self.clock = FakeClock()
                self.write(*reading)
                code, output = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn('Cooler Boost release not confirmed: no 6 consecutive', output)
                self.assertLessEqual(self.clock.now, 10.0)

    def test_coretemp_must_be_unique_and_present(self):
        (self.hwmon / 'hwmon5').mkdir()
        (self.hwmon / 'hwmon5' / 'name').write_text('coretemp\n')
        code, output = self.run_main()
        self.assertEqual((code, self.clock.sleeps), (1, []))
        self.assertIn('expected one coretemp hwmon; found 2', output)
        code, output = self.run_main(['confirm-cool', str(self.base), str(self.base)])
        self.assertIn('found 0', output)
        self.assertEqual(code, 1)
        self.assertEqual(self.run_main(['confirm-cool'])[0], 2)

    def test_uninstall_uses_the_shared_confirmation_before_releasing_boost(self):
        script = (ROOT / 'scripts/uninstall-profile.sh').read_text(encoding='utf-8')
        check = script.index('"$repo/src/msi_fan_control.py" confirm-cool')
        self.assertIn('/usr/bin/python3 -I -B "$repo/src/msi_fan_control.py" confirm-cool '
                      '\\\n    /sys/class/hwmon "$base"', script)
        self.assertLess(script.index("printf 'on\\n' > \"$base/cooler_boost\""), check)
        self.assertLess(check, script.index("printf 'off\\n' > \"$base/cooler_boost\""))
        # The former single-sample arithmetic is gone.
        self.assertNotIn('package < 85000', script)
        self.assertNotIn('temp1_input', script)


if __name__ == '__main__':
    unittest.main()
