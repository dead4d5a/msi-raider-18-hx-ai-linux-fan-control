#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Static lifecycle invariants for the main controller and failure keeper."""

from __future__ import annotations

import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
PRIMARY = "msi-fan-profile.service"
KEEPER = "msi-fan-profile-keeper.service"

# Safety-relevant directives shared by both units. A later assignment of the same
# key would override these in systemd, so each key must also appear only once.
COMMON_DIRECTIVES = (
    ("Unit", "RequiresMountsFor", "/usr/local /usr"),
    ("Service", "Type", "notify"),
    ("Service", "NotifyAccess", "main"),
    ("Service", "WatchdogSec", "5s"),
    ("Service", "WatchdogSignal", "SIGKILL"),
    ("Service", "TimeoutStartSec", "30s"),
    ("Service", "TimeoutStopSec", "30s"),
    ("Service", "KillMode", "control-group"),
    ("Service", "SendSIGKILL", "yes"),
    ("Service", "NoNewPrivileges", "yes"),
    ("Service", "PrivateTmp", "yes"),
    ("Service", "ProtectSystem", "strict"),
    ("Service", "ProtectHome", "yes"),
    ("Service", "ProtectKernelModules", "yes"),
    ("Service", "ProtectControlGroups", "yes"),
    ("Service", "LockPersonality", "yes"),
    ("Service", "RestrictRealtime", "yes"),
    ("Service", "RestrictAddressFamilies", "AF_UNIX"),
    ("Service", "DevicePolicy", "closed"),
    # A hard, swap-free leak tripwire; reclaim and global-OOM preference only.
    ("Service", "MemoryMin", "32M"),
    ("Service", "MemoryMax", "64M"),
    ("Service", "MemorySwapMax", "0"),
    ("Service", "OOMScoreAdjust", "-900"),
    ("Service", "TasksMax", "16"),
    ("Service", "UMask", "0077"),
)
UNIT_DIRECTIVES = {
    PRIMARY: COMMON_DIRECTIVES + (
        ("Unit", "After", "systemd-modules-load.service systemd-tmpfiles-setup.service"),
        ("Unit", "OnFailure", KEEPER),
        ("Service", "ExecStart", "/usr/local/libexec/msi-fan-profiled"),
        ("Service", "ExecStopPost", "+/usr/local/libexec/msi-gpu-recover candidate14"),
        ("Service", "Restart", "no"),
        ("Install", "WantedBy", "multi-user.target"),
    ),
    KEEPER: COMMON_DIRECTIVES + (
        ("Unit", "Conflicts", PRIMARY),
        ("Unit", "Before", PRIMARY),
        ("Unit", "StartLimitIntervalSec", "15min"),
        ("Unit", "StartLimitBurst", "3"),
        ("Service", "ExecStart", "/usr/local/libexec/msi-gpu-recover --keeper candidate14"),
        ("Service", "Restart", "on-failure"),
        ("Service", "RestartSec", "30s"),
    ),
}
ENVIRONMENT = ["MSI_CALIBRATION_PROFILE=candidate14", "MSI_FAN_TIMING_METRICS=0"]
# A condition skip is not a failure and never fires OnFailure. Only the operator's
# factory-auto marker may skip the primary; a missing driver must fail visibly.
CONDITIONS = {
    PRIMARY: [("ConditionPathExists", "!/run/msi-fan-profile.factory-auto")],
    KEEPER: [("ConditionPathExists", "/sys/devices/platform/msi-ec/fan_curve"),
             ("ConditionPathExists", "!/run/msi-fan-profile.factory-auto")],
}
# Read-only /sys breaks every EC write; MemoryHigh throttling starves the watchdog.
FORBIDDEN_KEYS = ("ProtectKernelTunables", "PrivateDevices", "ReadOnlyPaths",
                  "InaccessiblePaths", "MemoryHigh")
PROGRAMS = ("src/msi-fan-profile", "src/msi-fan-profiled", "src/msi-gpu-recover")


def directives(filename):
    """Parse a unit into ordered (section, key, value) assignments."""
    section = None
    parsed = []
    for line in (ROOT / "systemd" / filename).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if section is None or not separator or line.endswith("\\"):
            raise AssertionError(f"unsupported unit line in {filename}: {line!r}")
        parsed.append((section, key.strip(), value.strip()))
    return parsed


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


class SafetyDirectiveGoldenTests(unittest.TestCase):
    def test_each_safety_directive_is_present_exactly_once(self):
        for filename, wanted in UNIT_DIRECTIVES.items():
            parsed = directives(filename)
            for section, key, value in wanted:
                with self.subTest(unit=filename, key=key):
                    assigned = [item for item in parsed if item[:2] == (section, key)]
                    self.assertEqual(assigned, [(section, key, value)])

    def test_environment_is_exactly_the_profile_and_disabled_diagnostics(self):
        for filename in UNIT_DIRECTIVES:
            with self.subTest(unit=filename):
                self.assertEqual(
                    [value for section, key, value in directives(filename)
                     if (section, key) == ("Service", "Environment")], ENVIRONMENT)

    def test_conditions_cannot_silently_skip_the_primary_controller(self):
        for filename, wanted in CONDITIONS.items():
            with self.subTest(unit=filename):
                conditions = [(key, value) for _, key, value in directives(filename)
                              if key.startswith(("Condition", "Assert"))]
                self.assertEqual(conditions, wanted)
        self.assertNotIn(("Unit", "ConditionPathExists",
                          "/sys/devices/platform/msi-ec/fan_curve"), directives(PRIMARY))

    def test_directives_that_break_ec_control_or_hide_faults_are_absent(self):
        for filename in UNIT_DIRECTIVES:
            parsed = directives(filename)
            for key in FORBIDDEN_KEYS:
                with self.subTest(unit=filename, key=key):
                    self.assertNotIn(key, [item[1] for item in parsed])
        primary = directives(PRIMARY)
        self.assertEqual([value for _, key, value in primary if key == "Restart"], ["no"])
        self.assertNotIn("RestartSec", [item[1] for item in primary])
        keeper = directives(KEEPER)
        for key in ("ExecStopPost", "OnFailure"):
            self.assertNotIn(key, [item[1] for item in keeper])
        self.assertNotIn("Install", {item[0] for item in keeper})

    def test_unit_programs_pin_the_system_interpreter_in_isolated_mode(self):
        for program in PROGRAMS:
            with self.subTest(program=program):
                first = (ROOT / program).read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(first, "#!/usr/bin/python3 -I")


if __name__ == "__main__":
    unittest.main()
