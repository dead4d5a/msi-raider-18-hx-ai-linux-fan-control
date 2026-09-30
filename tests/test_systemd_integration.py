#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in systemd lifecycle checks using isolated user units and fake files.

Run with MSI_FANCTL_SYSTEMD_INTEGRATION=1 and unittest discovery. Prerequisites:
a non-root local user, Python 3, systemctl, a reachable systemd --user manager,
and the user's writable /run/user/<uid>/systemd/user runtime unit directory.
Default release verification skips this module without contacting systemd.

Each test creates UUID-named user units, a fake Boost/telemetry file, an advisory
lock, and an event log in a private temporary directory. Cleanup stops, resets,
and removes only those created units and reloads the user manager. No installed
controller, privileged unit, production helper, sysfs attribute, or thermal load
is used. The tests establish real systemd ordering, watchdog, conflict, flock,
and restart-limit semantics; they do not prove hardware fan response or the
privileged production unit's sandbox. Timeouts and restart limits are shortened
to keep the opt-in test bounded. An unavailable user manager is reported as a
skip; a supported manager with incorrect lifecycle behavior fails the test.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


# This actor deliberately imports no production controller code. Its only
# control operation changes a file inside the test's private temporary folder.
ACTOR = r'''import fcntl
import os
import pathlib
import signal
import socket
import sys
import time

mode, folder = sys.argv[1], pathlib.Path(sys.argv[2])
stopping = False

def event(message):
    with (folder / "events").open("a", encoding="ascii") as stream:
        stream.write(message + "\n")
        stream.flush()

def notify(message):
    address = os.environ["NOTIFY_SOCKET"]
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(message.encode("ascii"))

def stop(_number, _frame):
    global stopping
    stopping = True

if mode == "restart-fail":
    event("keeper-attempt")
    raise SystemExit(1)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
if mode == "post":
    event("post-start")
with (folder / "fan.lock").open("r") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        event(mode + "-lock-conflict")
        raise SystemExit(2)
    event(mode + "-acquired")
    if mode == "post":
        (folder / "boost").write_text("on", encoding="ascii")
        event("post-done")
    else:
        if mode == "keeper":
            (folder / "boost").write_text("on", encoding="ascii")
        notify("READY=1\nWATCHDOG=1")
        while not stopping:
            if mode == "keeper":
                if (folder / "boost").read_text(encoding="ascii") != "on":
                    (folder / "boost").write_text("on", encoding="ascii")
                    event("keeper-reasserted")
                notify("WATCHDOG=1")
            elif (folder / "telemetry").read_text(encoding="ascii") == "healthy":
                notify("WATCHDOG=1")
            time.sleep(0.025)
        event(mode + "-released")
'''


@unittest.skipUnless(
    os.environ.get("MSI_FANCTL_SYSTEMD_INTEGRATION") == "1",
    "isolated user-systemd integration is opt-in")
class IsolatedSystemdLifecycleTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("integration requires a non-root user manager")
        self.systemctl = shutil.which("systemctl")
        if not self.systemctl:
            self.skipTest("systemctl is unavailable")
        try:
            available = self.ctl("show", "-pVersion", "--value", check=False)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("systemd user manager could not be reached")
        if available.returncode:
            self.skipTest("systemd user manager is unavailable")
        runtime = pathlib.Path(f"/run/user/{os.geteuid()}")
        if not runtime.is_dir() or runtime.stat().st_uid != os.geteuid():
            self.skipTest("standard user runtime directory is unavailable")
        self.unit_dir = runtime / "systemd/user"
        try:
            self.unit_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.skipTest("user runtime unit directory is not writable")
        self.temporary = tempfile.TemporaryDirectory(prefix="msi-fanctl-systemd-test-")
        self.folder = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.units: list[str] = []
        self.unit_paths: list[pathlib.Path] = []
        self.addCleanup(self.cleanup_units)
        self.prefix = f"msi-fanctl-test-{uuid.uuid4().hex}"
        self.primary = f"{self.prefix}-primary.service"
        self.keeper = f"{self.prefix}-keeper.service"
        self.actor = self.folder / "actor.py"
        self.actor.write_text(ACTOR, encoding="ascii")
        for name, contents in (("fan.lock", ""), ("events", ""),
                               ("boost", "off"), ("telemetry", "healthy")):
            (self.folder / name).write_text(contents, encoding="ascii")

    def ctl(self, *arguments, check=True):
        return subprocess.run(
            [self.systemctl, "--user", "--no-pager", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=check, timeout=12)

    @staticmethod
    def unit_argument(value):
        # systemd's argument parser is not a shell; escape its quote/specifier
        # syntax without creating a shell command or depending on PATH.
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    def command(self, mode):
        return " ".join(self.unit_argument(value)
                        for value in (sys.executable, self.actor, mode, self.folder))

    def install_units(self, units):
        for name, contents in units.items():
            self.assertTrue(name.startswith(self.prefix + "-"))
            path = self.unit_dir / name
            # Exclusive creation guarantees cleanup cannot remove an older unit.
            with path.open("x", encoding="ascii") as stream:
                self.units.append(name)
                self.unit_paths.append(path)
                stream.write(contents)
        self.ctl("daemon-reload")

    def install_controller_pair(self):
        self.install_units({
            self.primary: f"""[Unit]
Description=Isolated fan-controller lifecycle test primary
OnFailure={self.keeper}
[Service]
Type=notify
NotifyAccess=main
ExecStart={self.command('primary')}
ExecStopPost={self.command('post')}
WatchdogSec=500ms
WatchdogSignal=SIGKILL
TimeoutStartSec=3s
TimeoutStopSec=3s
Restart=no
""",
            self.keeper: f"""[Unit]
Description=Isolated fan-controller lifecycle test keeper
Conflicts={self.primary}
Before={self.primary}
[Service]
Type=notify
NotifyAccess=main
ExecStart={self.command('keeper')}
WatchdogSec=500ms
TimeoutStartSec=3s
TimeoutStopSec=3s
Restart=no
""",
        })

    def cleanup_units(self):
        if not self.units:
            return
        try:
            try:
                stopped = self.ctl("stop", *self.units, check=False).returncode == 0
            except subprocess.TimeoutExpired:
                stopped = False
            if not stopped:
                self.ctl("kill", "--signal=SIGKILL", *self.units, check=False)
                self.ctl("stop", *self.units, check=False)
            self.ctl("reset-failed", *self.units, check=False)
        finally:
            for path in self.unit_paths:
                path.unlink(missing_ok=True)
            self.ctl("daemon-reload", check=False)

    def properties(self, unit):
        result = self.ctl("show", unit, "-pActiveState", "-pSubState", "-pResult",
                          "-pMainPID", "-pNRestarts")
        return dict(line.split("=", 1) for line in result.stdout.splitlines()
                    if "=" in line)

    def events(self):
        return (self.folder / "events").read_text(encoding="ascii").splitlines()

    def wait_for(self, predicate, message, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"{message}; events={self.events()}")

    def assert_running(self, unit):
        state = self.properties(unit)
        self.assertEqual((state["ActiveState"], state["SubState"]), ("active", "running"))
        self.assertGreater(int(state["MainPID"]), 0)

    def test_missed_watchdog_runs_post_stop_before_keeper_acquires_lock(self):
        self.install_controller_pair()
        (self.folder / "telemetry").write_text("miss-watchdog", encoding="ascii")
        self.ctl("start", self.primary)
        self.wait_for(lambda: "keeper-acquired" in self.events(), "watchdog handoff missing")
        self.assertEqual(self.properties(self.primary)["Result"], "watchdog")
        self.assert_running(self.keeper)
        events = self.events()
        self.assertLess(events.index("primary-acquired"), events.index("post-acquired"))
        self.assertLess(events.index("post-done"), events.index("keeper-acquired"))
        self.assertFalse(any(event.endswith("-lock-conflict") for event in events))
        self.assertEqual((self.folder / "boost").read_text(encoding="ascii"), "on")

    def test_primary_and_keeper_conflicts_yield_without_overlapping_control(self):
        self.install_controller_pair()
        self.ctl("start", self.primary)
        self.assert_running(self.primary)
        self.ctl("start", self.keeper)
        self.assert_running(self.keeper)
        self.assertEqual(self.properties(self.primary)["MainPID"], "0")
        first = self.events()
        self.assertLess(first.index("primary-released"), first.index("post-acquired"))
        self.assertLess(first.index("post-done"), first.index("keeper-acquired"))
        (self.folder / "boost").write_text("off", encoding="ascii")
        self.wait_for(lambda: "keeper-reasserted" in self.events(), "fake Boost repair missing")
        self.ctl("start", self.primary)
        self.assert_running(self.primary)
        self.assertEqual(self.properties(self.keeper)["MainPID"], "0")
        events = self.events()
        self.assertLess(events.index("keeper-released"), len(events) - 1)
        self.assertEqual(events[-1], "primary-acquired")
        self.assertFalse(any(event.endswith("-lock-conflict") for event in events))

    def test_keeper_failures_exhaust_real_systemd_restart_limit(self):
        failing = f"{self.prefix}-restart.service"
        self.install_units({failing: f"""[Unit]
Description=Isolated fan-controller restart-limit test
StartLimitIntervalSec=10s
StartLimitBurst=2
[Service]
Type=exec
ExecStart={self.command('restart-fail')}
Restart=on-failure
RestartSec=100ms
TimeoutStartSec=3s
TimeoutStopSec=3s
"""})
        self.ctl("start", failing, check=False)
        self.wait_for(lambda: self.properties(failing)["ActiveState"] == "failed" and
                      int(self.properties(failing)["NRestarts"]) >= 2,
                      "keeper restart limit did not become visible")
        state = self.properties(failing)
        self.assertEqual(state["ActiveState"], "failed")
        self.assertEqual(state["MainPID"], "0")
        self.assertEqual(self.events().count("keeper-attempt"), 2)
        # systemd versions differ in whether Result retains "exit-code" or
        # reports "start-limit-hit". A real denied additional start, with no
        # actor invocation, proves the limit without depending on that wording.
        denied = self.ctl("start", failing, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(self.events().count("keeper-attempt"), 2)


if __name__ == "__main__":
    unittest.main()
