#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for manager activation failure boundaries."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import contextlib
import collections
import io
import json
import pathlib
import sys
import types
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


def managed_snapshot():
    return {
        'service_enabled': True,
        'service_active_state': 'active',
        'service_sub_state': 'running',
        'service_main_pid': 17,
        'service_job': '',
        'keeper_active_state': 'inactive',
        'keeper_sub_state': 'dead',
        'keeper_main_pid': 0,
        'keeper_job': '',
        'factory_override': False,
        'recovery_boost_latched': False,
        'curve': 'candidate14',
        'fan_mode': 'advanced',
        'cooler_boost': 'on',
    }


def health_payload(**updates):
    payload = {
        'schema': 1,
        'pid': 17,
        'sampled_monotonic_ns': 9_000_000_000,
        'last_valid_monotonic_ns': 8_000_000_000,
        'state': 'normal',
        'reason': '',
        'manual_boost': False,
        'automatic_boost': False,
        'release_pending': False,
        'physical_response_verified': False,
        'cooldown_elapsed_seconds': 0.0,
        'telemetry': {
            'package_millidegrees': 50000,
            'ec_cpu_celsius': 50,
            'ec_gpu_celsius': 40,
            'fan1_rpm': 4000,
            'fan2_rpm': 4000,
        },
        'channel_errors': {},
    }
    payload.update(updates)
    return payload


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
            "recovery_boost_latched": False,
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
                mock.patch.object(MANAGER, "clear_recovery_latch") as clear_latch, \
                mock.patch.object(MANAGER, "ctl", side_effect=fake_ctl), \
                mock.patch.object(MANAGER, "full_status", return_value=healthy), \
                mock.patch.object(MANAGER.os, "close"):
            MANAGER.apply_default()

        self.assertLess(calls.index(("stop_keeper",)),
                        calls.index(("start", MANAGER.SERVICE)))
        clear_latch.assert_not_called()

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
            "recovery_boost_latched": False,
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

    def test_factory_auto_clears_the_recovery_latch_after_safe_factory_handoff(self):
        calls = []
        factory_auto = {
            "service_enabled": True,
            "service_active_state": "inactive",
            "service_sub_state": "dead",
            "service_main_pid": 0,
            "service_job": "",
            "keeper_active_state": "inactive",
            "keeper_sub_state": "dead",
            "keeper_main_pid": 0,
            "keeper_job": "",
            "factory_override": True,
            "recovery_boost_latched": False,
            "curve": "factory",
            "fan_mode": "auto",
            "cooler_boost": "off",
        }

        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", side_effect=(9, 10)), \
                mock.patch.object(MANAGER, "make_marker"), \
                mock.patch.object(MANAGER, "stop_keeper"), \
                mock.patch.object(MANAGER, "ctl"), \
                mock.patch.object(MANAGER, "inactive_dead", return_value=True), \
                mock.patch.object(
                    MANAGER, "restore_factory_locked",
                    side_effect=lambda **_kwargs: calls.append("restore")), \
                mock.patch.object(
                    MANAGER, "clear_recovery_latch",
                    side_effect=lambda: calls.append("clear")), \
                mock.patch.object(MANAGER, "full_status", return_value=factory_auto), \
                mock.patch.object(MANAGER.os, "close"):
            MANAGER.factory_auto()

        self.assertEqual(calls, ["restore", "clear"])

    def test_factory_auto_keeps_the_recovery_latch_when_factory_release_fails(self):
        with mock.patch.object(MANAGER, "root"), \
                mock.patch.object(MANAGER, "identity"), \
                mock.patch.object(MANAGER, "lock", side_effect=(9, 10)), \
                mock.patch.object(MANAGER, "make_marker"), \
                mock.patch.object(MANAGER, "stop_keeper"), \
                mock.patch.object(MANAGER, "ctl"), \
                mock.patch.object(MANAGER, "inactive_dead", return_value=True), \
                mock.patch.object(
                    MANAGER, "restore_factory_locked",
                    side_effect=MANAGER.ManagerError("too hot")), \
                mock.patch.object(MANAGER, "clear_recovery_latch") as clear_latch, \
                mock.patch.object(MANAGER, "disable_and_safe_factory") as fallback, \
                mock.patch.object(MANAGER.os, "close"):
            with self.assertRaisesRegex(MANAGER.ManagerError, "service disabled"):
                MANAGER.factory_auto()

        clear_latch.assert_not_called()
        fallback.assert_called_once_with()

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


class RecoveryLatchValidationTests(unittest.TestCase):
    def test_unprivileged_status_reports_private_marker_presence_without_opening_it(self):
        latch = mock.Mock()
        latch.lstat.return_value = types.SimpleNamespace(
            st_mode=MANAGER.stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1)
        units = {
            MANAGER.SERVICE: {'ActiveState': 'failed', 'SubState': 'failed',
                              'MainPID': '0', 'Job': '', 'UnitFileState': 'enabled'},
            MANAGER.KEEPER: {'ActiveState': 'active', 'SubState': 'running',
                             'MainPID': '18', 'Job': '', 'UnitFileState': 'static'},
        }
        with mock.patch.object(MANAGER, 'RECOVERY_LATCH', latch), \
                mock.patch.object(MANAGER.os, 'geteuid', return_value=1000), \
                mock.patch.object(MANAGER, 'identity', return_value={
                    MANAGER.BASE / 'fw_version': '1824EMS1.108',
                    pathlib.Path('/sys/module/msi_ec/version'): '0.13.1',
                    pathlib.Path('/sys/module/msi_ec/srcversion'): 'AB0BFAE2391B5ADD66E01BD'}), \
                mock.patch.object(MANAGER, 'unit_states', return_value=units), \
                mock.patch.object(MANAGER, 'hw', return_value={
                    'curve': 'candidate14', 'mode': 'advanced', 'boost': 'on'}), \
                mock.patch.object(MANAGER, 'marker', return_value=False), \
                mock.patch.object(MANAGER, 'read', return_value='test identity'), \
                mock.patch.object(MANAGER.os, 'open') as open_file:
            snapshot = MANAGER.full_status()
        self.assertTrue(snapshot['recovery_boost_latched'])
        self.assertEqual(snapshot['keeper_main_pid'], 18)
        self.assertFalse(MANAGER.managed_default(snapshot))
        open_file.assert_not_called()

    def test_clear_still_requires_verified_private_marker_payload(self):
        latch = mock.Mock()
        info = types.SimpleNamespace(
            st_mode=MANAGER.stat.S_IFREG | 0o600, st_uid=0, st_gid=0,
            st_nlink=1, st_dev=8, st_ino=9)
        latch.lstat.return_value = info
        with mock.patch.object(MANAGER, 'RECOVERY_LATCH', latch), \
                mock.patch.object(MANAGER.os, 'open', return_value=17), \
                mock.patch.object(MANAGER.os, 'fstat', return_value=info), \
                mock.patch.object(MANAGER.os, 'read', return_value=b'wrong\n'), \
                mock.patch.object(MANAGER.os, 'close'):
            with self.assertRaisesRegex(MANAGER.ManagerError, 'invalid recovery Boost latch payload'):
                MANAGER.clear_recovery_latch()
        latch.unlink.assert_not_called()

    def test_manager_rejects_a_root_owned_latch_with_the_wrong_payload(self):
        latch = mock.Mock()
        latch.lstat.return_value = types.SimpleNamespace(
            st_mode=MANAGER.stat.S_IFREG | 0o600,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_dev=8,
            st_ino=9,
        )
        pinned = types.SimpleNamespace(
            st_mode=MANAGER.stat.S_IFREG | 0o600,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_dev=8,
            st_ino=9,
        )

        with mock.patch.object(MANAGER, "RECOVERY_LATCH", latch), \
                mock.patch.object(MANAGER.os, "open", return_value=17), \
                mock.patch.object(MANAGER.os, "fstat", return_value=pinned), \
                mock.patch.object(MANAGER.os, "read", return_value=b"wrong\n"), \
                mock.patch.object(MANAGER.os, "close"):
            with self.assertRaisesRegex(
                    MANAGER.ManagerError, "invalid recovery Boost latch payload"):
                MANAGER.recovery_latch()


class BatchedUnitStateTests(unittest.TestCase):
    def test_two_units_are_parsed_without_relying_on_property_order(self):
        output = (
            'MainPID=17\nId=msi-fan-profile.service\nActiveState=active\n'
            'SubState=running\nJob=\nUnitFileState=enabled\n\n'
            'MainPID=0\nId=msi-fan-profile-keeper.service\nActiveState=inactive\n'
            'SubState=dead\nJob=\nUnitFileState=static\n')
        with mock.patch.object(MANAGER, 'ctl', return_value=mock.Mock(stdout=output)) as ctl:
            states = MANAGER.unit_states(MANAGER.SERVICE, MANAGER.KEEPER)
        self.assertEqual(states[MANAGER.SERVICE]['MainPID'], '17')
        self.assertEqual(states[MANAGER.KEEPER]['ActiveState'], 'inactive')
        self.assertEqual(states[MANAGER.SERVICE]['UnitFileState'], 'enabled')
        ctl.assert_called_once()

    def test_enablement_classification_remains_strict(self):
        for state in ('enabled', 'disabled'):
            with self.subTest(state=state):
                self.assertEqual(MANAGER.validated_enable_state(state), state)
        for state in ('', 'masked', 'masked-runtime', 'static', 'enabled-runtime',
                      'indirect', 'not-found', 'error'):
            with self.subTest(state=state):
                with self.assertRaisesRegex(MANAGER.ManagerError, 'unknown service enable state'):
                    MANAGER.validated_enable_state(state)

    def test_missing_unit_file_state_or_failed_show_is_not_a_status_snapshot(self):
        incomplete = ('MainPID=17\nId=msi-fan-profile.service\nActiveState=active\n'
                      'SubState=running\nJob=\n')
        with mock.patch.object(MANAGER, 'ctl', return_value=mock.Mock(stdout=incomplete)):
            with self.assertRaisesRegex(MANAGER.ManagerError, 'incomplete systemd unit properties'):
                MANAGER.unit_states(MANAGER.SERVICE)
        with mock.patch.object(MANAGER, 'ctl', side_effect=
                               MANAGER.subprocess.CalledProcessError(1, 'systemctl')):
            with self.assertRaises(MANAGER.subprocess.CalledProcessError):
                MANAGER.unit_states(MANAGER.SERVICE)


class StatusReadBudgetTests(unittest.TestCase):
    def exercise_status(self, twice, snapshot_driver=False, snapshot_error=None):
        values = {
            pathlib.Path('/sys/class/dmi/id/product_name'): 'Raider 18 HX AI A2XWIG',
            pathlib.Path('/sys/class/dmi/id/board_name'): 'MS-1824',
            pathlib.Path('/sys/class/dmi/id/bios_version'): 'E1824IMS.310',
            pathlib.Path('/sys/module/msi_ec/version'): '0.13.1',
            pathlib.Path('/sys/module/msi_ec/srcversion'): 'AB0BFAE2391B5ADD66E01BD',
            MANAGER.BASE / 'fw_version': '1824EMS1.108',
            MANAGER.BASE / 'fan_curve': MANAGER.DEFAULT,
            MANAGER.BASE / 'fan_mode': 'advanced',
            MANAGER.BASE / 'cooler_boost': 'off',
            MANAGER.BASE / 'shift_mode': 'comfort',
        }
        reads = collections.Counter()
        if snapshot_driver:
            values[MANAGER.fan_control.SOURCE_PATH] = MANAGER.fan_control.SNAPSHOT_SRCVERSION
            values[MANAGER.BASE / 'fan_control_snapshot'] = (
                f'1 {MANAGER.fan_control.FIRMWARE} comfort advanced off 50 40 {MANAGER.DEFAULT}')

        def fake_read(path):
            path = pathlib.Path(path)
            reads[path] += 1
            if path == MANAGER.BASE / 'fan_control_snapshot' and snapshot_error is not None:
                if isinstance(snapshot_error, Exception):
                    raise snapshot_error
                return snapshot_error
            return values[path]

        output = (
            'Id=msi-fan-profile.service\nActiveState=active\nSubState=running\n'
            'MainPID=17\nJob=\nUnitFileState=enabled\n\n'
            'Id=msi-fan-profile-keeper.service\nActiveState=inactive\nSubState=dead\n'
            'MainPID=0\nJob=\nUnitFileState=static\n')

        def fake_ctl(action, *_args, **_kwargs):
            self.assertEqual(action, 'show', 'status issued an extra enablement command')
            return mock.Mock(stdout=output)

        with mock.patch.object(MANAGER.pathlib.Path, 'exists', return_value=False), \
                mock.patch.object(MANAGER.pathlib.Path, 'is_file', return_value=snapshot_driver), \
                mock.patch.object(MANAGER, 'read', side_effect=fake_read), \
                mock.patch.object(MANAGER, 'ctl', side_effect=fake_ctl) as ctl, \
                mock.patch.object(MANAGER, 'marker', return_value=False), \
                mock.patch.object(MANAGER, 'recovery_latch_metadata', return_value=False), \
                mock.patch.object(MANAGER, 'health_snapshot', return_value={
                    'status': 'fresh', 'state': 'normal'}), \
                mock.patch.object(MANAGER.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            if twice:
                self.assertEqual(MANAGER.status(), 0)
                snapshot = json.loads(stream.getvalue())
            else:
                snapshot = MANAGER.full_status()
        return reads, ctl.call_count, snapshot

    def test_full_status_reuses_each_validated_firmware_and_module_read(self):
        reads, calls, snapshot = self.exercise_status(twice=False)
        for path in (MANAGER.BASE / 'fw_version',
                     pathlib.Path('/sys/module/msi_ec/version'),
                     pathlib.Path('/sys/module/msi_ec/srcversion')):
            self.assertEqual(reads[path], 1)
        self.assertEqual(calls, 1)
        self.assertEqual(snapshot['ec_firmware'], '1824EMS1.108')
        self.assertEqual(snapshot['driver_version'], '0.13.1')
        self.assertTrue(snapshot['service_enabled'])

    def test_stable_two_snapshot_status_uses_only_two_systemctl_calls(self):
        reads, calls, snapshot = self.exercise_status(twice=True)
        self.assertEqual(calls, 2)
        self.assertEqual(reads[MANAGER.BASE / 'fw_version'], 2)
        self.assertEqual(reads[MANAGER.BASE / 'fan_curve'], 2)
        self.assertTrue(snapshot['snapshot_stable'])
        self.assertTrue(snapshot['configuration_valid'])

    def test_new_abi_status_uses_one_snapshot_and_no_legacy_control_reads(self):
        reads, calls, snapshot = self.exercise_status(twice=False, snapshot_driver=True)
        self.assertEqual(reads[MANAGER.BASE / 'fan_control_snapshot'], 1)
        for name in ('fw_version', 'fan_curve', 'fan_mode', 'cooler_boost', 'shift_mode'):
            self.assertEqual(reads[MANAGER.BASE / name], 0, f'legacy read: {name}')
        self.assertEqual(calls, 1)
        self.assertEqual(snapshot['curve'], 'candidate14')
        self.assertEqual(snapshot['fan_mode'], 'advanced')
        self.assertEqual(snapshot['cooler_boost'], 'off')
        self.assertEqual(snapshot['shift_mode'], 'comfort')
        self.assertEqual(snapshot['ec_firmware'], MANAGER.fan_control.FIRMWARE)
        self.assertEqual(snapshot['driver_srcversion'], MANAGER.fan_control.SNAPSHOT_SRCVERSION)

    def test_new_abi_malformed_or_failed_snapshot_never_falls_back(self):
        for error in ('partial', OSError('snapshot read failed')):
            with self.subTest(error=error), mock.patch.object(MANAGER, 'hw') as hardware:
                with self.assertRaises((MANAGER.ManagerError, OSError)):
                    self.exercise_status(twice=False, snapshot_driver=True, snapshot_error=error)
                hardware.assert_not_called()

    def test_shared_identity_errors_keep_the_manager_error_boundary(self):
        with mock.patch.object(MANAGER.fan_control, 'identity', side_effect=
                               MANAGER.fan_control.ControlABIError('unsupported exact driver build')):
            with self.assertRaisesRegex(MANAGER.ManagerError, 'unsupported exact driver build'):
                MANAGER.identity()

    def test_write_guard_still_revalidates_identity_on_every_control_write(self):
        with mock.patch.object(MANAGER, 'identity', side_effect=[
                {}, MANAGER.ManagerError('identity changed')]) as identity, \
                mock.patch.object(MANAGER.pathlib.Path, 'open') as open_file, \
                mock.patch.object(MANAGER, 'read', return_value='on'):
            MANAGER.write(MANAGER.BASE / 'cooler_boost', 'on')
            with self.assertRaisesRegex(MANAGER.ManagerError, 'identity changed'):
                MANAGER.write(MANAGER.BASE / 'cooler_boost', 'on')
        self.assertEqual(identity.call_count, 2)
        open_file.assert_called_once()


class ControllerHealthTests(unittest.TestCase):
    def snapshot(self, payload, now=10_000_000_000):
        with mock.patch.object(MANAGER, 'private_file_bytes',
                               return_value=json.dumps(payload).encode()), \
                mock.patch.object(MANAGER.time, 'monotonic_ns', return_value=now):
            return MANAGER.health_snapshot(17)

    def test_fresh_health_reports_sample_age_and_last_valid_age(self):
        health = self.snapshot(health_payload())
        self.assertEqual(health['status'], 'fresh')
        self.assertEqual(health['age_seconds'], 1.0)
        self.assertEqual(health['last_valid_age_seconds'], 2.0)

    def test_status_distinguishes_normal_cooling_degraded_and_stale(self):
        for state, age, expected in (
                ('normal', 1, 0), ('cooling', 1, 0),
                ('degraded', 1, 1), ('normal', 6, 1)):
            with self.subTest(state=state, age=age):
                raw = health_payload(state=state)
                snapshot = managed_snapshot()
                with mock.patch.object(MANAGER, 'full_status', return_value=snapshot), \
                        mock.patch.object(MANAGER, 'private_file_bytes',
                                          return_value=json.dumps(raw).encode()), \
                        mock.patch.object(MANAGER.time, 'monotonic_ns',
                                          return_value=(9 + age) * 1_000_000_000), \
                        mock.patch.object(MANAGER.time, 'sleep'), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(MANAGER.status(), expected)
                report = json.loads(output.getvalue())
                self.assertTrue(report['snapshot_stable'])
                self.assertTrue(report['configuration_valid'])
                self.assertEqual(report['health']['state'], state)

    def test_cross_pid_and_future_or_invalid_timestamp_health_is_rejected(self):
        for updates in ({'pid': 18}, {'sampled_monotonic_ns': 11_000_000_000},
                        {'last_valid_monotonic_ns': 11_000_000_000}):
            with self.subTest(updates=updates):
                self.assertEqual(self.snapshot(health_payload(**updates))['status'], 'invalid')

    def test_invalid_channel_is_visible_without_discarding_known_channels(self):
        payload = health_payload(state='degraded', reason='EC CPU unavailable',
                                 channel_errors={'ec_cpu': '255C sentinel'})
        payload['telemetry']['ec_cpu_celsius'] = None
        health = self.snapshot(payload)
        self.assertEqual(health['status'], 'fresh')
        self.assertEqual(health['telemetry']['package_millidegrees'], 50000)
        self.assertIsNone(health['telemetry']['ec_cpu_celsius'])
        self.assertEqual(health['channel_errors'], {'ec_cpu': '255C sentinel'})

    def test_symlink_health_is_rejected_before_opening_it(self):
        path = mock.Mock()
        path.lstat.return_value = types.SimpleNamespace(
            st_mode=MANAGER.stat.S_IFLNK | 0o777, st_uid=0, st_gid=0, st_nlink=1)
        with mock.patch.object(MANAGER, 'HEALTH', path), \
                mock.patch.object(MANAGER.os, 'open') as open_file:
            health = MANAGER.health_snapshot(17)
        self.assertEqual(health['status'], 'invalid')
        open_file.assert_not_called()

    def test_unsafe_health_owner_permissions_and_links_are_rejected(self):
        for uid, gid, mode, links in ((1000, 0, 0o644, 1), (0, 1000, 0o644, 1),
                                     (0, 0, 0o666, 1), (0, 0, 0o644, 2)):
            path = mock.Mock()
            path.lstat.return_value = types.SimpleNamespace(
                st_mode=MANAGER.stat.S_IFREG | mode, st_uid=uid, st_gid=gid,
                st_nlink=links)
            with self.subTest(uid=uid, gid=gid, mode=mode, links=links), \
                    mock.patch.object(MANAGER, 'HEALTH', path), \
                    mock.patch.object(MANAGER.os, 'open') as open_file:
                self.assertEqual(MANAGER.health_snapshot(17)['status'], 'invalid')
                open_file.assert_not_called()

    def test_atomic_publication_replacement_is_retried_once(self):
        with mock.patch.object(MANAGER, 'private_file_bytes', side_effect=[
                MANAGER.ManagerError('controller health file changed during validation'),
                json.dumps(health_payload()).encode()]) as read_file, \
                mock.patch.object(MANAGER.time, 'monotonic_ns', return_value=10_000_000_000):
            self.assertEqual(MANAGER.health_snapshot(17)['status'], 'fresh')
        self.assertEqual(read_file.call_count, 2)

    def test_root_owned_readable_publication_and_nullable_reason_are_valid(self):
        path = mock.Mock()
        info = types.SimpleNamespace(st_mode=MANAGER.stat.S_IFREG | 0o644,
                                     st_uid=0, st_gid=0, st_nlink=1, st_dev=8, st_ino=9)
        path.lstat.return_value = info
        payload = json.dumps(health_payload(reason=None)).encode()
        with mock.patch.object(MANAGER, 'HEALTH', path), \
                mock.patch.object(MANAGER.os, 'open', return_value=17), \
                mock.patch.object(MANAGER.os, 'fstat', return_value=info), \
                mock.patch.object(MANAGER.os, 'read', return_value=payload), \
                mock.patch.object(MANAGER.os, 'close'), \
                mock.patch.object(MANAGER.time, 'monotonic_ns', return_value=10_000_000_000):
            health = MANAGER.health_snapshot(17)
        self.assertEqual(health['status'], 'fresh')
        self.assertIsNone(health['reason'])

    def test_incomplete_schema_boolean_version_or_nonfinite_cooldown_is_invalid(self):
        incomplete = health_payload()
        del incomplete['last_valid_monotonic_ns']
        for payload in (incomplete, health_payload(schema=True),
                        health_payload(cooldown_elapsed_seconds=float('nan'))):
            with self.subTest(payload=payload):
                self.assertEqual(self.snapshot(payload)['status'], 'invalid')

    def test_missing_health_and_other_pid_do_not_appear_healthy(self):
        for publication in (FileNotFoundError(), json.dumps(health_payload(pid=18)).encode()):
            with self.subTest(publication=publication):
                kwargs = ({'side_effect': publication} if isinstance(publication, Exception)
                          else {'return_value': publication})
                with mock.patch.object(MANAGER, 'full_status', return_value=managed_snapshot()), \
                        mock.patch.object(MANAGER, 'private_file_bytes', **kwargs), \
                        mock.patch.object(MANAGER.time, 'sleep'), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(MANAGER.status(), 1)

    def test_factory_override_does_not_require_a_running_daemon_health_file(self):
        snapshot = managed_snapshot()
        snapshot.update(service_active_state='inactive', service_sub_state='dead',
                        service_main_pid=0, factory_override=True,
                        curve='factory', fan_mode='auto', cooler_boost='off')
        with mock.patch.object(MANAGER, 'full_status', return_value=snapshot), \
                mock.patch.object(MANAGER, 'health_snapshot') as health, \
                mock.patch.object(MANAGER.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(MANAGER.status(), 0)
        health.assert_not_called()


class ManagerRequestTests(unittest.TestCase):
    def test_ack_rejects_the_wrong_pid_stale_timestamp_and_wrong_action(self):
        for response in (
                {'pid': 18, 'action': 'boost-release', 'monotonic_ns': 101},
                {'pid': 17, 'action': 'boost-release', 'monotonic_ns': 99},
                {'pid': 17, 'action': 'boost-on', 'monotonic_ns': 101}):
            with self.subTest(response=response), \
                    mock.patch.object(MANAGER, 'acknowledgement', return_value=response):
                self.assertFalse(MANAGER.ack('boost-release', 17, 100))

    def test_waiting_for_release_leaves_lock_available_for_boost_on(self):
        held = False
        waiters = []

        def acquire(_path):
            nonlocal held
            self.assertFalse(held, 'waiting release retained the manager lock')
            held = True
            return 9

        def close(_descriptor):
            nonlocal held
            held = False

        def wait(action, *_args):
            self.assertFalse(held)
            waiters.append(action)
            if action == 'boost-release':
                MANAGER.signal_action('boost-on', MANAGER.signal.SIGUSR1, 25)

        with mock.patch.object(MANAGER, 'root'), \
                mock.patch.object(MANAGER, 'identity'), \
                mock.patch.object(MANAGER, 'lock', side_effect=acquire), \
                mock.patch.object(MANAGER.os, 'close', side_effect=close), \
                mock.patch.object(MANAGER, 'full_status', return_value=managed_snapshot()), \
                mock.patch.object(MANAGER, 'ctl'), \
                mock.patch.object(MANAGER, 'wait_for_request', side_effect=wait):
            MANAGER.signal_action('boost-release', MANAGER.signal.SIGUSR2, None)
        self.assertEqual(waiters, ['boost-release', 'boost-on'])
        self.assertFalse(held)

    def test_wait_detects_service_failure_or_daemon_replacement(self):
        for state in (
                {'ActiveState': 'failed', 'SubState': 'failed', 'MainPID': '0'},
                {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '18'}):
            with self.subTest(state=state), \
                    mock.patch.object(MANAGER, 'unit_state', return_value=state), \
                    mock.patch.object(MANAGER, 'read') as read, \
                    mock.patch.object(MANAGER, 'hw') as hardware:
                with self.assertRaisesRegex(MANAGER.ManagerError, 'failed or was replaced'):
                    MANAGER.wait_for_request('boost-release', 17, 100, None)
                read.assert_not_called()
                hardware.assert_not_called()

    def test_thirty_second_pending_wait_has_only_one_terminal_boost_read(self):
        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '17'}
        queued = {'pid': 17, 'action': 'boost-release-queued', 'monotonic_ns': 101}
        terminal = {'pid': 17, 'action': 'boost-release', 'monotonic_ns': 102}
        now = 0.0

        def sleep(seconds):
            nonlocal now
            now += seconds

        def fake_read(path):
            self.assertEqual(now, 30.0, 'EC read happened during pending cooldown')
            self.assertEqual(path, MANAGER.BASE / 'cooler_boost')
            return 'off'

        with mock.patch.object(MANAGER, 'unit_state', return_value=state) as unit, \
                mock.patch.object(MANAGER, 'acknowledgement', side_effect=
                                  [None] * 60 + [queued] * 60 + [terminal]), \
                mock.patch.object(MANAGER, 'read', side_effect=fake_read) as read, \
                mock.patch.object(MANAGER, 'hw') as hardware, \
                mock.patch.object(MANAGER.time, 'sleep', side_effect=sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            MANAGER.wait_for_request('boost-release', 17, 100, None)
        self.assertEqual(unit.call_count, 121)
        read.assert_called_once_with(MANAGER.BASE / 'cooler_boost')
        hardware.assert_not_called()

    def test_queue_then_newer_boost_cancels_without_ec_reads(self):
        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '17'}
        queued = {'pid': 17, 'action': 'boost-release-queued', 'monotonic_ns': 101}
        newer_boost = {'pid': 17, 'action': 'boost-on', 'monotonic_ns': 102}
        health = {'status': 'fresh', 'release_pending': False, 'sampled_monotonic_ns': 103}
        with mock.patch.object(MANAGER, 'unit_state', return_value=state), \
                mock.patch.object(MANAGER, 'acknowledgement', side_effect=[queued, newer_boost]), \
                mock.patch.object(MANAGER, 'read') as read, \
                mock.patch.object(MANAGER, 'hw') as hardware, \
                mock.patch.object(MANAGER, 'health_snapshot', return_value=health), \
                mock.patch.object(MANAGER.time, 'sleep'):
            with self.assertRaisesRegex(MANAGER.ManagerError, 'cancelled'):
                MANAGER.wait_for_request('boost-release', 17, 100, None)
        read.assert_not_called()
        hardware.assert_not_called()

    def test_stale_ack_and_wrong_pid_are_ignored_until_matching_release(self):
        responses = [
            {'pid': 17, 'action': 'boost-release', 'monotonic_ns': 99},
            {'pid': 18, 'action': 'boost-release', 'monotonic_ns': 101},
            {'pid': 17, 'action': 'boost-release', 'monotonic_ns': 102},
        ]
        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '17'}
        with mock.patch.object(MANAGER, 'unit_state', return_value=state), \
                mock.patch.object(MANAGER, 'acknowledgement', side_effect=responses), \
                mock.patch.object(MANAGER, 'read', return_value='off') as read, \
                mock.patch.object(MANAGER, 'hw') as hardware, \
                mock.patch.object(MANAGER.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            MANAGER.wait_for_request('boost-release', 17, 100, None)
        self.assertEqual(sleep.call_count, 2)
        read.assert_called_once_with(MANAGER.BASE / 'cooler_boost')
        hardware.assert_not_called()

    def test_newer_boost_on_cancels_only_when_daemon_confirms_no_release_pending(self):
        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '17'}
        response = {'pid': 17, 'action': 'boost-on', 'monotonic_ns': 101}
        health = {'status': 'fresh', 'release_pending': False, 'sampled_monotonic_ns': 102}
        with mock.patch.object(MANAGER, 'unit_state', return_value=state), \
                mock.patch.object(MANAGER, 'acknowledgement', return_value=response), \
                mock.patch.object(MANAGER, 'read') as read, \
                mock.patch.object(MANAGER, 'hw') as hardware, \
                mock.patch.object(MANAGER, 'health_snapshot', return_value=health):
            with self.assertRaisesRegex(MANAGER.ManagerError, 'cancelled'):
                MANAGER.wait_for_request('boost-release', 17, 100, None)
        read.assert_not_called()
        hardware.assert_not_called()

    def test_deferred_boost_ack_does_not_cancel_a_still_pending_release(self):
        state = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '17'}
        responses = [
            {'pid': 17, 'action': 'boost-on', 'monotonic_ns': 101},
            {'pid': 17, 'action': 'boost-release', 'monotonic_ns': 103},
        ]
        health = {'status': 'fresh', 'release_pending': True, 'sampled_monotonic_ns': 102}
        with mock.patch.object(MANAGER, 'unit_state', return_value=state), \
                mock.patch.object(MANAGER, 'acknowledgement', side_effect=responses), \
                mock.patch.object(MANAGER, 'read', return_value='off') as read, \
                mock.patch.object(MANAGER, 'hw') as hardware, \
                mock.patch.object(MANAGER, 'health_snapshot', return_value=health), \
                mock.patch.object(MANAGER.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            MANAGER.wait_for_request('boost-release', 17, 100, None)
        read.assert_called_once_with(MANAGER.BASE / 'cooler_boost')
        hardware.assert_not_called()


class FactoryReleasePlausibilityTests(unittest.TestCase):
    def test_negative_package_or_ec_telemetry_never_releases_boost(self):
        core = mock.MagicMock()
        (core / 'name').exists.return_value = True
        for invalid in (('-1', '50', '40'), ('50000', '-1', '40'), ('50000', '50', '-1')):
            with self.subTest(telemetry=invalid), \
                    mock.patch.object(MANAGER.pathlib.Path, 'glob', return_value=[core]), \
                    mock.patch.object(MANAGER, 'read', side_effect=['coretemp', *invalid]), \
                    mock.patch.object(MANAGER, 'hw', return_value={
                        'raw': MANAGER.FACTORY, 'mode': 'auto', 'boost': 'on'}), \
                    mock.patch.object(MANAGER, 'write') as write:
                with self.assertRaisesRegex(MANAGER.ManagerError, 'telemetry is invalid'):
                    MANAGER.restore_factory_locked()
            self.assertNotIn(mock.call(MANAGER.BASE / 'cooler_boost', 'off'), write.call_args_list)


if __name__ == "__main__":
    unittest.main()
