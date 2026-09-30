#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute upgrade failure paths against temporary files and mocked services."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
FACTORY = "58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75"
DEFAULT = "50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120"

# Every privileged command is replaced, and every deployment/sysfs path is
# relocated beneath TemporaryDirectory. No test can reach the real controller.
COMMAND_SHIM = textwrap.dedent(r'''\
#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import stat
import sys

root = pathlib.Path(os.environ["UPGRADE_TEST_ROOT"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([command, *args]) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def cooling(curve, mode, boost):
    base = root / "sys/devices/platform/msi-ec"
    for name, value in (("fan_curve", curve), ("fan_mode", mode), ("cooler_boost", boost)):
        (base / name).write_text(value + "\n")
    state.update(curve=curve, mode=mode, boost=boost)
    save()

failure = os.environ.get("UPGRADE_TEST_FAIL_AT")
if command == "msi-fan-profile":
    if args == ["factory-auto"]:
        state.update(active="inactive", substate="dead", keeper="inactive")
        marker = root / "run/msi-fan-profile.factory-auto"
        marker.touch()
        marker.chmod(0o600)
        cooling(os.environ["UPGRADE_TEST_FACTORY"], "auto", "off")
    elif args == ["apply-default"]:
        (root / "run/msi-fan-profile.factory-auto").unlink()
        if failure == "activation":
            state.update(active="failed", substate="failed", keeper="active")
            cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "on")
            sys.exit(23)
        state.update(active="active", substate="running", keeper="inactive")
        cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "off")
    elif args == ["status"] and failure == "status":
        state.update(active="failed", substate="failed", keeper="active")
        cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "on")
        sys.exit(24)
elif command == "systemctl":
    action = args[0]
    if action in ("enable", "disable"):
        state["enabled"] = "enabled" if action == "enable" else "disabled"
        save()
    elif action == "is-enabled":
        print(state["enabled"])
    elif action == "stop":
        if "keeper" in args[1]:
            state["keeper"] = "inactive"
        else:
            state.update(active="inactive", substate="dead")
        save()
    elif action == "show":
        field = args[args.index("-p") + 1]
        print({"ActiveState": state["active"], "SubState": state["substate"],
               "NeedDaemonReload": "no"}[field])
elif command == "systemd-analyze":
    if failure == "preactivation":
        sys.exit(25)
elif command == "systemd-tmpfiles":
    for name in ("msi-fanctl.lock", "msi-fan-profile-manager.lock"):
        path = root / "run" / name
        path.touch()
        path.chmod(0o600)
elif command == "stat":
    fmt, path = args[1], pathlib.Path(args[2])
    info = path.stat()
    kind = "regular file" if info.st_size else "regular empty file"
    values = {"%F": kind, "%U": "root", "%G": "root",
              "%a": format(stat.S_IMODE(info.st_mode), "o"), "%h": str(info.st_nlink)}
    for key, value in values.items():
        fmt = fmt.replace(key, value)
    print(fmt)
elif command == "install":
    directory = False
    mode = 0o755
    paths = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-o", "-g", "-m"):
            if arg == "-m":
                mode = int(args[index + 1], 8)
            index += 2
            continue
        if arg == "-d":
            directory = True
        elif arg != "-D":
            paths.append(pathlib.Path(arg))
        index += 1
    if directory:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
    else:
        source, destination = paths
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(mode)
''').lstrip("\\\n")


class UpgradeCoolingPreservationTests(unittest.TestCase):
    def run_upgrade(self, failure):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = pathlib.Path(temporary)
            release = sandbox / "release"
            (release / "scripts").mkdir(parents=True)
            (sandbox / "run").mkdir()
            base = sandbox / "sys/devices/platform/msi-ec"
            base.mkdir(parents=True)
            for name, value in (("fan_curve", DEFAULT), ("fan_mode", "advanced"),
                                ("cooler_boost", "off")):
                (base / name).write_text(value)
            state_path = sandbox / "state.json"
            state_path.write_text(json.dumps({
                "active": "active", "substate": "running", "keeper": "inactive",
                "enabled": "enabled", "curve": DEFAULT, "mode": "advanced", "boost": "off",
            }))
            (sandbox / "calls.jsonl").touch()

            script = (ROOT / "scripts/upgrade-profile.sh").read_text()
            # Keep all control flow intact; relocate host paths and bypass only
            # the root guard, since ownership/privileged commands are mocked.
            root_guard = "[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }"
            self.assertIn(root_guard, script)
            script = script.replace(root_guard, ": # root check mocked")
            for prefix in ("/usr/local", "/etc", "/run", "/var/lib", "/sys/devices"):
                script = script.replace(prefix, str(sandbox) + prefix)
            script_path = release / "scripts/upgrade-profile.sh"
            script_path.write_text(script)

            for name in ("preflight.sh", "verify-release.sh"):
                path = release / "scripts" / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n")
                path.chmod(0o755)
            for relative in (
                "src/msi-fan-profile", "src/msi-fan-profiled", "src/msi-gpu-recover",
                "src/msi_fan_control.py",
                "systemd/msi-fan-profile.service", "systemd/msi-fan-profile-keeper.service",
                "tmpfiles/msi-fan-profile.conf", "README.md", "docs/OPERATIONS.md", "docs/SAFETY.md",
                "SHA256SUMS",
            ):
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(COMMAND_SHIM if relative == "src/msi-fan-profile" else "test artifact\n")
            for relative in (
                "usr/local/sbin/msi-fan-profile", "usr/local/libexec/msi-fan-profiled",
                "usr/local/libexec/msi-gpu-recover", "etc/systemd/system/msi-fan-profile.service",
                "etc/tmpfiles.d/msi-fan-profile.conf",
            ):
                path = sandbox / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(COMMAND_SHIM if path.name == "msi-fan-profile" else "old artifact\n")
                path.chmod(0o755 if "usr/local" in relative else 0o644)

            mock_bin = sandbox / "mock-bin"
            mock_bin.mkdir()
            for name in ("install", "stat", "systemctl", "systemd-analyze", "systemd-tmpfiles"):
                path = mock_bin / name
                path.write_text(COMMAND_SHIM)
                path.chmod(0o755)
            environment = dict(os.environ, PATH=str(mock_bin) + os.pathsep + os.environ["PATH"],
                               UPGRADE_TEST_ROOT=str(sandbox), UPGRADE_TEST_FAIL_AT=failure,
                               UPGRADE_TEST_FACTORY=FACTORY, UPGRADE_TEST_DEFAULT=DEFAULT)
            result = subprocess.run(["bash", str(script_path)], env=environment,
                                    text=True, capture_output=True, timeout=30)
            state = json.loads(state_path.read_text())
            calls = [json.loads(line) for line in (sandbox / "calls.jsonl").read_text().splitlines()]
            return result, state, calls

    def assert_recovery_preserved(self, failure, code):
        result, state, calls = self.run_upgrade(failure)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn("runtime cooling and recovery were left untouched", result.stderr)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["keeper"]),
                         (DEFAULT, "advanced", "on", "active"))
        self.assertEqual(state["enabled"], "enabled")
        activation = calls.index(["msi-fan-profile", "apply-default"])
        self.assertNotIn(["msi-fan-profile", "factory-auto"], calls[activation + 1:])
        self.assertNotIn(["systemctl", "disable", "msi-fan-profile.service"], calls[activation + 1:])
        self.assertNotIn(["systemctl", "stop", "msi-fan-profile-keeper.service"], calls[activation + 1:])

    def test_activation_failure_preserves_full_cooling_keeper(self):
        self.assert_recovery_preserved("activation", 23)

    def test_post_activation_status_failure_preserves_full_cooling_keeper(self):
        self.assert_recovery_preserved("status", 24)

    def test_pre_activation_failure_retains_disabled_factory_state(self):
        result, state, calls = self.run_upgrade("preactivation")
        self.assertEqual(result.returncode, 25, result.stdout + result.stderr)
        self.assertIn("service remains disabled in factory auto mode", result.stderr)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["keeper"]),
                         (FACTORY, "auto", "off", "inactive"))
        self.assertEqual(state["enabled"], "disabled")
        self.assertNotIn(["msi-fan-profile", "apply-default"], calls)


if __name__ == "__main__":
    unittest.main()
