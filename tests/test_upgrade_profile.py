#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute upgrade failure paths against temporary files and mocked services."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import pwd
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
FACTORY = "58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75"
DEFAULT = "50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120"
# Coretemp package (millidegrees), EC CPU and EC GPU readings for one sample.
COOL = ("52000", "50", "40")
HOT = ("90000", "88", "72")

# Every privileged command is replaced, every deployment/sysfs path is
# relocated beneath TemporaryDirectory, and PATH holds only the mocks and an
# explicit allowlist of real tools. No test can reach the real controller.
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
if command == "setpriv":
    # Record the privilege drop, then run the command as this test user.
    separator = args.index("--")
    child = args[separator + 1:]
    uid = next(arg.split("=", 1)[1] for arg in args[:separator] if arg.startswith("--reuid="))
    unreadable = os.environ.get("UPGRADE_TEST_UNREADABLE")
    if child[0] == "test" and unreadable and child[-1].startswith(unreadable + "/"):
        sys.exit(1)
    os.execvpe(child[0], child, dict(os.environ, UPGRADE_TEST_UID=uid))
elif command == "verify-release.sh":
    tree = pathlib.Path(sys.argv[0]).parents[1]
    (root / "verify.json").write_text(json.dumps({
        "tree": str(tree), "uid": os.environ.get("UPGRADE_TEST_UID"),
        "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        "manifest": (tree / "SHA256SUMS").is_file(), "git": (tree / ".git").exists(),
        "mode": stat.S_IMODE(tree.stat().st_mode)}))
    if failure == "verify":
        sys.exit(26)
elif command == "preflight.sh":
    # The exact-platform check: the upgrade runs it first and again before it
    # continues at full cooling; the identity failure fails the second run.
    if failure == "identity" and sum(call == ["preflight.sh"] for call in map(
            json.loads, (root / "calls.jsonl").read_text().splitlines())) > 1:
        print("FAIL board                    got=<MS-0000> expected=<MS-1824>")
        print("PREFLIGHT FAILED — do not install.", file=sys.stderr)
        sys.exit(1)
    print("PREFLIGHT PASSED")
elif command == "msi-fan-profile":
    if args == ["factory-auto"]:
        state.update(active="inactive", substate="dead", keeper="inactive")
        marker = root / "run/msi-fan-profile.factory-auto"
        marker.touch()
        marker.chmod(0o600)
        refusal = os.environ.get("UPGRADE_TEST_REFUSAL")
        if refusal:
            # An earlier release's refused handover, as on 2026-10-11: its
            # daemon's single reading refused the clean stop, ExecStopPost wrote
            # the recovery latch, and its manager disabled the service and left
            # factory auto with Boost on. Tests vary what it left behind.
            left = json.loads(refusal)
            state.update({key: left[key] for key in ("enabled", "active", "substate", "keeper")})
            marker.chmod(int(left["marker_mode"], 8))
            if not left["marker"]:
                marker.unlink()
            latch = root / "run/msi-fan-profile.recovery-boost"
            if left["latch"] is not None:
                target = root / "run/latch-target" if left["latch_kind"] == "symlink" else latch
                target.write_text(left["latch"])
                target.chmod(int(left["latch_mode"], 8))
                if left["latch_kind"] == "symlink":
                    latch.symlink_to(target)
                elif left["latch_kind"] == "hardlink":
                    os.link(latch, root / "run/latch-link")
            cooling(left["curve"], left["mode"], left["boost"])
            print("ERROR: factory-auto failed: factory restored but too hot to release "
                  "Boost; service disabled, factory auto with Boost on", file=sys.stderr)
            sys.exit(1)
        if failure == "hot-factory":
            # The current manager's refused release: verified factory auto with
            # Boost on; marker, enablement and recovery latch are left as they were.
            cooling(os.environ["UPGRADE_TEST_FACTORY"], "auto", "on")
            print("ERROR: factory-auto incomplete: factory restored but too hot to "
                  "release Boost; factory staged with Boost on", file=sys.stderr)
            sys.exit(27)
        cooling(os.environ["UPGRADE_TEST_FACTORY"], "auto", "off")
    elif args == ["apply-default"]:
        (root / "run/msi-fan-profile.factory-auto").unlink()
        # Record what the new daemon finds. It consumes a recovery latch once it
        # has reapplied Candidate14, and the inherited Boost becomes its
        # automatic latch until 30 continuously valid, cool seconds.
        latch = root / "run/msi-fan-profile.recovery-boost"
        latched = latch.is_file() and not latch.is_symlink()
        state["at_activation"] = {
            "boost": state["boost"], "enabled": state["enabled"],
            "latch": latch.read_text() if latched else None,
            "latch_mode": format(stat.S_IMODE(latch.stat().st_mode), "o") if latched else None}
        if failure == "activation":
            state.update(active="failed", substate="failed", keeper="active")
            cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "on")
            sys.exit(23)
        if latched:
            latch.unlink()
        state.update(active="active", substate="running", keeper="inactive")
        cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "on" if latched else "off")
    elif args == ["status"] and failure == "status":
        state.update(active="failed", substate="failed", keeper="active")
        cooling(os.environ["UPGRADE_TEST_DEFAULT"], "advanced", "on")
        sys.exit(24)
elif command == "systemctl":
    action = args[0]
    if action == "disable" and failure == "disable" and not state.get("disable_refused"):
        # Only the first disable fails, as a transient systemd error would.
        state["disable_refused"] = True
        save()
        sys.exit(1)
    if action in ("enable", "disable"):
        state["enabled"] = "enabled" if action == "enable" else "disabled"
        save()
        change = os.environ.get("UPGRADE_TEST_AT_DISABLE")
        if action == "disable" and change and not state.get("changed_at_disable"):
            # Something outside the upgrade changes the full-cooling state after
            # it was verified and before the post-disable checks, once.
            state["changed_at_disable"] = True
            latch = root / "run/msi-fan-profile.recovery-boost"
            if change == "boost-off":  # The operator presses the Cooler Boost key.
                cooling(state["curve"], state["mode"], "off")
            elif change == "latch-unlinked":
                latch.unlink()
            elif change == "latch-payload":
                latch.write_text("candidate13\n")
            elif change == "keeper-active":
                state["keeper"] = "active"
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
        if "keeper" in args[1]:
            print({"ActiveState": state["keeper"],
                   "SubState": {"inactive": "dead", "active": "running"}.get(
                       state["keeper"], state["keeper"])}[field])
        else:
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
    owner = "tester" if str(path) == os.environ.get("UPGRADE_TEST_NOT_ROOT") else "root"
    values = {"%F": kind, "%U": owner, "%G": owner,
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

# The cool-down gate's one-second wait. It records the call and advances the
# scripted sensor readings to the next sample instead of waiting; bash keeps
# each of the many samples cheap. On the first wait it can also edit a file in
# the source checkout, as an operator's git pull or editor would.
SLEEP_SHIM = textwrap.dedent(r'''\
#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 1 && $1 == 1 ]] || exit 2
root=$UPGRADE_TEST_ROOT
printf '%s\n' '["sleep", "1"]' >> "$root/calls.jsonl"
slept=1
[[ ! -f $root/slept ]] || slept=$(($(<"$root/slept") + 1))
printf '%s\n' "$slept" > "$root/slept"
if ((slept == 1)) && [[ -n ${UPGRADE_TEST_EDIT_DURING_WAIT-} ]]; then
    printf 'edited during the cool-down wait\n' >> "$UPGRADE_TEST_EDIT_DURING_WAIT"
fi
mapfile -t samples <<< "${UPGRADE_TEST_TEMPERATURES-}"
if ((slept < ${#samples[@]})); then
    # Comma-separated, so an empty (unreadable) reading keeps its position.
    IFS=, read -r package cpu gpu <<< "${samples[slept]}"
    printf '%s\n' "$package" > "$root/sys/class/hwmon/hwmon8/temp1_input"
    printf '%s\n' "$cpu" > "$root/sys/devices/platform/msi-ec/cpu/realtime_temperature"
    printf '%s\n' "$gpu" > "$root/sys/devices/platform/msi-ec/gpu/realtime_temperature"
fi
''').lstrip("\\\n")


# Real tools the upgrade and its release-check helper may use. Any other bare
# command, including a future privileged one, is not found inside the sandbox.
ALLOWED_TOOLS = ("awk", "bash", "cat", "chmod", "cmp", "cp", "dirname", "env", "find",
                 "id", "mktemp", "python3", "rm", "sha256sum", "test")
MOCKED_TOOLS = ("chgrp", "install", "setpriv", "stat", "systemctl", "systemd-analyze",
                "systemd-tmpfiles")
ROOT_GUARD = "[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }"
# An absolute host reference left after relocation would bypass PATH shims.
HOST_REFERENCE = re.compile(
    r"(?<![\w./-])/(?:bin|boot|dev|etc|lib|lib64|proc|run|sbin|sys|usr|var)(?![\w-])")
LATCH_PAYLOAD = "candidate14\n"
# What an earlier release's refused hot handover left on 2026-10-11: factory
# auto with Cooler Boost on, both units stopped, the service disabled, the
# factory-auto marker, and the recovery latch ExecStopPost wrote after the hot
# clean stop. Tests override single entries.
OLD_REFUSAL = {"curve": FACTORY, "mode": "auto", "boost": "on", "enabled": "disabled",
               "active": "inactive", "substate": "dead", "keeper": "inactive",
               "marker": True, "marker_mode": "600", "latch": LATCH_PAYLOAD, "latch_mode": "600",
               "latch_kind": "file"}


class UpgradeSandboxTestCase(unittest.TestCase):
    def run_upgrade(self, failure, *, sudo=("4242", "4343"), unreadable=False,
                    latch_mode=None, initial=None, marker=False, temperatures=(COOL,),
                    hwmon=None, cool_timeout=None, edit_during_wait=None, refusal=None,
                    not_root=None, at_disable=None):
        """Run the sandboxed upgrade.

        temperatures holds one (package, EC CPU, EC GPU) sample per cool-down
        gate sample; the first is written here verbatim (None leaves the file
        missing), each later one by the sleep shim ("" is an unreadable
        reading), and the last one persists. hwmon maps hwmon directory names
        to their reported name. edit_during_wait names a release file the
        first cool-down wait appends to. refusal makes the installed manager's
        factory-auto fail like an earlier release's refused hot handover,
        leaving OLD_REFUSAL updated with it. not_root names a sandbox path that
        stat reports as owned by a regular user. at_disable changes the state
        once, at the first systemctl disable: boost-off, latch-unlinked,
        latch-payload or keeper-active.
        """
        self.assertNotIn(None, [reading for sample in temperatures[1:] for reading in sample])
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = pathlib.Path(temporary)
            release = sandbox / "release"
            (release / "scripts").mkdir(parents=True)
            (release / ".git").mkdir()
            (release / ".git/config").write_text("[core]\n")
            (sandbox / "run").mkdir()
            base = sandbox / "sys/devices/platform/msi-ec"
            base.mkdir(parents=True)
            # A hot non-coretemp hwmon must not stand in for the CPU package.
            package, ec_cpu, ec_gpu = temperatures[0]
            for directory, name in (hwmon or {"hwmon0": "acpitz", "hwmon8": "coretemp"}).items():
                path = sandbox / "sys/class/hwmon" / directory
                path.mkdir(parents=True)
                (path / "name").write_text(name + "\n")
                reading = package if name == "coretemp" else "99000"
                if reading is not None:
                    (path / "temp1_input").write_text(reading + "\n")
            for relative, reading in (("cpu", ec_cpu), ("gpu", ec_gpu)):
                (base / relative).mkdir()
                if reading is not None:
                    (base / relative / "realtime_temperature").write_text(reading + "\n")
            initial_state = {
                "active": "active", "substate": "running", "keeper": "inactive",
                "enabled": "enabled", "curve": DEFAULT, "mode": "advanced", "boost": "off",
                **(initial or {}),
            }
            for name, key in (("fan_curve", "curve"), ("fan_mode", "mode"),
                              ("cooler_boost", "boost")):
                (base / name).write_text(initial_state[key])
            if marker:
                path = sandbox / "run/msi-fan-profile.factory-auto"
                path.touch()
                path.chmod(0o600)
            latch = sandbox / "run/msi-fan-profile.recovery-boost"
            if latch_mode is not None:
                latch.write_text(LATCH_PAYLOAD)
                latch.chmod(latch_mode)
            state_path = sandbox / "state.json"
            state_path.write_text(json.dumps(initial_state))
            (sandbox / "calls.jsonl").touch()

            # Keep all control flow intact; relocate host paths and bypass only
            # the root guards, since ownership/privileged commands are mocked.
            for name, relocate in (("upgrade-profile.sh", True),
                                   ("verify-release-unprivileged.sh", False)):
                script = (ROOT / "scripts" / name).read_text()
                self.assertIn(ROOT_GUARD, script)
                script = script.replace(ROOT_GUARD, ": # root check mocked")
                if relocate:
                    for prefix in ("/usr/local", "/etc", "/run", "/var/lib", "/sys/devices",
                                   "/sys/class"):
                        script = script.replace(prefix, str(sandbox) + prefix)
                body = "\n".join(line for line in script.splitlines()
                                 if not line.lstrip().startswith("#"))
                body = body.replace("/dev/null", "")
                self.assertIsNone(HOST_REFERENCE.search(body), name)
                script_path = release / "scripts" / name
                script_path.write_text(script)
                script_path.chmod(0o755)

            path = release / "scripts/preflight.sh"
            path.write_text(COMMAND_SHIM)
            path.chmod(0o755)
            path = release / "scripts/verify-release.sh"
            path.write_text(COMMAND_SHIM)
            path.chmod(0o755)
            for relative in (
                "src/msi-fan-profile", "src/msi-fan-profiled", "src/msi-gpu-recover",
                "src/msi_fan_control.py", "scripts/check-kernel-readiness.sh",
                "kernel/postinst.d/zz-msi-fan-profile-readiness",
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
            for name in MOCKED_TOOLS:
                path = mock_bin / name
                path.write_text(COMMAND_SHIM)
                path.chmod(0o755)
            path = mock_bin / "sleep"
            path.write_text(SLEEP_SHIM)
            path.chmod(0o755)
            allowed_bin = sandbox / "allowed-bin"
            allowed_bin.mkdir()
            for name in ALLOWED_TOOLS:
                tool = shutil.which(name)
                self.assertIsNotNone(tool, name)
                (allowed_bin / name).symlink_to(tool)
            # verify-release.sh points PYTHONPYCACHEPREFIX at an empty cache,
            # which would make every Python shim recompile its imports.
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("SUDO_", "UPGRADE_TEST_")) and key not in (
                               "PYTHONPYCACHEPREFIX", "MSI_FAN_UPGRADE_COOL_TIMEOUT")}
            if sudo:
                environment.update(SUDO_USER="tester", SUDO_UID=sudo[0], SUDO_GID=sudo[1])
            if unreadable:
                environment["UPGRADE_TEST_UNREADABLE"] = str(release)
            if cool_timeout is not None:
                environment["MSI_FAN_UPGRADE_COOL_TIMEOUT"] = cool_timeout
            if edit_during_wait is not None:
                environment["UPGRADE_TEST_EDIT_DURING_WAIT"] = str(release / edit_during_wait)
            if refusal is not None:
                environment["UPGRADE_TEST_REFUSAL"] = json.dumps({**OLD_REFUSAL, **refusal})
            if not_root is not None:
                environment["UPGRADE_TEST_NOT_ROOT"] = str(sandbox / not_root)
            if at_disable is not None:
                environment["UPGRADE_TEST_AT_DISABLE"] = at_disable
            environment.update(PATH=str(mock_bin) + os.pathsep + str(allowed_bin),
                               UPGRADE_TEST_ROOT=str(sandbox), UPGRADE_TEST_FAIL_AT=failure,
                               UPGRADE_TEST_FACTORY=FACTORY, UPGRADE_TEST_DEFAULT=DEFAULT,
                               # Line N is the sample after the Nth sleep.
                               UPGRADE_TEST_TEMPERATURES="\n".join(
                                   ["written above", *map(",".join, temperatures[1:])]))
            self.deployed_before = self.deployed_files(sandbox)
            result = subprocess.run(["bash", str(release / "scripts/upgrade-profile.sh")],
                                    env=environment, text=True, capture_output=True, timeout=30)
            state = json.loads(state_path.read_text())
            calls = [json.loads(line) for line in (sandbox / "calls.jsonl").read_text().splitlines()]
            self.stages_remaining = list((sandbox / "run").glob("msi-fan-profile-upgrade.*"))
            self.backup_created = (sandbox / "var/lib/msi-fan-profile").exists()
            verify = sandbox / "verify.json"
            self.verify = json.loads(verify.read_text()) if verify.exists() else None
            if self.verify is not None:
                self.verify["in_place"] = self.verify["tree"] == str(release)
                self.verify["tree_remaining"] = pathlib.Path(self.verify["tree"]).exists()
            self.latch_remaining = latch.exists() or latch.is_symlink()
            self.latch_after = (latch.read_text() if latch.is_file() and not latch.is_symlink()
                                else None)
            self.deployed_after = self.deployed_files(sandbox)
            self.deployed = {path: mode for path, (mode, _) in self.deployed_after.items()}
            record = sandbox / "usr/local/share/doc/msi-fan-profile/INSTALL_RECORD"
            self.record = record.read_text() if record.exists() else ""
            return result, state, calls

    @staticmethod
    def deployed_files(sandbox):
        return {
            str(path.relative_to(sandbox)): (format(path.stat().st_mode & 0o777, "o"),
                                             path.read_bytes())
            for prefix in ("etc", "usr") for path in (sandbox / prefix).rglob("*")
            if path.is_file()}


class UpgradeCoolingPreservationTests(UpgradeSandboxTestCase):
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

    def test_hot_factory_refusal_without_a_latch_replaces_nothing_and_says_how_to_retry(self):
        # Without a recovery latch the new daemon would keep the inherited Boost
        # as manual Boost, so the upgrade does not continue at full cooling.
        result, state, calls = self.run_upgrade("hot-factory")
        self.assertEqual(result.returncode, 27, result.stdout + result.stderr)
        self.assertIn("Not continuing at full cooling: no recovery Boost latch", result.stderr)
        self.assertIn("did not reach verified factory auto/off", result.stderr)
        self.assertIn("let the machine cool and rerun this upgrade", result.stderr)
        self.assertRegex(result.stderr, r"run 'sudo \S*/usr/local/sbin/msi-fan-profile "
                                        r"apply-default' and rerun this upgrade when cool")
        # Boot enablement and staged Boost are left alone, so the installed
        # profile still returns at boot and releases Boost when cool.
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (FACTORY, "auto", "on", "enabled"))
        self.assertFalse(self.latch_remaining)
        self.assertNotIn(["systemctl", "disable", "msi-fan-profile.service"], calls)
        self.assertNotIn(["msi-fan-profile", "apply-default"], calls)
        self.assertNotIn("usr/local/libexec/msi-fan-kernel-readiness", self.deployed)
        self.assertEqual(self.record, "")

    def test_cool_retry_after_a_refused_factory_release_completes(self):
        staged = {"active": "inactive", "substate": "dead",
                  "curve": FACTORY, "mode": "auto", "boost": "on"}
        result, state, calls = self.run_upgrade("", latch_mode=0o600, initial=staged,
                                                marker=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.latch_remaining)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (DEFAULT, "advanced", "off", "enabled"))
        self.assertLess(calls.index(["msi-fan-profile", "factory-auto"]),
                        calls.index(["msi-fan-profile", "apply-default"]))

    def test_keeper_recovery_latch_is_removed_in_the_verified_factory_state(self):
        result, state, calls = self.run_upgrade("", latch_mode=0o600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.latch_remaining)
        self.assertEqual(state["enabled"], "enabled")
        self.assertLess(calls.index(["msi-fan-profile", "factory-auto"]),
                        calls.index(["msi-fan-profile", "apply-default"]))

    def test_upgrade_adds_the_kernel_readiness_check_and_hook(self):
        result, _, _ = self.run_upgrade("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for target in ("usr/local/libexec/msi-fan-kernel-readiness",
                       "etc/kernel/postinst.d/zz-msi-fan-profile-readiness"):
            with self.subTest(target=target):
                self.assertEqual(self.deployed.get(target), "755")
                self.assertRegex(self.record, r"(?m)^[0-9a-f]{64}  \S*/" + target + "$")

    def assert_stopped_after_verified_factory(self, result, state, calls):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        # The manager did reach factory auto/off: no cool-retry advice, and the
        # disabled service is named, since the profile would not return at boot.
        self.assertIn("existing manager reported factory auto/off, but a later",
                      result.stderr)
        self.assertNotIn("did not reach verified factory auto/off", result.stderr)
        self.assertNotIn("let the machine cool", result.stderr)
        self.assertIn("The service was disabled, so the installed profile will not "
                      "return at boot.", result.stderr)
        self.assertRegex(result.stderr, r"run 'sudo \S*/usr/local/sbin/msi-fan-profile "
                                        r"apply-default' before rebooting")
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (FACTORY, "auto", "off", "disabled"))
        self.assertNotIn(["msi-fan-profile", "apply-default"], calls)
        self.assertNotIn("usr/local/libexec/msi-fan-kernel-readiness", self.deployed)
        self.assertEqual(self.record, "")

    def test_unsafe_recovery_latch_stops_before_activation_and_is_kept(self):
        result, state, calls = self.run_upgrade("", latch_mode=0o644)
        self.assert_stopped_after_verified_factory(result, state, calls)
        self.assertTrue(self.latch_remaining)

    def test_failed_disable_after_factory_auto_replaces_nothing_and_retries(self):
        result, state, calls = self.run_upgrade("disable")
        self.assert_stopped_after_verified_factory(result, state, calls)
        self.assertEqual(calls.count(["systemctl", "disable", "msi-fan-profile.service"]), 2)


class UpgradeFullCoolingHandoverTests(UpgradeSandboxTestCase):
    """An earlier release's factory-auto decides a Boost release from one
    reading, so a spike can refuse the handover right after the cool-down gate.
    When the installed manager verifiably left full cooling with no controller
    running (exact platform, factory curve, auto mode, Boost on, both units
    stopped, the marker and a valid recovery latch), the upgrade continues and
    keeps the latch for the new daemon. Any other shape stops as before."""

    CONTINUING = ("The installed manager left factory auto with Cooler Boost on (too hot or "
                  "unconfirmed); continuing the upgrade at full cooling; the new controller "
                  "will release Boost after its 30 s cool confirmation.")
    LATCH = "run/msi-fan-profile.recovery-boost"
    INVALID_LATCH = (r"Not continuing at full cooling: the recovery Boost latch \S*/" + LATCH +
                     r" is not a root-owned, mode-0600 file with the expected payload\.")
    # The handover through the old manager, its replacement and activation,
    # exactly as before this change, for a cool, confirmed factory auto/off.
    COOL_HANDOVER = [
        ["preflight.sh"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "stop", "msi-fan-profile-keeper.service"],
        ["systemctl", "reset-failed", "msi-fan-profile-keeper.service"],
        ["msi-fan-profile", "factory-auto"],
        ["systemctl", "disable", "msi-fan-profile.service"],
        ["systemctl", "stop", "msi-fan-profile.service"],
        ["systemctl", "reset-failed", "msi-fan-profile.service"],
        ["systemctl", "is-enabled", "msi-fan-profile.service"],
        ["systemctl", "show", "msi-fan-profile.service", "-p", "ActiveState", "--value"],
        ["systemctl", "show", "msi-fan-profile.service", "-p", "SubState", "--value"],
        ["systemd-tmpfiles", "--create", "/etc/tmpfiles.d/msi-fan-profile.conf"],
        ["systemd-analyze", "verify", "/etc/systemd/system/msi-fan-profile.service",
         "/etc/systemd/system/msi-fan-profile-keeper.service"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "show", "msi-fan-profile.service", "-p", "NeedDaemonReload", "--value"],
        ["systemctl", "enable", "msi-fan-profile.service"],
        ["systemctl", "is-enabled", "msi-fan-profile.service"],
        ["msi-fan-profile", "apply-default"],
        ["msi-fan-profile", "status"],
    ]

    @staticmethod
    def replacements(calls):
        """Indexes of installs onto the live deployment, not into the stage."""
        return [index for index, call in enumerate(calls) if call[0] == "install" and
                "-d" not in call and "/run/msi-fan-profile-upgrade." not in call[-1]]

    @staticmethod
    def live(calls):
        """Service, manager and platform-check calls, with sandbox paths removed."""
        return [[re.sub(r"^\S*/(?=(?:etc|run)/)", "/", arg) for arg in call] for call in calls
                if call[0] in ("preflight.sh", "systemctl", "msi-fan-profile",
                               "systemd-analyze", "systemd-tmpfiles")]

    def assert_continued_at_full_cooling(self, result, state, calls):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(self.CONTINUING, result.stderr)
        self.assertNotIn("Upgrade stopped", result.stderr)
        # The exact-platform check runs again after the refusal and before the
        # upgrade changes anything; the service stays disabled while every
        # artifact is replaced and recorded, and only the new manager activates.
        refused = calls.index(["msi-fan-profile", "factory-auto"])
        self.assertEqual(calls.count(["preflight.sh"]), 2)
        self.assertEqual(calls[refused + 1], ["preflight.sh"])
        disable = calls.index(["systemctl", "disable", "msi-fan-profile.service"])
        replaced = self.replacements(calls)
        enable = calls.index(["systemctl", "enable", "msi-fan-profile.service"])
        activation = calls.index(["msi-fan-profile", "apply-default"])
        self.assertTrue(all(call[0] in ("preflight.sh", "stat") or call[:2] == ["systemctl", "show"]
                            for call in calls[refused + 1:disable]), calls[refused + 1:disable])
        self.assertLess(disable, replaced[0])
        self.assertLess(replaced[-1], enable)
        self.assertLess(enable, activation)
        self.assertEqual(calls[activation + 1], ["msi-fan-profile", "status"])
        self.assertEqual(self.deployed.get("usr/local/libexec/msi-fan-kernel-readiness"), "755")
        self.assertRegex(self.record, r"(?m)^source_commit=")
        self.assertTrue(self.backup_created)
        # Boost stayed on and the latch was kept, unchanged, until the new
        # manager's apply-default; the new daemon consumed it and holds Boost as
        # its automatic latch.
        self.assertEqual(state["at_activation"], {"boost": "on", "enabled": "enabled",
                                                  "latch": LATCH_PAYLOAD, "latch_mode": "600"})
        self.assertFalse(self.latch_remaining)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"],
                          state["active"], state["keeper"]),
                         (DEFAULT, "advanced", "on", "enabled", "active", "inactive"))
        self.assertIn("Upgrade complete.", result.stdout)
        self.assertIn("Cooler Boost stays on as the new controller's automatic latch until it "
                      "has seen 30 continuously valid, cool seconds.", result.stdout)

    def test_old_manager_hot_refusal_continues_at_full_cooling_and_keeps_the_latch(self):
        result, state, calls = self.run_upgrade("", refusal={})
        self.assert_continued_at_full_cooling(result, state, calls)

    def test_current_manager_refusal_with_a_valid_latch_also_continues(self):
        # This release's manager keeps boot enablement on a refused release; the
        # upgrade still disables the service for the replacement window.
        result, state, calls = self.run_upgrade("hot-factory", latch_mode=0o600)
        self.assert_continued_at_full_cooling(result, state, calls)

    def assert_stopped_as_before(self, result, state, calls, reason, **expected):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stderr, reason)
        self.assertNotIn("continuing the upgrade at full cooling", result.stderr)
        self.assertNotIn("This script did not change Cooler Boost", result.stderr)
        self.assertIn("did not reach verified factory auto/off", result.stderr)
        self.assertIn("let the machine cool and rerun this upgrade", result.stderr)
        self.assertRegex(result.stderr, r"run 'sudo \S*/usr/local/sbin/msi-fan-profile "
                                        r"apply-default' and rerun this upgrade when cool")
        # After the refusal the upgrade only read; it replaced and recorded
        # nothing and left what the installed manager left.
        refused = calls.index(["msi-fan-profile", "factory-auto"])
        self.assertTrue(all(call[0] in ("preflight.sh", "stat") or call[:2] == ["systemctl", "show"]
                            for call in calls[refused + 1:]), calls[refused + 1:])
        self.assertEqual(self.deployed_after, self.deployed_before)
        self.assertEqual(self.record, "")
        self.assertFalse(self.backup_created)
        self.assertFalse(self.stages_remaining)
        left = {**OLD_REFUSAL, **expected}
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"],
                          state["active"], state["keeper"]),
                         tuple(left[key] for key in ("curve", "mode", "boost", "enabled",
                                                     "active", "keeper")))

    def test_missing_latch_stops_as_before(self):
        result, state, calls = self.run_upgrade("", refusal={"latch": None})
        self.assert_stopped_as_before(
            result, state, calls, r"Not continuing at full cooling: no recovery Boost latch \S*/" +
            self.LATCH + r"\.")
        self.assertFalse(self.latch_remaining)

    def test_unsafe_or_invalid_latch_stops_as_before_and_is_kept(self):
        for name, options in (
                ("mode 0644", {"refusal": {"latch_mode": "644"}}),
                ("owner", {"refusal": {}, "not_root": self.LATCH}),
                ("symlink", {"refusal": {"latch_kind": "symlink"}}),
                ("second link", {"refusal": {"latch_kind": "hardlink"}}),
                ("other profile", {"refusal": {"latch": "candidate13\n"}}),
                ("no newline", {"refusal": {"latch": "candidate14"}}),
                ("extra newline", {"refusal": {"latch": "candidate14\n\n"}}),
                ("empty", {"refusal": {"latch": ""}}),
        ):
            with self.subTest(name=name):
                result, state, calls = self.run_upgrade("", **options)
                self.assert_stopped_as_before(result, state, calls, self.INVALID_LATCH)
                self.assertTrue(self.latch_remaining)
                if options["refusal"].get("latch_kind", "file") == "file":
                    self.assertEqual(self.latch_after, {**OLD_REFUSAL, **options["refusal"]}["latch"])

    def test_any_other_state_after_the_refusal_stops_as_before(self):
        for left, reason in (
                ({"curve": DEFAULT}, "the EC is not on the factory curve"),
                ({"curve": DEFAULT, "mode": "advanced"}, "the EC is not on the factory curve"),
                ({"mode": "advanced"}, "fan_mode is not auto"),
                ({"boost": "off"}, "Cooler Boost is not on"),
                ({"active": "active", "substate": "running"},
                 "msi-fan-profile.service is not inactive"),
                ({"active": "failed", "substate": "failed"},
                 "msi-fan-profile.service is not inactive"),
                ({"keeper": "active"}, "msi-fan-profile-keeper.service is not inactive"),
                ({"active": "inactive", "substate": "failed"},
                 "msi-fan-profile.service is not inactive"),
                ({"marker": False}, "no root-owned, mode-0600 factory-auto marker"),
                ({"marker_mode": "644"}, "no root-owned, mode-0600 factory-auto marker"),
        ):
            with self.subTest(left=left):
                result, state, calls = self.run_upgrade("", refusal=left)
                self.assert_stopped_as_before(
                    result, state, calls, "Not continuing at full cooling: " + re.escape(reason),
                    **left)
                self.assertTrue(self.latch_remaining)
                self.assertEqual(self.latch_after, LATCH_PAYLOAD)

    def test_marker_that_is_not_root_owned_stops_as_before(self):
        result, state, calls = self.run_upgrade(
            "", refusal={}, not_root="run/msi-fan-profile.factory-auto")
        self.assert_stopped_as_before(
            result, state, calls,
            r"Not continuing at full cooling: no root-owned, mode-0600 factory-auto marker \S*/"
            r"run/msi-fan-profile\.factory-auto\.")
        self.assertTrue(self.latch_remaining)

    def test_failed_platform_check_after_the_refusal_stops_with_nothing_replaced(self):
        result, state, calls = self.run_upgrade("identity", refusal={})
        self.assert_stopped_as_before(
            result, state, calls,
            r"Not continuing at full cooling: the exact-platform check failed\.")
        self.assertIn("PREFLIGHT FAILED", result.stderr)
        self.assertEqual(calls.count(["preflight.sh"]), 2)
        self.assertTrue(self.latch_remaining)
        self.assertEqual(self.latch_after, LATCH_PAYLOAD)

    def test_cool_handover_is_unchanged(self):
        result, state, calls = self.run_upgrade("", latch_mode=0o600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.live(calls), self.COOL_HANDOVER)
        self.assertNotIn("full cooling", result.stdout + result.stderr)
        self.assertNotIn("Cooler Boost stays on", result.stdout)
        # The verified auto/off state completes the handover; the latch goes.
        self.assertFalse(self.latch_remaining)
        self.assertEqual(state["at_activation"], {"boost": "off", "enabled": "enabled",
                                                  "latch": None, "latch_mode": None})
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (DEFAULT, "advanced", "off", "enabled"))

    @classmethod
    def retained(cls, boost, latch):
        """The exit message's report of the Boost and latch it finds."""
        return (r"This script did not change Cooler Boost or the recovery Boost latch: "
                r"cooler_boost is " + boost + r" and \S*/" + cls.LATCH + " is " + latch + r"\.")

    def test_failure_after_continuing_reports_boost_on_and_the_latch_kept(self):
        for failure, code, message, replaced in (
                ("preactivation", 25,
                 r"Upgrade stopped; the service remains disabled in factory auto mode\. " +
                 self.retained("on", "present"), True),
                ("disable", 1,
                 r"Upgrade stopped before replacing any artifact: the installed manager left "
                 r"factory auto with Cooler Boost on, but a later service, marker, cooling or "
                 r"recovery Boost latch check failed\. " + self.retained("on", "present"), False),
        ):
            with self.subTest(failure=failure):
                result, state, calls = self.run_upgrade(failure, refusal={})
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(self.CONTINUING, result.stderr)
                self.assertRegex(result.stderr, message)
                self.assertNotIn("did not reach verified factory auto/off", result.stderr)
                self.assertNotIn("reported factory auto/off", result.stderr)
                self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                                 (FACTORY, "auto", "on", "disabled"))
                self.assertTrue(self.latch_remaining)
                self.assertEqual(self.latch_after, LATCH_PAYLOAD)
                self.assertNotIn(["msi-fan-profile", "apply-default"], calls)
                self.assertEqual(bool(self.replacements(calls)), replaced)
                if not replaced:
                    self.assertEqual(self.deployed_after, self.deployed_before)
                    self.assertIn("The service was disabled, so the installed profile will "
                                  "not return at boot.", result.stderr)

    def test_a_change_after_continuing_stops_and_reports_what_it_finds(self):
        # The checks after the disable guard the replacement: Boost on, the
        # keeper stopped and a latch the new daemon consumes. Each is read again,
        # and the exit message reports the state found, not the state intended.
        for change, boost, latch in (("boost-off", "off", "present"),
                                     ("latch-unlinked", "on", "missing"),
                                     ("latch-payload", "on", "present but not valid"),
                                     ("keeper-active", "on", "present")):
            with self.subTest(change=change):
                result, state, calls = self.run_upgrade("", refusal={}, at_disable=change)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(self.CONTINUING, result.stderr)
                self.assertRegex(
                    result.stderr,
                    r"Upgrade stopped before replacing any artifact: the installed manager "
                    r"left factory auto with Cooler Boost on, but a later service, marker, "
                    r"cooling or recovery Boost latch check failed\. " +
                    self.retained(boost, latch))
                self.assertNotIn("was left on", result.stderr)
                self.assertNotIn("was kept", result.stderr)
                self.assertEqual(calls.count(["systemctl", "disable", "msi-fan-profile.service"]),
                                 2)
                self.assertFalse(self.replacements(calls))
                self.assertEqual(self.deployed_after, self.deployed_before)
                self.assertEqual(self.record, "")
                self.assertNotIn(["msi-fan-profile", "apply-default"], calls)
                self.assertEqual((state["curve"], state["mode"], state["boost"],
                                  state["enabled"]), (FACTORY, "auto", boost, "disabled"))


class UpgradeCoolDownGateTests(UpgradeSandboxTestCase):
    """The release checks heat the CPU; the installed manager's factory-auto then
    refuses to release Cooler Boost at a CPU package or EC CPU temperature of
    85 C or an EC GPU temperature of 70 C, and older managers also disable the
    service. The upgrade hands over only after 10 consecutive cool samples."""

    LIMITS = "CPU package 80 C, EC CPU 80 C and EC GPU 65 C"
    LIVE = ("systemctl", "msi-fan-profile", "systemd-analyze", "systemd-tmpfiles")

    def staged_only(self, calls):
        installs = [call for call in calls if call[0] == "install"]
        return bool(installs) and all("/run/msi-fan-profile-upgrade." in call[-1]
                                      for call in installs)

    def assert_gate_precedes_live_changes(self, calls, sleeps):
        self.assertEqual(calls.count(["sleep", "1"]), sleeps)
        first_sleep = calls.index(["sleep", "1"])
        live = next(index for index, call in enumerate(calls) if call[0] in self.LIVE)
        self.assertEqual(calls[live], ["systemctl", "daemon-reload"])
        self.assertNotIn(["sleep", "1"], calls[live:])
        # The release checks and artifact staging run before the gate.
        self.assertIn("verify-release.sh", [call[0] for call in calls[:first_sleep]])
        self.assertTrue(self.staged_only(calls[:live]))
        self.assertEqual(calls[first_sleep:live], [["sleep", "1"]] * sleeps)

    def assert_nothing_changed(self, result, state, calls):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse([call for call in calls if call[0] in self.LIVE])
        self.assertTrue(self.staged_only(calls))
        self.assertEqual(self.deployed_after, self.deployed_before)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"],
                          state["active"], state["keeper"]),
                         (DEFAULT, "advanced", "off", "enabled", "active", "inactive"))
        self.assertFalse(self.stages_remaining)
        self.assertFalse(self.backup_created)
        # No state changed, so the exit handler reports no stop or recovery.
        self.assertNotIn("Upgrade stopped", result.stderr)
        self.assertNotIn("apply-default", result.stderr)

    def test_cool_machine_hands_over_after_the_confirmation_window_only(self):
        # Just under each margin, and EC GPU 0 for a powered-off dGPU.
        for sample in (("79999", "79", "64"), ("52000", "50", "0")):
            with self.subTest(sample=sample):
                result, state, calls = self.run_upgrade("", temperatures=(sample,))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # Ten consecutive samples take nine one-second waits, no more.
                self.assert_gate_precedes_live_changes(calls, sleeps=9)
                self.assertNotIn("Waiting", result.stdout)
                self.assertNotIn("Cooled below", result.stdout)
                self.assertLess(calls.index(["msi-fan-profile", "factory-auto"]),
                                calls.index(["msi-fan-profile", "apply-default"]))
                self.assertEqual((state["curve"], state["mode"], state["boost"]),
                                 (DEFAULT, "advanced", "off"))

    def test_hot_machine_waits_for_consecutive_cool_samples_then_hands_over(self):
        # Each sensor holds the gate at its margin in turn, a reading at the
        # margin restarts the count, and progress is reported every 30 s.
        samples = ([("80000", "50", "40")] * 10 + [("52000", "80", "40")] * 10 +
                   [("52000", "50", "68")] * 11 + [COOL] * 4 + [("52000", "50", "65")] +
                   [COOL] * 10)
        result, state, calls = self.run_upgrade("", temperatures=samples)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_gate_precedes_live_changes(calls, sleeps=len(samples) - 1)
        self.assertIn(f"Waiting up to 600 s for the machine to cool below {self.LIMITS}, "
                      "5 C under the installed manager's Cooler Boost release limits: "
                      "CPU package 80.0 C, EC CPU 50.0 C, EC GPU 40.0 C", result.stdout)
        self.assertIn("Still waiting for cool-down after 30 s (0 of 10 consecutive cool "
                      "samples): CPU package 52.0 C, EC CPU 50.0 C, EC GPU 68.0 C",
                      result.stdout)
        self.assertEqual(result.stdout.count("Still waiting"), 1)
        self.assertIn(f"Cooled below {self.LIMITS} after 45 s: CPU package 52.0 C, "
                      "EC CPU 50.0 C, EC GPU 40.0 C", result.stdout)
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (DEFAULT, "advanced", "off", "enabled"))

    def test_machine_that_stays_hot_times_out_before_any_live_change(self):
        result, state, calls = self.run_upgrade("", temperatures=(HOT,), cool_timeout="12")
        self.assert_nothing_changed(result, state, calls)
        self.assertEqual(calls.count(["sleep", "1"]), 12)
        self.assertIn(f"did not cool below {self.LIMITS} within 12 s; stop heavy workloads "
                      "and rerun.", result.stderr)
        self.assertIn("Last readings: CPU package 90.0 C, EC CPU 88.0 C, EC GPU 72.0 C. "
                      "No installed file, service or fan setting was changed.", result.stderr)
        self.assertNotIn("invalid reading", result.stderr)

    def test_implausible_or_missing_readings_never_count_as_cool(self):
        two_coretemps = {"hwmon0": "acpitz", "hwmon8": "coretemp", "hwmon9": "coretemp"}
        for reading, options in (
                ("EC CPU invalid (255)", {"temperatures": (("52000", "255", "40"),)}),
                ("EC CPU invalid (0)", {"temperatures": (("52000", "0", "40"),)}),
                ("EC GPU invalid (-1)", {"temperatures": (("52000", "50", "-1"),)}),
                ("EC GPU unreadable", {"temperatures": (("52000", "50", None),)}),
                ("CPU package unreadable", {"temperatures": (("", "50", "40"),)}),
                ("CPU package unreadable", {"hwmon": two_coretemps}),
        ):
            with self.subTest(reading=reading, options=options):
                result, state, calls = self.run_upgrade("", cool_timeout="10", **options)
                self.assert_nothing_changed(result, state, calls)
                self.assertEqual(calls.count(["sleep", "1"]), 10)
                self.assertIn("within 10 s; stop heavy workloads and rerun.", result.stderr)
                self.assertIn("Last readings: ", result.stderr)
                self.assertIn(reading, result.stderr.split("Last readings: ")[1])
                self.assertIn("An unreadable or invalid reading never counts as cool",
                              result.stderr)

    def test_invalid_sample_restarts_the_cool_count(self):
        # Like a hot one, an unreadable or invalid sample in the middle of a
        # cool streak restarts the count of consecutive cool samples.
        for bad, reading in ((("52000", "255", "40"), "EC CPU invalid (255)"),
                             (("", "50", "40"), "CPU package unreadable"),
                             (("52000", "50", "-1"), "EC GPU invalid (-1)")):
            with self.subTest(bad=bad):
                samples = [COOL] * 9 + [bad] + [COOL] * 10
                result, state, calls = self.run_upgrade("", temperatures=samples)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_gate_precedes_live_changes(calls, sleeps=len(samples) - 1)
                self.assertRegex(result.stdout, r"Waiting up to 600 s .*: .*" + re.escape(reading))
                self.assertIn(f"Cooled below {self.LIMITS} after 19 s", result.stdout)
                self.assertEqual((state["curve"], state["mode"], state["boost"]),
                                 (DEFAULT, "advanced", "off"))

    def test_source_change_during_the_wait_stops_before_any_live_change(self):
        # The wait can last minutes, and the operator may pull or edit the
        # checkout meanwhile. The stage then no longer matches the source, so the
        # upgrade stops before factory-auto rather than after replacing artifacts.
        for relative, temperatures, sleeps in (("README.md", (HOT, *[COOL] * 10), 10),
                                               ("src/msi-fan-profiled", (COOL,), 9),
                                               ("docs/OPERATIONS.md", (COOL,), 9)):
            with self.subTest(relative=relative):
                result, state, calls = self.run_upgrade(
                    "", temperatures=temperatures, edit_during_wait=relative)
                self.assert_nothing_changed(result, state, calls)
                self.assertEqual(calls.count(["sleep", "1"]), sleeps)
                self.assertRegex(result.stderr, r"Upgrade not started: \S*/release/"
                                 + re.escape(relative) + r" changed during the cool-down "
                                 r"wait\. No installed file, service or fan setting was "
                                 r"changed; rerun\.")

    def test_install_record_names_the_manifest_verified_before_the_wait(self):
        # A manifest change during the wait leaves every deployed artifact as
        # verified, and the record still names the manifest the checks used.
        result, _, calls = self.run_upgrade("", temperatures=(HOT, *[COOL] * 10),
                                            edit_during_wait="SHA256SUMS")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_gate_precedes_live_changes(calls, sleeps=10)
        verified = hashlib.sha256(b"test artifact\n").hexdigest()
        self.assertIn(f"source_manifest_sha256={verified}\n", self.record)

    def test_invalid_cool_down_timeout_is_rejected_before_any_change(self):
        for value in ("", "0", "9", "86401", "-5", "010", "1.5", "60s", "abc"):
            with self.subTest(value=value):
                result, state, calls = self.run_upgrade("", cool_timeout=value)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("MSI_FAN_UPGRADE_COOL_TIMEOUT must be a whole number of seconds "
                              "from 10 to 86400", result.stderr)
                # Rejected before the release checks; only the read-only manager
                # ownership check precedes it.
                self.assertEqual({call[0] for call in calls}, {"stat"})
                self.assertIsNone(self.verify)
                self.assertEqual(self.deployed_after, self.deployed_before)
                self.assertEqual((state["curve"], state["mode"], state["boost"],
                                  state["enabled"]), (DEFAULT, "advanced", "off", "enabled"))


class UnprivilegedReleaseCheckTests(UpgradeSandboxTestCase):
    def assert_dropped(self, calls, uid, gid):
        drops = [call for call in calls if call[0] == "setpriv"]
        self.assertTrue(drops)
        for call in drops:
            self.assertEqual(call[1:call.index("--")], [
                f"--reuid={uid}", f"--regid={gid}", "--clear-groups",
                "--no-new-privs", "--reset-env"])
        self.assertEqual(drops[-1][drops[-1].index("--") + 1:][:2],
                         ["env", "PYTHONDONTWRITEBYTECODE=1"])
        self.assertEqual(self.verify["uid"], uid)
        self.assertEqual(self.verify["bytecode"], "1")
        # Release checks finish before the installed controller is touched.
        first_change = next(index for index, call in enumerate(calls)
                            if call[0] in ("systemctl", "msi-fan-profile", "install"))
        self.assertLess(calls.index(drops[-1]), first_change)

    def test_release_checks_run_in_place_as_the_sudo_user(self):
        result, _, calls = self.run_upgrade("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_dropped(calls, "4242", "4343")
        self.assertTrue(self.verify["in_place"])
        self.assertNotIn("chgrp", [call[0] for call in calls])

    def test_without_a_sudo_user_release_checks_run_as_nobody_on_a_private_copy(self):
        nobody = pwd.getpwnam("nobody")
        for sudo in (None, ("0", "0")):
            with self.subTest(sudo=sudo):
                result, _, calls = self.run_upgrade("", sudo=sudo, unreadable=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_dropped(calls, str(nobody.pw_uid), str(nobody.pw_gid))
                self.assertFalse(self.verify["in_place"])
                self.assertTrue(self.verify["manifest"])
                self.assertFalse(self.verify["git"])
                self.assertEqual(self.verify["mode"], 0o750)
                self.assertIn(["chgrp", "--", str(nobody.pw_gid), self.verify["tree"]], calls)
                self.assertFalse(self.verify["tree_remaining"])

    def test_checkout_unreadable_by_the_sudo_user_is_copied_for_that_user(self):
        result, _, calls = self.run_upgrade("", unreadable=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_dropped(calls, "4242", "4343")
        self.assertFalse(self.verify["in_place"])
        self.assertIn(["chgrp", "--", "4343", self.verify["tree"]], calls)
        self.assertFalse(self.verify["tree_remaining"])

    def test_failed_release_checks_stop_before_any_controller_change(self):
        result, state, calls = self.run_upgrade("verify")
        self.assertEqual(result.returncode, 26, result.stdout + result.stderr)
        self.assertEqual(self.verify["uid"], "4242")
        # Only the read-only manager ownership and platform checks precede the
        # release checks.
        self.assertEqual({call[0] for call in calls},
                         {"stat", "preflight.sh", "setpriv", "verify-release.sh"})
        self.assertEqual((state["curve"], state["mode"], state["boost"], state["enabled"]),
                         (DEFAULT, "advanced", "off", "enabled"))

    def test_privileged_scripts_never_run_release_checks_directly(self):
        for name in ("install-profile.sh", "upgrade-profile.sh"):
            script = (ROOT / "scripts" / name).read_text()
            with self.subTest(name=name):
                self.assertEqual(script.count('"$repo/scripts/verify-release-unprivileged.sh"\n'), 1)
                self.assertNotIn("scripts/verify-release.sh", script)
                self.assertLess(script.index('"$repo/scripts/preflight.sh"'),
                                script.index("verify-release-unprivileged.sh"))
        verify = (ROOT / "scripts/verify-release.sh").read_text()
        guard = verify.index("if [[ $EUID -eq 0 ]]; then")
        self.assertLess(guard, verify.index("python3 -m unittest"))


if __name__ == "__main__":
    unittest.main()
