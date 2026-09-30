#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only exact driver ABI checks with fully mocked host and EC channels."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
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
    def identity(self, values, *, monitoring=False, abi_present=True):
        read = mock.Mock(side_effect=lambda path: values[path])
        with mock.patch.object(CONTROL.pathlib.Path, 'exists', return_value=False), \
                mock.patch.object(CONTROL.pathlib.Path, 'is_file', return_value=abi_present):
            result = CONTROL.identity(read, BASE, monitoring=monitoring)
        return result, read

    def test_legacy_identity_checks_firmware_even_when_monitoring(self):
        values = identity_values()
        result, read = self.identity(values, monitoring=True, abi_present=False)
        self.assertEqual(result[BASE / 'fw_version'], CONTROL.FIRMWARE)
        self.assertEqual(read.call_args_list.count(mock.call(BASE / 'fw_version')), 1)
        self.assertNotIn(mock.call(BASE / 'fan_control_snapshot'), read.call_args_list)

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


if __name__ == '__main__':
    unittest.main()
