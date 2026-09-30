#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise CI header selection and build preflight without host kernel access.

Package metadata is supplied by a fake dpkg-query. Build-header paths are
relocated into private temporary directories, and external commands are replaced
by tripwires or fixture metadata. Valid preflight stops at the first fake git
call; post-build checks use fake modinfo. These checks never compile, install,
load, or inspect a real kernel module.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).parents[1]
RELEASE = "6.8.0-101-generic"
HEADERS_PACKAGE = "linux-headers-" + RELEASE
INSTALLED = "install ok installed"
SNAPSHOT_SRCVERSION = "9086C45007CBB7FA1264430"
CI_68_SRCVERSION = "123635EB33D32BA9FCFABCA"
EXPORTS = "".join(
    f"0x01234567\t{symbol}\tdrivers/acpi/battery\tEXPORT_SYMBOL_GPL\t\n"
    for symbol in ("battery_hook_register", "battery_hook_unregister")
)

COMMAND_SHIM = textwrap.dedent(r'''\
#!/usr/bin/env python3
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["CI_DRIVER_TEST_ROOT"])
command = pathlib.Path(sys.argv[0]).name
arguments = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="ascii") as stream:
    stream.write(json.dumps([command, *arguments]) + "\n")
if command == "modinfo" and (root / "module-info.json").is_file():
    expected_module = root / "module-build/driver/msi-ec.ko"
    if len(arguments) != 3 or arguments[0] != "-F" or arguments[2] != str(expected_module):
        print("Unexpected mocked module query", file=sys.stderr)
        raise SystemExit(1)
    fields = json.loads((root / "module-info.json").read_text(encoding="ascii"))
    sys.stdout.write(fields.get(arguments[1], "") + "\n")
    raise SystemExit(0)
if command != "dpkg-query":
    print("Blocked test tripwire: " + command, file=sys.stderr)
    raise SystemExit(73)

metadata = json.loads((root / "packages.json").read_text(encoding="ascii"))
package = arguments[-1]
field = next((argument[3:] for argument in arguments if argument.startswith("-f=")), None)
if field is None and "-f" in arguments:
    field = arguments[arguments.index("-f") + 1]
if field not in ("${Status}", "${Depends}") or package not in metadata:
    print("Missing or unexpected package query", file=sys.stderr)
    raise SystemExit(1)
key = "status" if field == "${Status}" else "depends"
value = metadata[package]
if value.get("error_" + key):
    raise SystemExit(value["error_" + key])
sys.stdout.write(value.get(key, ""))
''').lstrip("\\\n")


class TemporaryCommandFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="msi-fanctl-ci-driver-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = pathlib.Path(temporary.name)
        self.commands = self.folder / "mock-bin"
        self.commands.mkdir()
        (self.folder / "calls.jsonl").touch()
        for command in ("dpkg-query", "git", "make", "modinfo", "find", "uname"):
            path = self.commands / command
            path.write_text(COMMAND_SHIM, encoding="ascii")
            path.chmod(0o755)
        self.environment = dict(os.environ)
        self.environment.update(
            CI_DRIVER_TEST_ROOT=str(self.folder),
            PATH=str(self.commands) + os.pathsep + os.environ["PATH"],
            PYTHONDONTWRITEBYTECODE="1",
        )

    def calls(self):
        return [json.loads(line) for line in
                (self.folder / "calls.jsonl").read_text(encoding="ascii").splitlines()]

    def run_script(self, script, *arguments):
        return subprocess.run(
            ["bash", str(script), *map(str, arguments)], cwd=self.folder,
            env=self.environment, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=10,
        )


class GenericKernelSelectionTests(TemporaryCommandFixture):
    def select(self, *, dependencies=HEADERS_PACKAGE,
               meta_status=INSTALLED, concrete_status=INSTALLED,
               meta_error=None, dependencies_error=None, extra_packages=None):
        packages = {
            "linux-headers-generic": {"status": meta_status, "depends": dependencies},
            HEADERS_PACKAGE: {"status": concrete_status},
        }
        if meta_error is not None:
            packages["linux-headers-generic"]["error_status"] = meta_error
        if dependencies_error is not None:
            packages["linux-headers-generic"]["error_depends"] = dependencies_error
        packages.update(extra_packages or {})
        (self.folder / "packages.json").write_text(json.dumps(packages), encoding="ascii")
        (self.folder / "calls.jsonl").write_text("", encoding="ascii")
        return self.run_script(ROOT / "scripts/select-ci-kernel.sh")

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "", "failure must not return a fallback release")
        self.assertTrue(all(call[0] == "dpkg-query" for call in self.calls()), self.calls())

    def test_exact_meta_dependency_wins_over_newer_azure_and_generic_packages(self):
        result = self.select(
            dependencies="libc6 (>= 2.39), " + HEADERS_PACKAGE + " (= 6.8.0-101.101)",
            extra_packages={
                "linux-headers-6.17.0-1001-azure": {"status": INSTALLED},
                "linux-headers-7.0.0-34-generic": {"status": INSTALLED},
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, RELEASE + "\n")
        self.assertEqual(self.calls(), [
            ["dpkg-query", "-W", "-f=${Status}", "linux-headers-generic"],
            ["dpkg-query", "-W", "-f=${Depends}", "linux-headers-generic"],
            ["dpkg-query", "-W", "-f=${Status}", HEADERS_PACKAGE],
        ])

    def test_native_versioned_dependency_without_version_constraint_is_accepted(self):
        result = self.select(dependencies="  " + HEADERS_PACKAGE + "  ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, RELEASE + "\n")

    def test_missing_or_uninstalled_meta_package_stops_before_dependency_query(self):
        for options in ({"meta_error": 1}, {"meta_status": "deinstall ok config-files"},
                        {"meta_status": "install ok unpacked"}, {"meta_status": ""}):
            with self.subTest(options=options):
                self.assert_rejected(self.select(**options))
                self.assertEqual(len(self.calls()), 1)

    def test_dependency_query_failure_has_no_kernel_fallback(self):
        self.assert_rejected(self.select(dependencies_error=2))
        self.assertEqual(len(self.calls()), 2)

    def test_no_exact_native_dependency_and_malformed_metadata_are_rejected(self):
        for dependencies in (
            "", "linux-headers-generic", "linux-headers-6.17.0-1001-azure",
            "linux-headers-6.8.0-101", HEADERS_PACKAGE + ":amd64",
            HEADERS_PACKAGE + "-extra", "not-" + HEADERS_PACKAGE,
            HEADERS_PACKAGE + " | linux-headers-other", HEADERS_PACKAGE + " (= 1",
            HEADERS_PACKAGE + "; echo injected", "linux-headers-../../escape-generic",
        ):
            with self.subTest(dependencies=dependencies):
                self.assert_rejected(self.select(dependencies=dependencies))

    def test_multiple_versioned_generic_dependencies_are_rejected(self):
        for dependencies in (
            HEADERS_PACKAGE + ", linux-headers-7.0.0-34-generic",
            HEADERS_PACKAGE + ", " + HEADERS_PACKAGE,
        ):
            with self.subTest(dependencies=dependencies):
                self.assert_rejected(self.select(dependencies=dependencies))

    def test_selected_dependency_must_itself_be_fully_installed(self):
        for status in ("deinstall ok config-files", "install ok unpacked", ""):
            with self.subTest(status=status):
                self.assert_rejected(self.select(concrete_status=status))
        self.assert_rejected(self.select(extra_packages={HEADERS_PACKAGE: {"error_status": 1}}))

    def test_workflow_uses_dependency_selector_not_global_header_scan(self):
        workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="ascii")
        self.assertIn("./scripts/select-ci-kernel.sh", workflow)
        self.assertNotRegex(workflow, r"find\s+/lib/modules")
        selected = re.search(r"([A-Za-z_][A-Za-z0-9_]*)=\$\(\./scripts/select-ci-kernel\.sh\)",
                             workflow)
        self.assertIsNotNone(selected, "capture the selector output, not a global directory scan")
        variable = selected.group(1)
        self.assertRegex(
            workflow,
            r"\./scripts/verify-driver-build\.sh\s+\.driver-build-source\s+\"\$(?:" +
            re.escape(variable) + r"|\{" + re.escape(variable) + r"\})\"",
        )


class DriverHeaderPreflightTests(TemporaryCommandFixture):
    def setUp(self):
        super().setUp()
        self.headers = self.folder / "headers"
        (self.headers / "include/config").mkdir(parents=True)
        (self.headers / "Makefile").write_text("# fake; make must never reach it\n", encoding="ascii")
        (self.headers / "include/config/kernel.release").write_text(RELEASE + "\n", encoding="ascii")
        self.set_config()
        (self.headers / "Module.symvers").write_text(EXPORTS, encoding="ascii")
        self.source = self.folder / "upstream"
        self.source.mkdir()
        script_directory = self.folder / "release/scripts"
        script_directory.mkdir(parents=True)
        original = (ROOT / "scripts/verify-driver-build.sh").read_text(encoding="ascii")
        assignment = "kernel_build=/lib/modules/$kernel_release/build"
        self.assertEqual(original.count(assignment), 1, "relocate the only host-header path")
        self.script = script_directory / "verify-driver-build.sh"
        self.script.write_text(
            original.replace(assignment, "kernel_build=" + shlex.quote(str(self.headers))),
            encoding="ascii",
        )

    def set_config(self, modules="y", battery="m"):
        settings = []
        if modules is not None:
            settings.append("CONFIG_MODULES=" + modules)
        if battery is not None:
            settings.append("CONFIG_ACPI_BATTERY=" + battery)
        (self.headers / ".config").write_text("\n".join(settings) + "\n", encoding="ascii")

    def verify(self):
        (self.folder / "calls.jsonl").write_text("", encoding="ascii")
        return self.run_script(self.script, self.source, RELEASE)

    def assert_preflight_rejected(self):
        result = self.verify()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [], "preflight must reject before git, make, or modinfo")
        self.assertTrue(result.stderr.strip(), "report an actionable preflight failure")

    def test_builtin_and_modular_battery_configs_reach_only_fake_git(self):
        for battery in ("y", "m"):
            with self.subTest(battery=battery):
                self.set_config(battery=battery)
                result = self.verify()
                self.assertNotEqual(result.returncode, 0, "fake git deliberately stops the build")
                self.assertEqual(self.calls(), [
                    ["git", "-C", str(self.source), "rev-parse", "HEAD"],
                ])

    def test_missing_makefile_is_rejected(self):
        (self.headers / "Makefile").unlink()
        self.assert_preflight_rejected()

    def test_missing_or_mismatched_release_is_rejected(self):
        path = self.headers / "include/config/kernel.release"
        for value in ("6.17.0-1001-azure\n", "", RELEASE + " extra\n"):
            with self.subTest(value=value):
                path.write_text(value, encoding="ascii")
                self.assert_preflight_rejected()
        path.unlink()
        self.assert_preflight_rejected()

    def test_missing_kernel_config_is_rejected(self):
        (self.headers / ".config").unlink()
        self.assert_preflight_rejected()

    def test_module_and_battery_config_requirements_are_not_optional(self):
        for modules, battery in (("n", "m"), ("m", "m"), (None, "m"),
                                 ("y", "n"), ("y", None)):
            with self.subTest(modules=modules, battery=battery):
                self.set_config(modules=modules, battery=battery)
                self.assert_preflight_rejected()

    def test_missing_or_empty_export_table_is_rejected(self):
        path = self.headers / "Module.symvers"
        path.write_text("", encoding="ascii")
        self.assert_preflight_rejected()
        path.unlink()
        self.assert_preflight_rejected()

    def test_each_battery_hook_export_is_required_in_exact_second_field(self):
        path = self.headers / "Module.symvers"
        for missing in ("battery_hook_register", "battery_hook_unregister"):
            for replacement in ("devm_" + missing, missing + "_suffix"):
                with self.subTest(missing=missing, replacement=replacement):
                    path.write_text(EXPORTS.replace("\t" + missing + "\t",
                                                    "\t" + replacement + "\t"), encoding="ascii")
                    self.assert_preflight_rejected()
        path.write_text(
            "battery_hook_register\tunrelated\tmodule\tEXPORT_SYMBOL_GPL\n"
            "battery_hook_unregister\tunrelated\tmodule\tEXPORT_SYMBOL_GPL\n",
            encoding="ascii",
        )
        self.assert_preflight_rejected()


class OfflineModuleIdentityTests(TemporaryCommandFixture):
    def setUp(self):
        super().setUp()
        self.module_directory = self.folder / "module-build"
        self.module = self.module_directory / "driver/msi-ec.ko"
        original = (ROOT / "scripts/verify-driver-build.sh").read_text(encoding="ascii")
        boundary = "\nmodule=$verify_dir/driver/msi-ec.ko\n"
        self.assertEqual(original.count(boundary), 1, "execute the actual post-build verification tail")
        tail = original.partition(boundary)[2]
        self.script = self.folder / "verify-module-tail.sh"
        self.script.write_text(
            "set -Eeuo pipefail\n"
            "verify_dir=" + shlex.quote(str(self.module_directory)) + "\n"
            "kernel_release=" + shlex.quote(RELEASE) + "\n"
            "module=$verify_dir/driver/msi-ec.ko\n" + tail,
            encoding="ascii",
        )

    def verify_identity(self, **overrides):
        fields = {"name": "msi_ec", "version": "0.13.1",
                  "srcversion": SNAPSHOT_SRCVERSION,
                  "vermagic": RELEASE + " SMP preempt mod_unload modversions "}
        fields.update(overrides)
        (self.folder / "module-info.json").write_text(json.dumps(fields), encoding="ascii")
        (self.folder / "calls.jsonl").write_text("", encoding="ascii")
        self.assertFalse(self.module.exists(), "there is no real module for modinfo to inspect")
        result = self.run_script(self.script)
        self.assertTrue(self.calls(), "actual tail must query the fake metadata")
        self.assertTrue(all(call[0] == "modinfo" and call[-1] == str(self.module)
                            for call in self.calls()), self.calls())
        self.assertFalse(self.module.exists(), "verification must not generate a module")
        return result

    def test_both_exact_offline_build_checksums_are_accepted(self):
        for source in (SNAPSHOT_SRCVERSION, CI_68_SRCVERSION):
            with self.subTest(source=source):
                result = self.verify_identity(srcversion=source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("verification: PASS", result.stdout)

    def test_unknown_empty_or_extended_source_checksum_is_rejected(self):
        for source in ("", "0" * 24, CI_68_SRCVERSION + "0", CI_68_SRCVERSION.lower()):
            with self.subTest(source=source):
                result = self.verify_identity(srcversion=source)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("verification: PASS", result.stdout)

    def test_both_known_checksums_still_require_name_version_and_vermagic(self):
        for source in (SNAPSHOT_SRCVERSION, CI_68_SRCVERSION):
            for field, value in (("name", "untrusted_driver"), ("version", "0.13.2"),
                                 ("vermagic", "6.17.0-1001-azure SMP "),
                                 ("vermagic", RELEASE + "-other SMP "),
                                 ("vermagic", "")):
                with self.subTest(source=source, field=field, value=value):
                    result = self.verify_identity(srcversion=source, **{field: value})
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("verification: PASS", result.stdout)

    def test_ci_only_checksum_does_not_expand_runtime_build_trust(self):
        tree = ast.parse((ROOT / "src/msi_fan_control.py").read_text(encoding="ascii"))
        assignments = {target.id: node.value
                       for node in tree.body if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name)}
        supported_node = assignments["SUPPORTED_SRCVERSIONS"]
        self.assertIsInstance(supported_node, (ast.Tuple, ast.List))
        supported = tuple(ast.literal_eval(assignments[item.id]) if isinstance(item, ast.Name)
                          else ast.literal_eval(item) for item in supported_node.elts)
        self.assertEqual(supported, ("AB0BFAE2391B5ADD66E01BD", SNAPSHOT_SRCVERSION))
        self.assertNotIn(CI_68_SRCVERSION, supported)


if __name__ == "__main__":
    unittest.main()
