#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute the uninstall's Cooler Boost release against temporary files and mocked services."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
FACTORY = "58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75"
DEFAULT = "50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120"
LEGACY_SRCVERSION = "AB0BFAE2391B5ADD66E01BD"
# Coretemp package (millidegrees), EC CPU and EC GPU readings for one sample.
COOL = ("59000", "60", "44")
HOT = ("90000", "60", "44")
ROOT_GUARD = "[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }"
HELPER = '/usr/bin/python3 -I -B "$repo/src/msi_fan_control.py" confirm-cool'
# An absolute host reference left after relocation would bypass PATH shims.
HOST_REFERENCE = re.compile(
    r"(?<![\w./-])/(?:bin|boot|dev|etc|lib|lib64|proc|run|sbin|sys|usr|var)(?![\w-])")
# Everything the uninstall removes once Boost is released, relative to the sandbox.
TARGETS = (
    "etc/systemd/system/msi-fan-profile.service",
    "etc/systemd/system/msi-fan-profile-keeper.service",
    "etc/tmpfiles.d/msi-fan-profile.conf",
    "etc/kernel/postinst.d/zz-msi-fan-profile-readiness",
    "usr/local/sbin/msi-fan-profile",
    "usr/local/libexec/msi-fan-profiled",
    "usr/local/libexec/msi-gpu-recover",
    "usr/local/libexec/msi_fan_control.py",
    "usr/local/libexec/msi-fan-kernel-readiness",
    "usr/local/share/doc/msi-fan-profile/README.md",
)

# Every privileged command is replaced and every host path is relocated
# beneath TemporaryDirectory; PATH holds only these mocks and real bash, cat,
# dirname, rm and python3. No test can reach the real controller.
COMMAND_SHIM = textwrap.dedent(r'''\
#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import stat
import sys

root = pathlib.Path(os.environ["UNINSTALL_TEST_ROOT"])
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([command, *args]) + "\n")
state_path = root / "state.json"
state = json.loads(state_path.read_text())
if command == "systemctl":
    if args[0] == "disable":
        state["enabled"] = "disabled"
    elif args[0] == "is-enabled":
        print(state["enabled"])
    elif args[0] == "show":
        print({"ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
               "Job": ""}[args[args.index("-p") + 1]])
    state_path.write_text(json.dumps(state))
elif command == "stat":
    path = pathlib.Path(args[2])
    info = path.stat()
    kind = "regular file" if info.st_size else "regular empty file"
    print(f"{kind}:root:root:{stat.S_IMODE(info.st_mode):o}")
elif command == "install":
    mode, paths, index = 0o755, [], 0
    while index < len(args):
        if args[index] in ("-o", "-g", "-m"):
            if args[index] == "-m":
                mode = int(args[index + 1], 8)
            index += 2
            continue
        paths.append(pathlib.Path(args[index]))
        index += 1
    source, destination = paths
    shutil.copyfile(source, destination)
    destination.chmod(mode)
''').lstrip("\\\n")

# Stands in for "$repo/src/msi_fan_control.py": it runs the real helper's
# confirm-cool on a fake clock. Every sleep advances to the next scripted
# sample, as the sysfs files would change; the last sample persists. On the
# first sleep it can also change the platform, as a keyboard toggle or a
# concurrent driver reload would.
HELPER_SHIM = textwrap.dedent(r'''\
import importlib.util
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["UNINSTALL_TEST_ROOT"])
base = root / "sys/devices/platform/msi-ec"
samples = json.loads(os.environ["UNINSTALL_TEST_SAMPLES"])
during = os.environ.get("UNINSTALL_TEST_DURING_WAIT", "").split(",")
spec = importlib.util.spec_from_file_location("msi_fan_control",
                                              os.environ["UNINSTALL_TEST_HELPER"])
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)
clock = {"now": 0.0, "slept": 0}

def write_sample(index):
    for path, reading in zip((root / "sys/class/hwmon/hwmon8/temp1_input",
                              base / "cpu/realtime_temperature",
                              base / "gpu/realtime_temperature"),
                             samples[min(index, len(samples) - 1)]):
        if reading is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(reading + "\n")

def sleep(seconds):
    clock["now"] += seconds
    clock["slept"] += 1
    write_sample(clock["slept"])
    if clock["slept"] == 1:
        if "boost-off" in during:
            (base / "cooler_boost").write_text("off\n")
        if "srcversion" in during:
            (root / "sys/module/msi_ec/srcversion").write_text("0000000000000000000000\n")
        if "ec_sys" in during:
            (root / "sys/module/ec_sys").mkdir()

write_sample(0)
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(["confirm-cool", *sys.argv[2:], {
        name: (base / name).read_text().strip()
        for name in ("fan_curve", "fan_mode", "cooler_boost")}]) + "\n")
control.time.monotonic = lambda: clock["now"]
control.time.sleep = sleep
code = control.main(sys.argv[1:])
with (root / "confirmation.json").open("w") as stream:
    json.dump({"elapsed": clock["now"], "code": code}, stream)
raise SystemExit(code)
''').lstrip("\\\n")

ALLOWED_TOOLS = ("bash", "cat", "dirname", "python3", "rm")
MOCKED_TOOLS = ("flock", "install", "stat", "systemctl")


class UninstallSandboxTestCase(unittest.TestCase):
    def run_uninstall(self, samples, *, during="", platform=None):
        """Run the sandboxed uninstall.

        samples holds one (package, EC CPU, EC GPU) reading per confirmation
        sample; the last one persists, and None leaves that file missing.
        during changes the platform at the first confirmation wait; it is a
        comma-separated list of boost-off, srcversion and ec_sys. platform
        overrides initial sysfs values by sandbox-relative path.
        """
        platform = platform or {}
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = pathlib.Path(temporary)
            release = sandbox / "release"
            (release / "scripts").mkdir(parents=True)
            (release / "src").mkdir()
            (sandbox / "run").mkdir()
            base = sandbox / "sys/devices/platform/msi-ec"
            for relative, value in (
                    ("sys/class/dmi/id/product_name", "Raider 18 HX AI A2XWIG"),
                    ("sys/class/dmi/id/board_name", "MS-1824"),
                    ("sys/class/dmi/id/bios_version", "E1824IMS.310"),
                    ("sys/module/msi_ec/version", "0.13.1"),
                    ("sys/module/msi_ec/srcversion", LEGACY_SRCVERSION),
                    ("sys/devices/platform/msi-ec/fw_version", "1824EMS1.108"),
                    # The running Candidate14 controller's state before the stop.
                    ("sys/devices/platform/msi-ec/fan_curve", DEFAULT),
                    ("sys/devices/platform/msi-ec/fan_mode", "advanced"),
                    ("sys/devices/platform/msi-ec/cooler_boost", "off"),
                    ("sys/class/hwmon/hwmon0/name", "acpitz"),
                    ("sys/class/hwmon/hwmon0/temp1_input", "99000"),
                    ("sys/class/hwmon/hwmon8/name", "coretemp"),
                    *((target, "installed artifact") for target in TARGETS)):
                path = sandbox / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(platform.get(relative, value) + "\n")
            for channel in ("cpu", "gpu"):
                (base / channel).mkdir()
            (sandbox / "state.json").write_text(json.dumps({"enabled": "enabled"}))
            (sandbox / "calls.jsonl").touch()

            # Keep all control flow intact; relocate host paths and bypass only
            # the root guard, since ownership and privileged commands are mocked.
            script = (ROOT / "scripts/uninstall-profile.sh").read_text()
            self.assertIn(ROOT_GUARD, script)
            self.assertEqual(script.count(HELPER), 1)
            script = script.replace(ROOT_GUARD, ": # root check mocked")
            script = script.replace(HELPER, HELPER.replace("/usr/bin/python3", "python3"))
            # One pass, so a relocated path is never relocated again.
            script = re.sub(r"(?<![\w./-])/(?=(?:usr/local|etc|run|sys)/)", str(sandbox) + "/",
                            script)
            body = "\n".join(line for line in script.splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertIsNone(HOST_REFERENCE.search(body.replace("/dev/null", "")))
            script_path = release / "scripts/uninstall-profile.sh"
            script_path.write_text(script)
            (release / "src/msi_fan_control.py").write_text(HELPER_SHIM)

            mock_bin = sandbox / "mock-bin"
            mock_bin.mkdir()
            for name in MOCKED_TOOLS:
                path = mock_bin / name
                path.write_text(COMMAND_SHIM)
                path.chmod(0o755)
            allowed_bin = sandbox / "allowed-bin"
            allowed_bin.mkdir()
            for name in ALLOWED_TOOLS:
                tool = shutil.which(name)
                self.assertIsNotNone(tool, name)
                (allowed_bin / name).symlink_to(tool)
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("UNINSTALL_TEST_", "PYTHON"))}
            environment.update(
                PATH=str(mock_bin) + os.pathsep + str(allowed_bin),
                UNINSTALL_TEST_ROOT=str(sandbox),
                UNINSTALL_TEST_HELPER=str(ROOT / "src/msi_fan_control.py"),
                UNINSTALL_TEST_SAMPLES=json.dumps(samples),
                UNINSTALL_TEST_DURING_WAIT=during)
            result = subprocess.run(["bash", str(script_path)], env=environment, text=True,
                                    capture_output=True, timeout=30)
            self.calls = [json.loads(line) for line in
                          (sandbox / "calls.jsonl").read_text().splitlines()]
            confirmation = sandbox / "confirmation.json"
            self.confirmation = (json.loads(confirmation.read_text())
                                 if confirmation.exists() else None)
            self.ec = {name: (base / name).read_text().strip()
                       for name in ("fan_curve", "fan_mode", "cooler_boost")}
            self.remaining = [target for target in TARGETS if (sandbox / target).exists()]
            self.runtime = sorted(path.name for path in (sandbox / "run").iterdir())
            return result


class UninstallBoostReleaseTests(UninstallSandboxTestCase):
    """The uninstall restores the factory envelope with Cooler Boost on, then
    switches Boost off and removes the profile only after the shared helper
    confirms six consecutive valid, cool samples."""

    def assert_confirmed_after_factory_restore(self):
        confirmations = [call for call in self.calls if call[0] == "confirm-cool"]
        self.assertEqual(len(confirmations), 1)
        # The helper samples the relocated sysfs paths only after the factory
        # curve, auto mode and Cooler Boost on were written and verified.
        self.assertRegex(confirmations[0][1], r"/sys/class/hwmon$")
        self.assertRegex(confirmations[0][2], r"/sys/devices/platform/msi-ec$")
        self.assertEqual(confirmations[0][3], {"fan_curve": FACTORY, "fan_mode": "auto",
                                               "cooler_boost": "on"})
        self.assertEqual(self.calls.index(confirmations[0]),
                         max(index for index, call in enumerate(self.calls)
                             if call[0] == "flock") + 1)

    def assert_refused(self, result, *messages):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_confirmed_after_factory_restore()
        for message in messages:
            self.assertIn(message, result.stderr)
        self.assertIn("Files were not removed.", result.stderr)
        self.assertNotIn("Profile service removed", result.stdout)
        self.assertEqual(self.remaining, list(TARGETS))
        self.assertIn("msi-fan-profile.factory-auto", self.runtime)
        self.assertNotIn(["systemctl", "daemon-reload"], self.calls)
        self.assertEqual((self.ec["fan_curve"], self.ec["fan_mode"]), (FACTORY, "auto"))

    def test_heat_or_junk_keeps_boost_on_and_removes_nothing(self):
        for samples in ([HOT], [("", "60", "44")], [("59000", "255", "44")],
                        [(None, "60", "44")], [HOT, COOL, COOL, HOT]):
            with self.subTest(samples=samples):
                result = self.run_uninstall(samples)
                self.assert_refused(
                    result, "Cooler Boost release not confirmed: no 6 consecutive valid cool",
                    "too hot to release Cooler Boost, or its temperature telemetry is invalid")
                self.assertEqual(self.ec["cooler_boost"], "on")
                self.assertEqual(self.confirmation["code"], 1)
                self.assertLessEqual(self.confirmation["elapsed"], 10.0)

    def test_refusal_switches_boost_back_on_after_a_toggle_during_the_wait(self):
        result = self.run_uninstall([HOT], during="boost-off")
        self.assert_refused(result, "too hot to release Cooler Boost")
        self.assertEqual(self.ec["cooler_boost"], "on")

    def test_cool_confirmation_releases_boost_and_removes_the_profile(self):
        for samples in ([COOL], [("59000", "60", "0")], [HOT, COOL],
                        [("59000", "255", "44"), COOL]):
            with self.subTest(samples=samples):
                result = self.run_uninstall(samples)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_confirmed_after_factory_restore()
                self.assertIn("Cooler Boost release confirmed: 6 consecutive", result.stderr)
                self.assertIn("Profile service removed; exact factory auto/off verified.",
                              result.stdout)
                self.assertEqual(self.ec, {"fan_curve": FACTORY, "fan_mode": "auto",
                                           "cooler_boost": "off"})
                self.assertEqual(self.remaining, [])
                self.assertEqual(self.runtime, [])
                self.assertEqual(self.calls[-1], ["systemctl", "daemon-reload"])

    def test_identity_change_during_the_wait_writes_nothing_and_removes_nothing(self):
        # A reloaded driver build or ec_sys also supplied the readings, so the
        # release and every later EC write wait for a fresh identity check.
        for during, extra in (("srcversion", "Refusing uninstall recovery on an "
                                             "unsupported driver build."),
                              ("ec_sys", "Refusing uninstall recovery while ec_sys is loaded.")):
            with self.subTest(during=during):
                result = self.run_uninstall([COOL], during=during)
                self.assert_refused(
                    result, extra, "The exact platform or driver identity changed during the "
                    "cool confirmation; nothing was written after it, so Cooler Boost "
                    "was not released.")
                self.assertEqual(self.confirmation["code"], 0)
                self.assertEqual(self.ec["cooler_boost"], "on")
            with self.subTest(during=during, hot=True):
                result = self.run_uninstall([HOT], during=during)
                self.assert_refused(result, extra, "too hot to release Cooler Boost")
                self.assertEqual(self.ec["cooler_boost"], "on")
            with self.subTest(during=during, hot=True, toggled=True):
                # A refusal's Boost request is an EC write too; it never goes
                # to a driver whose identity no longer matches.
                result = self.run_uninstall([HOT], during="boost-off," + during)
                self.assert_refused(result, extra, "too hot to release Cooler Boost")
                self.assertEqual(self.ec["cooler_boost"], "off")

    def test_identity_mismatch_before_the_restore_writes_nothing(self):
        for relative, value in (("sys/class/dmi/id/board_name", "MS-0000"),
                                ("sys/module/msi_ec/srcversion", "0000000000000000000000"),
                                ("sys/devices/platform/msi-ec/fw_version", "1824EMS1.107")):
            with self.subTest(relative=relative):
                result = self.run_uninstall([COOL], platform={relative: value})
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("confirm-cool", [call[0] for call in self.calls])
                self.assertEqual(self.ec, {"fan_curve": DEFAULT, "fan_mode": "advanced",
                                           "cooler_boost": "off"})
                self.assertEqual(self.remaining, list(TARGETS))


if __name__ == "__main__":
    unittest.main()
