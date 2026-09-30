#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the actual patched snapshot callback against mocked EC operations."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


PATCH = pathlib.Path(__file__).parents[1] / "patches/msi-ec-0.13.1-exact-fan-curve.patch"


def patched_function(source: str, name: str) -> str:
    match = re.search(r"^static [^;{]*\b" + re.escape(name) + r"\([^;]*?\)\s*\{", source,
                      re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing patched function {name}")
    start = match.start()
    cursor = source.index("{", start)
    depth = 0
    for end in range(cursor, len(source)):
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
            if depth == 0:
                return source[start:end + 1]
    raise AssertionError(f"unterminated patched function {name}")


PREAMBLE = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <errno.h>
#include <sys/types.h>
typedef uint8_t u8;
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define BIT(n) (1U << (n))
#define DMI_PRODUCT_NAME 1
#define DMI_BOARD_NAME 2
#define DMI_BIOS_VERSION 3
#define MSI_EC_FW_VERSION_LENGTH 12
#define FAN_CURVE_TEMP_POINTS 6
#define FAN_CURVE_SPEED_POINTS 7
#define FAN_CURVE_TOTAL_VALUES 26
#define FAN_CURVE_CPU_TEMP_ADDR 0x6a
#define FAN_CURVE_CPU_SPEED_ADDR 0x72
#define FAN_CURVE_GPU_TEMP_ADDR 0x82
#define FAN_CURVE_GPU_SPEED_ADDR 0x8a
#define FAN_CURVE_MODE_ADDR 0xd4
#define FAN_CURVE_BOOST_ADDR 0x98
#define FAN_CURVE_BOOST_BIT 7
struct device {};
struct device_attribute {};
struct msi_ec_mode { const char *name; int value; };
struct mode_conf { int address; struct msi_ec_mode modes[5]; };
static struct {
    struct mode_conf shift_mode, fan_mode;
    struct { int address, bit; } cooler_boost;
    struct { int rt_temp_address; } cpu, gpu;
} conf = {
    .shift_mode = {0xd2, {{"turbo", 0xc4}, {"eco", 0xc2}, {"comfort", 0xc1}, {NULL, 0}}},
    .fan_mode = {0xd4, {{"auto", 0x0d}, {"silent", 0x1d}, {"advanced", 0x8d}, {NULL, 0}}},
    .cooler_boost = {0x98, 7}, .cpu = {0x68}, .gpu = {0x80},
};
static u8 memory[256];
static bool fan_curve_supported = true, platform_matches = true;
static int fan_curve_mutex, locked, reads, lock_errors, fail_at;
static void mutex_lock(int *unused) { (void)unused; if (locked) lock_errors++; locked = 1; }
static void mutex_unlock(int *unused) { (void)unused; if (!locked) lock_errors++; locked = 0; }
static bool dmi_match(int field, const char *value) { (void)field; (void)value; return platform_matches; }
static int ec_read(u8 address, u8 *value) {
    if (!locked) lock_errors++;
    if (++reads == fail_at) return -EIO;
    *value = memory[address];
    return 0;
}
static int ec_read_seq(u8 address, u8 *values, u8 length) {
    for (u8 i = 0; i < length; i++) {
        int result = ec_read(address + i, values + i);
        if (result < 0) return result;
    }
    return 0;
}
static int ec_get_firmware_version(u8 *version) {
    memset(version, 0, MSI_EC_FW_VERSION_LENGTH + 1);
    int result = ec_read_seq(0xa0, version, MSI_EC_FW_VERSION_LENGTH);
    return result < 0 ? result : MSI_EC_FW_VERSION_LENGTH + 1;
}
static const char *str_on_off(bool value) { return value ? "on" : "off"; }
static int sysfs_emit(char *buffer, const char *format, ...) {
    va_list arguments;
    va_start(arguments, format);
    int count = vsnprintf(buffer, 4096, format, arguments);
    va_end(arguments);
    return count;
}
static int sysfs_emit_at(char *buffer, int offset, const char *format, ...) {
    va_list arguments;
    va_start(arguments, format);
    int count = vsnprintf(buffer + offset, 4096 - offset, format, arguments);
    va_end(arguments);
    return count;
}
'''


HARNESS = r'''
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    memcpy(memory + 0xa0, "1824EMS1.108", 12);
    for (int i = 0; i < 6; i++) memory[0x6a + i] = 50 + i;
    for (int i = 0; i < 7; i++) memory[0x72 + i] = 20 + i;
    for (int i = 0; i < 6; i++) memory[0x82 + i] = 38 + i;
    for (int i = 0; i < 7; i++) memory[0x8a + i] = 30 + i;
    memory[0xd2] = 0xc1; memory[0xd4] = 0x8d; memory[0x98] = 0x80;
    memory[0x68] = 54; memory[0x80] = 40;
    if (!strcmp(argv[1], "sentinel")) memory[0x68] = memory[0x80] = 255;
    if (!strcmp(argv[1], "unspecified")) memory[0xd2] = 0x80;
    if (!strcmp(argv[1], "unknown-mode")) memory[0xd4] = 0xff;
    if (!strcmp(argv[1], "unknown-shift")) memory[0xd2] = 0xff;
    if (!strcmp(argv[1], "unsupported-platform")) platform_matches = false;
    if (!strcmp(argv[1], "unsupported-attribute")) fan_curve_supported = false;
    if (!strcmp(argv[1], "bad-config")) conf.cpu.rt_temp_address = 0x69;
    if (!strcmp(argv[1], "wrong-firmware")) memory[0xa0] = '9';
    if (!strncmp(argv[1], "io-", 3)) fail_at = atoi(argv[1] + 3);
    char output[4096] = {0};
    int result = fan_control_snapshot_show(NULL, NULL, output);
    if (!strcmp(argv[1], "firmware-changed-between-reads")) {
        if (result <= 0 || reads != 43 || locked || lock_errors) return 3;
        memset(output, 0, sizeof(output)); reads = 0;
        memory[0xa0] = '9';
        result = fan_control_snapshot_show(NULL, NULL, output);
    }
    printf("%d %d %d %d\n", result, reads, locked, lock_errors);
    if (result >= 0) fputs(output, stdout);
    else if (output[0]) return 4; /* Errors must never publish a partial snapshot. */
    return 0;
}
'''


class DriverSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if compiler is None:
            raise unittest.SkipTest("C compiler unavailable for hardware-free snapshot harness")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = pathlib.Path(cls.temporary.name)
        cls.patch = PATCH.read_text()
        cls.source = "\n".join(line[1:] for line in cls.patch.splitlines()
                               if line.startswith("+") and not line.startswith("+++"))
        names = (
            "fan_curve_exact_platform", "fan_curve_check_firmware",
            "fan_curve_configuration_matches", "fan_curve_read_values",
            "fan_control_snapshot_configuration_matches", "fan_control_snapshot_mode_name",
            "fan_control_snapshot_show",
        )
        harness = PREAMBLE + "\n" + "\n\n".join(patched_function(cls.source, name) for name in names) + HARNESS
        source_path = cls.directory / "snapshot.c"
        cls.binary = cls.directory / "snapshot"
        source_path.write_text(harness)
        result = subprocess.run([compiler, "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                                 "-Wno-unused-parameter", "-Wno-pointer-sign",
                                 str(source_path), "-o", str(cls.binary)],
                                text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def snapshot(self, scenario):
        result = subprocess.run([str(self.binary), scenario], text=True, capture_output=True,
                                timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata, _, payload = result.stdout.partition("\n")
        value, reads, locked, lock_errors = map(int, metadata.split())
        self.assertEqual((locked, lock_errors), (0, 0))
        return value, reads, payload

    def test_snapshot_has_exact_schema_and_43_locked_fresh_reads(self):
        value, reads, payload = self.snapshot("normal")
        self.assertEqual(reads, 43)
        self.assertEqual(value, len(payload))
        self.assertTrue(payload.isascii())
        self.assertEqual(payload.count("\n"), 1)
        self.assertTrue(payload.endswith("\n"))
        self.assertNotIn("\x00", payload)
        tokens = payload.split()
        self.assertEqual(len(tokens), 33)
        self.assertEqual(tokens[:7], ["1", "1824EMS1.108", "comfort", "advanced", "on", "54", "40"])
        self.assertEqual(list(map(int, tokens[7:])),
                         list(range(50, 56)) + list(range(20, 27)) +
                         list(range(38, 44)) + list(range(30, 37)))

    def test_temperature_sentinels_are_exposed_without_reinterpretation(self):
        value, reads, payload = self.snapshot("sentinel")
        self.assertGreater(value, 0)
        self.assertEqual(reads, 43)
        self.assertEqual(payload.split()[5:7], ["255", "255"])

    def test_existing_unspecified_shift_semantics_are_preserved(self):
        self.assertEqual(self.snapshot("unspecified")[2].split()[2], "unspecified")

    def test_unknown_control_modes_fail_without_partial_output(self):
        for scenario in ("unknown-mode", "unknown-shift"):
            with self.subTest(scenario=scenario):
                value, reads, payload = self.snapshot(scenario)
                self.assertLess(value, 0)
                self.assertEqual(reads, 43)
                self.assertEqual(payload, "")

    def test_platform_configuration_and_support_gates_precede_curve_reads(self):
        for scenario in ("unsupported-platform", "unsupported-attribute", "bad-config"):
            with self.subTest(scenario=scenario):
                value, reads, payload = self.snapshot(scenario)
                self.assertLess(value, 0)
                self.assertEqual((reads, payload), (0, ""))

    def test_firmware_mismatch_and_later_change_fail_after_one_fresh_check(self):
        for scenario in ("wrong-firmware", "firmware-changed-between-reads"):
            with self.subTest(scenario=scenario):
                value, reads, payload = self.snapshot(scenario)
                self.assertLess(value, 0)
                self.assertEqual((reads, payload), (12, ""))

    def test_every_firmware_curve_control_and_temperature_io_failure_propagates(self):
        for position in range(1, 44):
            with self.subTest(position=position):
                value, reads, payload = self.snapshot(f"io-{position}")
                self.assertLess(value, 0)
                self.assertEqual((reads, payload), (position, ""))

    def test_attribute_is_read_only_and_uses_the_exact_visibility_gate(self):
        self.assertIn("static DEVICE_ATTR_RO(fan_control_snapshot);", self.source)
        self.assertNotIn("DEVICE_ATTR_RW(fan_control_snapshot)", self.source)
        self.assertIn("&dev_attr_fan_control_snapshot.attr,", self.source)
        self.assertRegex(self.source, r"attr == &dev_attr_fan_control_snapshot\.attr\)\s*"
                         r"return fan_curve_supported && fan_control_snapshot_configuration_matches\(\)")
        callback = patched_function(self.source, "fan_control_snapshot_show")
        self.assertNotIn("ec_write", callback)
        self.assertNotIn("fan_curve_write", callback)


if __name__ == "__main__":
    unittest.main()
