#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the pre-reboot kernel readiness check and its kernel postinst hook.

The check runs from a private copy whose /boot, /lib/modules, DKMS tree and MOK
certificate paths are relocated beneath a TemporaryDirectory. dkms, modinfo,
mokutil, openssl, readlink and modprobe are fixture-driven fakes that accept
only read-only queries; privileged or loading commands are tripwires. Nothing
reads the host's kernels, DKMS state, EFI variables or journal.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
CHECK = ROOT / "scripts/check-kernel-readiness.sh"
HOOK = ROOT / "kernel/postinst.d/zz-msi-fan-profile-readiness"
RELEASE = "7.0.0-38-generic"
FALLBACK = "7.0.0-34-generic"
LEGACY_SRCVERSION = "AB0BFAE2391B5ADD66E01BD"
SNAPSHOT_SRCVERSION = "9086C45007CBB7FA1264430"
SIG_KEY = "73:26:A2:5E:32:C1:F7:16:AE:C5:75:BE:32:83:69:B1:AA:88:E4:F2"
SERIAL = "7326A25E32C1F716AEC575BE328369B1AA88E4F2"
OTHER_KEY = "46:96:8A:BA:70:C9:2C:71:4B:8E:EF:69:A5:C0:BD:24:90:5A:70:CE"
FINGERPRINT = "76:A0:92:06:58:00:BF:37:69:01:C3:72:CD:55:A9:0E:1F:DE:D2:E0"
SIGNER = "Local Secure Boot Module Signature key"
PATH_ASSIGNMENTS = {
    "modules_root": "/lib/modules",
    "boot_dir": "/boot",
    "dkms_tree": "/var/lib/dkms",
    "mok_certificate": "/var/lib/shim-signed/mok/MOK.der",
}
FAKE_COMMANDS = ("dkms", "modinfo", "mokutil", "openssl", "readlink", "modprobe")
TRIPWIRES = ("depmod", "insmod", "rmmod", "kmod", "sudo", "systemctl", "update-grub",
             "update-initramfs", "logger")
# An absolute host reference left after relocation would bypass the sandbox.
HOST_REFERENCE = re.compile(
    r"(?<![\w./-])/(?:bin|boot|dev|etc|lib|lib64|proc|run|sbin|sys|usr|var)(?![\w-])")

COMMAND_SHIM = textwrap.dedent(r'''\
#!/usr/bin/env python3
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["READINESS_TEST_ROOT"])
command = pathlib.Path(sys.argv[0]).name
arguments = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([command, *arguments]) + "\n")
fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))


def reply(text, status=0):
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    raise SystemExit(status)


def blocked():
    print("Blocked test tripwire: " + " ".join([command, *arguments]), file=sys.stderr)
    raise SystemExit(73)


certificate = fixture["certificate"]
if command == "dkms" and arguments == ["status", "-m", "msi_ec"]:
    reply(fixture["dkms_status"], fixture.get("dkms_rc", 0))
elif command == "modinfo" and len(arguments) == 4 and arguments[0] == "-k" and arguments[2] == "-n":
    path = fixture["modules"].get(arguments[1], {}).get(arguments[3])
    if path is None:
        print("modinfo: ERROR: Module " + arguments[3] + " not found.", file=sys.stderr)
        raise SystemExit(1)
    reply(path)
elif command == "modinfo" and len(arguments) == 3 and arguments[0] == "-F":
    if arguments[2] not in fixture["fields"]:
        raise SystemExit(1)
    reply(fixture["fields"][arguments[2]].get(arguments[1], ""))
elif command == "mokutil" and arguments == ["--sb-state"]:
    reply(fixture["sb_state"])
elif command == "mokutil" and arguments == ["--test-key", certificate]:
    reply(fixture["test_key"], 1)
elif command == "mokutil" and arguments == ["--list-enrolled"]:
    reply(fixture["list_enrolled"])
elif command == "openssl" and arguments == ["x509", "-inform", "DER", "-in", certificate,
                                            "-noout", "-serial"]:
    reply(fixture["serial"])
elif command == "openssl" and arguments == ["x509", "-inform", "DER", "-in", certificate,
                                            "-noout", "-fingerprint", "-sha1"]:
    reply(fixture["fingerprint"])
elif command == "readlink" and len(arguments) == 3 and arguments[:2] == ["-f", "--"]:
    reply(fixture["links"].get(arguments[2], arguments[2]))
elif command == "modprobe" and arguments == ["-c"]:
    reply(fixture["modprobe_config"])
blocked()
''').lstrip("\\\n")


def without_usage_and_comments(script):
    """Executable text only: drop comments and the usage here-document."""
    lines, in_usage = [], False
    for line in script.splitlines():
        if line.strip() == "cat <<EOF":
            in_usage = True
        elif in_usage and line == "EOF":
            in_usage = False
        elif not in_usage and not line.lstrip().startswith("#"):
            lines.append(line)
    return "\n".join(lines)


class ReadinessFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="msi-fanctl-readiness-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = pathlib.Path(temporary.name)
        self.sandbox = self.folder / "root"
        self.commands = self.folder / "mock-bin"
        self.commands.mkdir()
        for command in FAKE_COMMANDS + TRIPWIRES:
            path = self.commands / command
            path.write_text(COMMAND_SHIM, encoding="utf-8")
            path.chmod(0o755)
        (self.folder / "calls.jsonl").touch()

        release = self.folder / "release"
        (release / "scripts").mkdir(parents=True)
        (release / "src").mkdir()
        shutil.copy(ROOT / "src/msi_fan_control.py", release / "src/msi_fan_control.py")
        self.helper = release / "src/msi_fan_control.py"
        script = CHECK.read_text(encoding="utf-8")
        for variable, host_path in PATH_ASSIGNMENTS.items():
            assignment = f"{variable}={host_path}\n"
            self.assertEqual(script.count(assignment), 1, "relocate " + variable)
            script = script.replace(
                assignment, f"{variable}={shlex.quote(str(self.sandbox) + host_path)}\n")
        body = without_usage_and_comments(script)
        for allowed in ("/usr/bin/python3", "/dev/null"):
            body = body.replace(allowed, "")
        self.assertIsNone(HOST_REFERENCE.search(body), "unrelocated host path")
        self.check = release / "scripts/check-kernel-readiness.sh"
        self.check.write_text(script, encoding="utf-8")
        self.check.chmod(0o755)

        self.boot = self.sandbox / "boot"
        self.modules = self.sandbox / "lib/modules"
        self.certificate = self.sandbox / PATH_ASSIGNMENTS["mok_certificate"].lstrip("/")
        self.certificate.parent.mkdir(parents=True)
        self.certificate.write_bytes(b"fake DER certificate\n")
        self.boot.mkdir()
        self.fixture = {
            "certificate": str(self.certificate),
            "dkms_status": "",
            "modules": {},
            "fields": {},
            "links": {str(self.boot / "vmlinuz"): str(self.boot / ("vmlinuz-" + RELEASE))},
            "sb_state": "SecureBoot enabled",
            "test_key": str(self.certificate) + " is already enrolled",
            "list_enrolled": "",
            "serial": "serial=" + SERIAL,
            "fingerprint": "SHA1 Fingerprint=" + FINGERPRINT,
            "modprobe_config": "alias pci:v* nothing\nblacklist pcspkr\n",
        }
        for release_name in (RELEASE, FALLBACK):
            self.add_kernel(release_name)
        self.environment = dict(os.environ)
        self.environment.update(
            READINESS_TEST_ROOT=str(self.folder),
            PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            PYTHONDONTWRITEBYTECODE="1",
        )

    def add_kernel(self, release, *, dkms="installed", module="dkms",
                   srcversion=LEGACY_SRCVERSION, sig_key=SIG_KEY):
        (self.boot / ("vmlinuz-" + release)).write_bytes(b"kernel image\n")
        directory = self.modules / release
        directory.mkdir(parents=True, exist_ok=True)
        dkms_module = "updates/dkms/msi-ec.ko"
        in_tree = "kernel/drivers/platform/x86/msi-ec.ko.zst"
        chosen = dkms_module if module == "dkms" else in_tree
        (directory / "modules.dep").write_text(
            "kernel/drivers/hwmon/coretemp.ko.zst:\n"
            "kernel/drivers/platform/x86/msi-wmi-platform.ko.zst: kernel/drivers/platform/x86/wmi.ko.zst\n"
            + chosen + ":\n", encoding="utf-8")
        path = str(directory / chosen)
        self.fixture["modules"][release] = {
            "msi_ec": path,
            "msi_wmi_platform": str(directory / "kernel/drivers/platform/x86/msi-wmi-platform.ko.zst"),
            "coretemp": str(directory / "kernel/drivers/hwmon/coretemp.ko.zst"),
        }
        self.fixture["fields"][path] = {
            "version": "0.13.1" if module == "dkms" else "",
            "srcversion": srcversion,
            "vermagic": release + " SMP preempt mod_unload modversions ",
            "sig_key": sig_key,
            "signer": SIGNER,
        }
        if dkms:
            self.fixture["dkms_status"] += f"msi_ec/0.13.1, {release}, x86_64: {dkms}\n"
        return path

    def calls(self):
        return [json.loads(line) for line in
                (self.folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    def run_check(self, *arguments):
        (self.folder / "fixture.json").write_text(json.dumps(self.fixture), encoding="utf-8")
        (self.folder / "calls.jsonl").write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(self.check), *arguments], cwd=self.folder, env=self.environment,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        for call in self.calls():
            self.assertIn(call[0], FAKE_COMMANDS, "only read-only queries: " + repr(call))
        return result

    def lines(self, result, status):
        return {line.split()[1]: line for line in result.stdout.splitlines()
                if line.startswith(status + " ")}

    def assert_ready(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lines(result, "FAIL"), {})
        self.assertIn("KERNEL READINESS PASSED: " + RELEASE, result.stdout)

    def assert_not_ready(self, result, *labels):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        failures = self.lines(result, "FAIL")
        for label in labels:
            self.assertIn(label, failures, result.stdout)
        self.assertIn("KERNEL READINESS FAILED", result.stderr)
        self.assertNotIn("KERNEL READINESS PASSED", result.stdout)
        return failures


class KernelReadinessTests(ReadinessFixture):
    def test_signed_pinned_dkms_build_is_ready(self):
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        self.assertEqual(set(self.lines(result, "PASS")), {
            "secure_boot", "dkms_build", "module_path", "modules_dep", "version",
            "srcversion", "vermagic", "module_signature", "mok_enrolled",
            "msi_wmi_platform", "coretemp", "modprobe_config"})
        self.assertIn(LEGACY_SRCVERSION + " (pinned legacy build)", result.stdout)
        self.assertEqual(self.lines(result, "WARN"), {})

    def test_default_target_is_the_kernel_boot_symlink_selects(self):
        self.add_kernel("7.0.0-40-generic")
        result = self.run_check()
        self.assert_ready(result)
        self.assertIn(f"for kernel {RELEASE} ({self.boot / 'vmlinuz'})", result.stdout)
        self.assertIn(["readlink", "-f", "--", str(self.boot / "vmlinuz")], self.calls())

    def test_without_a_boot_symlink_the_newest_installed_kernel_is_checked(self):
        # Version order, not lexical order: -9 sorts last as text.
        self.add_kernel("7.0.0-9-generic")
        self.fixture["links"] = {}
        result = self.run_check()
        self.assert_ready(result)
        self.assertIn(f"for kernel {RELEASE} (newest installed kernel)", result.stdout)

    def set_field(self, field, value, release=RELEASE):
        self.fixture["fields"][self.fixture["modules"][release]["msi_ec"]][field] = value

    def test_unsigned_module_fails_under_secure_boot(self):
        # DKMS still reports "installed" after a signing failure.
        self.set_field("sig_key", "")
        failures = self.assert_not_ready(self.run_check(RELEASE), "module_signature")
        self.assertIn("unsigned", failures["module_signature"])
        self.assertIn("dkms_build", self.lines(self.run_check(RELEASE), "PASS"))

    def test_signing_problems_only_warn_when_secure_boot_is_disabled(self):
        self.set_field("sig_key", "")
        self.fixture["test_key"] = f"{self.certificate} is not enrolled"
        self.fixture["sb_state"] = "SecureBoot disabled"
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        warnings = self.lines(result, "WARN")
        for label in ("secure_boot", "module_signature", "mok_enrolled"):
            self.assertIn(label, warnings)

    def test_unknown_secure_boot_state_is_checked_as_enabled(self):
        self.set_field("sig_key", "")
        self.fixture["sb_state"] = "EFI variables are not supported on this system"
        result = self.run_check(RELEASE)
        self.assert_not_ready(result, "module_signature")
        self.assertIn("secure_boot", self.lines(result, "WARN"))

    def test_module_signed_with_another_key_fails(self):
        self.set_field("sig_key", OTHER_KEY)
        failures = self.assert_not_ready(self.run_check(RELEASE), "module_signature")
        self.assertIn("not the MOK certificate", failures["module_signature"])

    def test_signing_key_comparison_ignores_separators_case_and_leading_zeros(self):
        self.set_field("sig_key", "00:" + SIG_KEY.lower())
        self.assert_ready(self.run_check(RELEASE))
        self.set_field("sig_key", SIG_KEY[:-2] + "F3")
        self.assert_not_ready(self.run_check(RELEASE), "module_signature")

    def test_unenrolled_or_pending_certificate_fails(self):
        for state in ("is not enrolled", "is already in the enrollment request"):
            with self.subTest(state=state):
                self.fixture["test_key"] = f"{self.certificate} {state}"
                failures = self.assert_not_ready(self.run_check(RELEASE), "mok_enrolled")
                self.assertIn(state, failures["mok_enrolled"])

    def test_enrollment_listed_by_fingerprint_is_accepted(self):
        self.fixture["test_key"] = "Failed to access kernel trusted keyring"
        self.fixture["list_enrolled"] = (
            "[key 1]\nSHA1 Fingerprint: " + FINGERPRINT.lower() + "\nCertificate:\n")
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        self.assertIn("listed as enrolled", self.lines(result, "PASS")["mok_enrolled"])

    def test_missing_mok_certificate_fails(self):
        self.certificate.unlink()
        failures = self.assert_not_ready(self.run_check(RELEASE), "mok_certificate",
                                         "module_signature")
        self.assertIn("unenrolled", failures["mok_certificate"])
        self.assertNotIn(["mokutil", "--test-key", str(self.certificate)], self.calls())

    def test_missing_dkms_build_falls_back_to_rejecting_in_tree_module(self):
        self.fixture["dkms_status"] = f"msi_ec/0.13.1, {FALLBACK}, x86_64: installed\n"
        self.add_kernel(RELEASE, dkms="", module="in-tree", srcversion="000CB54099F66F442017CCC",
                        sig_key=OTHER_KEY)
        failures = self.assert_not_ready(
            self.run_check(RELEASE), "dkms_build", "module_path", "modules_dep", "version",
            "srcversion", "module_signature")
        self.assertIn("has no build", failures["dkms_build"])
        self.assertIn("in-tree", failures["module_path"])

    def test_dkms_build_that_is_not_installed_is_reported_verbatim(self):
        self.fixture["dkms_status"] = f"msi_ec/0.13.1, {RELEASE}, x86_64: built\n"
        failures = self.assert_not_ready(self.run_check(RELEASE), "dkms_build")
        self.assertIn("x86_64: built", failures["dkms_build"])

    def test_missing_kernel_modules_fail(self):
        failures = self.assert_not_ready(self.run_check("7.0.0-99-generic"), "kernel_modules",
                                         "dkms_build", "module_path", "msi_wmi_platform",
                                         "coretemp")
        self.assertIn("not installed", failures["kernel_modules"])

    def test_unpinned_wrong_version_or_wrong_kernel_build_fails(self):
        path = self.fixture["modules"][RELEASE]["msi_ec"]
        for field, value in (("srcversion", "123635EB33D32BA9FCFABCA"), ("srcversion", ""),
                             ("version", "0.13"), ("vermagic", FALLBACK + " SMP "),
                             ("vermagic", RELEASE + "-other SMP ")):
            with self.subTest(field=field, value=value):
                original = self.fixture["fields"][path][field]
                self.fixture["fields"][path][field] = value
                self.assert_not_ready(self.run_check(RELEASE), field)
                self.fixture["fields"][path][field] = original

    def test_snapshot_pin_is_accepted_and_pins_come_from_the_identity_helper(self):
        path = self.fixture["modules"][RELEASE]["msi_ec"]
        self.fixture["fields"][path]["srcversion"] = SNAPSHOT_SRCVERSION
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        self.assertIn(SNAPSHOT_SRCVERSION + " (pinned snapshot build)", result.stdout)
        helper = self.helper.read_text(encoding="utf-8")
        self.assertEqual(helper.count(f'SNAPSHOT_SRCVERSION = "{SNAPSHOT_SRCVERSION}"'), 1)
        self.helper.write_text(helper.replace(
            f'SNAPSHOT_SRCVERSION = "{SNAPSHOT_SRCVERSION}"',
            'SNAPSHOT_SRCVERSION = "00000000000000000000000"'), encoding="utf-8")
        self.assert_not_ready(self.run_check(RELEASE), "srcversion")
        self.helper.unlink()
        self.assert_not_ready(self.run_check(RELEASE), "driver_pins", "srcversion")

    def test_modules_dep_must_have_exactly_the_dkms_entry(self):
        dependencies = self.modules / RELEASE / "modules.dep"
        original = dependencies.read_text(encoding="utf-8")
        for text in (original + "extra/msi-ec.ko.xz:\n",
                     original.replace("updates/dkms/msi-ec.ko:\n", "")):
            with self.subTest(text=text):
                dependencies.write_text(text, encoding="utf-8")
                self.assert_not_ready(self.run_check(RELEASE), "modules_dep")
        dependencies.unlink()
        self.assert_not_ready(self.run_check(RELEASE), "modules_dep")

    def test_compressed_dkms_module_is_accepted(self):
        path = self.fixture["modules"][RELEASE]["msi_ec"]
        compressed = path + ".zst"
        self.fixture["modules"][RELEASE]["msi_ec"] = compressed
        self.fixture["fields"][compressed] = self.fixture["fields"].pop(path)
        dependencies = self.modules / RELEASE / "modules.dep"
        dependencies.write_text(dependencies.read_text(encoding="utf-8").replace(
            "updates/dkms/msi-ec.ko:", "updates/dkms/msi-ec.ko.zst:"), encoding="utf-8")
        self.assert_ready(self.run_check(RELEASE))

    def test_missing_platform_modules_fail(self):
        for name in ("msi_wmi_platform", "coretemp"):
            with self.subTest(name=name):
                saved = self.fixture["modules"][RELEASE].pop(name)
                self.assert_not_ready(self.run_check(RELEASE), name)
                self.fixture["modules"][RELEASE][name] = saved

    def test_modprobe_overrides_fail_except_an_ec_sys_blacklist(self):
        for line in ("blacklist msi_ec", "blacklist msi-ec", "install msi_ec /bin/false",
                     "options msi_ec firmware=1824EMS1.107", "softdep coretemp pre: ec_sys",
                     "BLACKLIST msi_wmi_platform", "remove coretemp /bin/true",
                     "install ec_sys /sbin/modprobe --ignore-install ec_sys write_support=1",
                     "options ec_sys write_support=1"):
            with self.subTest(line=line):
                self.fixture["modprobe_config"] = "blacklist pcspkr\n" + line + "\n"
                failures = self.assert_not_ready(self.run_check(RELEASE), "modprobe_config")
                self.assertIn(line, failures["modprobe_config"])
        for line in ("blacklist ec_sys", "blacklist msi_ec_extra", "alias msi_ec msi_ec"):
            with self.subTest(line=line):
                self.fixture["modprobe_config"] = line + "\n"
                result = self.run_check(RELEASE)
                self.assert_ready(result)
                self.assertEqual("modprobe_config" in self.lines(result, "WARN"),
                                 line == "blacklist ec_sys")
        self.fixture["modprobe_config"] = ""
        self.assert_ready(self.run_check(RELEASE))

    def test_failed_queries_are_failures_not_passes(self):
        self.fixture["dkms_rc"] = 3
        self.assert_not_ready(self.run_check(RELEASE), "dkms_status", "dkms_build")

    def test_stale_autoinstall_registration_warns_without_failing(self):
        configuration = self.sandbox / "var/lib/dkms/msi_ec/0.13/source/dkms.conf"
        configuration.parent.mkdir(parents=True)
        self.fixture["dkms_status"] += "msi_ec/0.13, 7.0.0-31-generic, x86_64: built\n"
        for value, warned in (('"yes"', True), ("yes", True), ("'no'", True),
                              ('""', False), (None, False)):
            with self.subTest(value=value):
                configuration.write_text(
                    'PACKAGE_NAME="msi-ec"\nPACKAGE_VERSION="0.13"\n' +
                    ("" if value is None else "AUTOINSTALL=" + value + "\n"), encoding="utf-8")
                result = self.run_check(RELEASE)
                self.assert_ready(result)
                warnings = self.lines(result, "WARN")
                self.assertEqual("dkms_registration" in warnings, warned, result.stdout)
                if warned:
                    self.assertIn("msi_ec/0.13 ", warnings["dkms_registration"])
        configuration.unlink()
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        self.assertIn("cannot read", self.lines(result, "WARN")["dkms_registration"])

    def test_installed_kernel_without_a_build_warns_without_failing(self):
        self.add_kernel("7.0.0-28-generic", dkms="", module="in-tree")
        result = self.run_check(RELEASE)
        self.assert_ready(result)
        warnings = [line for line in result.stdout.splitlines()
                    if line.startswith("WARN other_kernel ")]
        self.assertEqual(len(warnings), 1, result.stdout)
        self.assertIn("7.0.0-28-generic has no msi_ec/0.13.1 build", warnings[0])

    def test_invalid_arguments_are_rejected_without_queries(self):
        for arguments in (("-k",), ("../7.0.0-38-generic",), ("7.0.0 38",), ("",),
                          (RELEASE, FALLBACK)):
            with self.subTest(arguments=arguments):
                result = self.run_check(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])
        result = self.run_check("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("need sudo", result.stdout)
        self.assertEqual(self.calls(), [])


HOOK_CHECK = textwrap.dedent('''\
#!/bin/bash
printf '%s\\n' "$*" >> "$READINESS_TEST_ROOT/check-arguments"
cat "$READINESS_TEST_ROOT/check-output"
sleep "$(cat "$READINESS_TEST_ROOT/check-sleep")"
exit "$(cat "$READINESS_TEST_ROOT/check-status")"
''')
LOGGER = textwrap.dedent('''\
#!/bin/bash
printf '%s\\0' "$@" >> "$READINESS_TEST_ROOT/logger-calls"
printf '\\n' >> "$READINESS_TEST_ROOT/logger-calls"
exit "${READINESS_TEST_LOGGER_STATUS:-0}"
''')
# Real tools the hook may use; anything else is not found in the sandbox.
HOOK_TOOLS = ("bash", "cat", "grep", "sleep", "timeout")


class KernelPostinstHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="msi-fanctl-readiness-hook-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = pathlib.Path(temporary.name)
        tools = self.folder / "bin"
        tools.mkdir()
        for name in HOOK_TOOLS:
            tool = shutil.which(name)
            self.assertIsNotNone(tool, name)
            (tools / name).symlink_to(tool)
        (tools / "logger").write_text(LOGGER, encoding="utf-8")
        (tools / "logger").chmod(0o755)
        self.check = self.folder / "msi-fan-kernel-readiness"
        self.check.write_text(HOOK_CHECK, encoding="utf-8")
        self.check.chmod(0o755)
        hook = HOOK.read_text(encoding="utf-8")
        replacements = {
            "export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n": f"export PATH={shlex.quote(str(tools))}\n",
            "check=/usr/local/libexec/msi-fan-kernel-readiness\n":
                f"check={shlex.quote(str(self.check))}\n",
        }
        for old, new in replacements.items():
            self.assertEqual(hook.count(old), 1, old)
            hook = hook.replace(old, new)
        self.hook = self.folder / "zz-msi-fan-profile-readiness"
        self.hook.write_text(hook, encoding="utf-8")
        self.environment = {"READINESS_TEST_ROOT": str(self.folder), "PATH": "/nonexistent"}
        self.configure("PASS dkms_build ok\nKERNEL READINESS PASSED: " + RELEASE + "\n", 0)

    def configure(self, output, status, sleep=0):
        (self.folder / "check-output").write_text(output, encoding="utf-8")
        (self.folder / "check-status").write_text(str(status), encoding="utf-8")
        (self.folder / "check-sleep").write_text(str(sleep), encoding="utf-8")

    def run_hook(self, *arguments, timeout=20, **environment):
        for name in ("check-arguments", "logger-calls"):
            (self.folder / name).unlink(missing_ok=True)
        return subprocess.run(
            [shutil.which("bash"), str(self.hook), *arguments], cwd=self.folder,
            env=dict(self.environment, **environment), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout)

    def logged(self):
        path = self.folder / "logger-calls"
        if not path.exists():
            return []
        return [entry.split("\0")[:-1] for entry in
                path.read_text(encoding="utf-8").splitlines() if entry]

    def priorities(self):
        calls = self.logged()
        for call in calls:
            self.assertEqual(call[:2], ["-t", "msi-fan-profile"])
            self.assertEqual(call[2], "-p")
            self.assertEqual(call[4], "--")
        return [(call[3], call[5]) for call in calls]

    def test_ready_kernel_is_logged_and_reported_quietly(self):
        result = self.run_hook(RELEASE, "/boot/vmlinuz-" + RELEASE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("readiness PASSED for kernel " + RELEASE, result.stdout)
        self.assertEqual((self.folder / "check-arguments").read_text(), RELEASE + "\n")
        self.assertEqual(self.priorities(), [
            ("user.info", f"kernel {RELEASE}: PASS dkms_build ok"),
            ("user.info", f"kernel {RELEASE}: KERNEL READINESS PASSED: {RELEASE}"),
            ("user.info", f"msi_ec readiness PASSED for kernel {RELEASE}"),
        ])

    def test_failed_readiness_warns_on_stderr_and_journal_but_exits_zero(self):
        self.configure("PASS secure_boot enabled\nFAIL module_signature module is unsigned\n"
                       "WARN other_kernel 7.0.0-28-generic has no build\n"
                       "KERNEL READINESS FAILED: do not reboot\n", 1)
        result = self.run_hook(RELEASE, "/boot/vmlinuz-" + RELEASE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"WARNING: kernel {RELEASE} is not ready", result.stderr)
        self.assertIn("FAIL module_signature module is unsigned", result.stderr)
        self.assertIn("WARN other_kernel", result.stderr)
        self.assertIn(f"Re-check with: {self.check} {RELEASE}", result.stderr)
        self.assertIn("OPERATIONS.md", result.stderr)
        priorities = self.priorities()
        self.assertIn(("user.err", f"kernel {RELEASE}: FAIL module_signature module is unsigned"),
                      priorities)
        self.assertIn(("user.warning",
                       f"kernel {RELEASE}: WARN other_kernel 7.0.0-28-generic has no build"),
                      priorities)
        self.assertEqual(priorities[-1][0], "user.err")
        self.assertIn(f"readiness FAILED for kernel {RELEASE} (status 1)", priorities[-1][1])

    def test_hook_never_fails_the_kernel_installation(self):
        cases = (
            ("missing kernel release", (), {}),
            ("missing check", (RELEASE,), {"remove_check": True}),
            ("logger failure", (RELEASE,), {"READINESS_TEST_LOGGER_STATUS": "1"}),
            ("check crash", (RELEASE,), {"status": 139}),
        )
        for name, arguments, options in cases:
            with self.subTest(name=name):
                self.configure("FAIL dkms_build no build\n", options.get("status", 1))
                if options.get("remove_check"):
                    self.check.rename(self.folder / "moved-check")
                environment = {key: value for key, value in options.items() if key.isupper()}
                result = self.run_hook(*arguments, **environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if options.get("remove_check"):
                    (self.folder / "moved-check").rename(self.check)
                    self.assertIn("is missing", self.priorities()[0][1])
                if not arguments:
                    self.assertFalse((self.folder / "check-arguments").exists())

    def test_a_hung_check_is_bounded_and_reported(self):
        hook = self.hook.read_text(encoding="utf-8")
        self.assertEqual(hook.count("timeout 120 "), 1)
        self.hook.write_text(hook.replace("timeout 120 ", "timeout 1 "), encoding="utf-8")
        self.configure("", 0, sleep=10)
        result = self.run_hook(RELEASE, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not ready", result.stderr)
        self.assertIn("(status 124)", self.priorities()[-1][1])



class KernelHookWithReadinessCheckTests(ReadinessFixture):
    def test_hook_reports_real_check_failures_and_still_exits_zero(self):
        self.fixture["fields"][self.fixture["modules"][RELEASE]["msi_ec"]]["sig_key"] = ""
        (self.folder / "fixture.json").write_text(json.dumps(self.fixture), encoding="utf-8")
        hook = HOOK.read_text(encoding="utf-8")
        for old, new in (("export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n",
                          f"export PATH={shlex.quote(self.environment['PATH'])}\n"),
                         ("check=/usr/local/libexec/msi-fan-kernel-readiness\n",
                          f"check={shlex.quote(str(self.check))}\n")):
            self.assertEqual(hook.count(old), 1, old)
            hook = hook.replace(old, new)
        path = self.folder / "zz-msi-fan-profile-readiness"
        path.write_text(hook, encoding="utf-8")
        result = subprocess.run(
            ["bash", str(path), RELEASE, "/boot/vmlinuz-" + RELEASE], env=self.environment,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL module_signature", result.stderr)
        calls = self.calls()
        journal = [call for call in calls if call[0] == "logger"]
        self.assertIn(["logger", "-t", "msi-fan-profile", "-p", "user.err", "--",
                       f"kernel {RELEASE}: FAIL module_signature     "
                       "module is unsigned; Secure Boot lockdown will refuse to load it"], journal)
        self.assertIn(["logger", "-t", "msi-fan-profile", "-p", "user.err", "--",
                       f"kernel {RELEASE}: KERNEL READINESS FAILED: do not reboot into "
                       f"{RELEASE} until every FAIL is resolved."], journal)
        self.assertTrue(all(call[0] in FAKE_COMMANDS + ("logger",) for call in calls), calls)


class KernelHookInstallationTests(unittest.TestCase):
    """Every deployed artifact is installed, recorded, upgraded and removed alike."""

    def install_targets(self):
        script = (ROOT / "scripts/install-profile.sh").read_text(encoding="utf-8")
        script = script.replace("\\\n", " ")
        installs = {}
        for line in script.splitlines():
            words = shlex.split(line) if line.startswith("install ") else []
            if words and "-d" not in words and not words[-1].endswith("/INSTALL_RECORD"):
                mode = words[words.index("-m") + 1]
                installs[words[-1]] = (words[-2].replace("$repo/", ""), mode)
        return script, installs

    def test_hook_name_runs_after_dkms_under_run_parts(self):
        self.assertRegex(HOOK.name, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(sorted(["dkms", "initramfs-tools", "unattended-upgrades",
                                 "xx-update-initrd-links", HOOK.name])[-1], HOOK.name)

    def test_hook_always_exits_zero(self):
        hook = HOOK.read_text(encoding="utf-8")
        self.assertEqual(hook.splitlines()[0], "#!/bin/bash")
        self.assertEqual(hook.rstrip().splitlines()[-1], "exit 0")
        self.assertEqual(re.findall(r"\bexit\s+(\S+)", hook), ["0", "0", "0"])
        self.assertNotRegex(hook, r"(?m)^\s*set\s+-")
        self.assertIn("export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n", hook)

    def test_install_upgrade_record_and_uninstall_cover_the_same_artifacts(self):
        script, installs = self.install_targets()
        self.assertEqual(installs["/usr/local/libexec/msi-fan-kernel-readiness"],
                         ("scripts/check-kernel-readiness.sh", "0755"))
        self.assertEqual(installs["/etc/kernel/postinst.d/zz-msi-fan-profile-readiness"],
                         ("kernel/postinst.d/zz-msi-fan-profile-readiness", "0755"))
        refused = re.search(r"\npaths=\(\n(.*?)\n\)\n", script, re.S).group(1).split()
        record = re.search(r"for target in (.*?); do", script, re.S).group(1).split()
        rollback = re.search(r"\nrollback\(\) \{\n(.*?)\n\}\n", script, re.S).group(1)
        uninstall = (ROOT / "scripts/uninstall-profile.sh").read_text(encoding="utf-8")
        upgrade = (ROOT / "scripts/upgrade-profile.sh").read_text(encoding="utf-8")

        def array(name):
            body = re.search(r"\n" + name + r"=\((.*?)\)\n", upgrade, re.S).group(1)
            return shlex.split(body.replace("$repo/", ""))

        upgraded = dict(zip(array("target_files"),
                            zip(array("source_files"), array("modes"))))
        must_exist = dict(zip(array("target_files"), array("must_exist")))
        self.assertEqual(upgraded, installs)

        def covered(paths, target):
            return any(target == path or target.startswith(path + "/") for path in paths)

        def removed(text, target):
            return covered([line.split()[-1] for line in text.splitlines()
                            if line.strip().startswith("rm ")], target)

        for target in installs:
            with self.subTest(target=target):
                self.assertTrue(covered(refused, target), "never overwritten")
                self.assertIn(target, record)
                self.assertTrue(removed(rollback, target), "install rollback")
                self.assertTrue(removed(uninstall, target), "uninstall")
        # Existing deployments predate the kernel hook; the upgrade adds it.
        for target in ("/usr/local/libexec/msi-fan-kernel-readiness",
                       "/etc/kernel/postinst.d/zz-msi-fan-profile-readiness"):
            self.assertEqual(must_exist[target], "no")

    def test_release_checks_lint_and_pin_the_new_artifacts(self):
        verify = (ROOT / "scripts/verify-release.sh").read_text(encoding="utf-8")
        self.assertIn("kernel_hook=kernel/postinst.d/zz-msi-fan-profile-readiness", verify)
        self.assertIn('bash -n scripts/*.sh "$kernel_hook"', verify)
        self.assertIn('shellcheck scripts/*.sh "$kernel_hook"', verify)
        manifest = (ROOT / "SHA256SUMS").read_text(encoding="utf-8")
        for path in ("scripts/check-kernel-readiness.sh", "kernel/postinst.d/" + HOOK.name,
                     "patches/msi-ec-0.13.1-legacy-fan-curve.patch",
                     "tests/test_kernel_readiness.py"):
            with self.subTest(path=path):
                self.assertRegex(manifest, r"(?m)^[0-9a-f]{64}  \./" + re.escape(path) + "$")


if __name__ == "__main__":
    unittest.main()
