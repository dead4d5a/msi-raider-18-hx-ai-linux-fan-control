# Contributing

## Safety first

Do not ask users to probe arbitrary EC addresses or force another firmware table.
Do not submit broad “supports MSI Raider” claims.

## Supporting another exact configuration

Open an issue with nonsecret output from:

```bash
cat /sys/class/dmi/id/product_name
cat /sys/class/dmi/id/board_name
cat /sys/class/dmi/id/bios_version
cat /sys/devices/platform/msi-ec/fw_version
uname -r
```

Do **not** include serial numbers, private keys, tokens, full system journals, or
raw EC dumps in a public issue.

A support pull request should include:

1. Exact DMI/BIOS/EC identity.
2. Upstream driver base commit.
3. Read-only capture of the original curve, with sensitive metadata removed.
4. A narrow exact-platform driver interface or upstream-supported equivalent.
5. Transactional write/readback/rollback behavior.
6. Physical two-fan RPM validation.
7. Idle, CPU, GPU, and combined measurements.
8. Failure injection and full-cooling runtime recovery.
9. Reboot and suspend/resume validation.
10. Documentation that clearly separates tested and untested variants.

## Code checks

Before submitting:

```bash
python3 -m py_compile src/msi-fan-profile src/msi-fan-profiled src/msi-gpu-recover
bash -n scripts/*.sh
shellcheck scripts/*.sh
python3 -m json.tool data/results.json >/dev/null
./scripts/verify-release.sh
```

The default suite uses mocks and skips real systemd integration. To exercise
lifecycle behavior using only UUID-named user services and temporary fake files:

```bash
MSI_FANCTL_SYSTEMD_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p test_systemd_integration.py -v
```

This requires a reachable non-root `systemd --user` manager. The test cleans up
only its generated units; it never starts/stops the installed controller or
writes sysfs. It does not validate the production privileged sandbox or hardware.

With a clean checkout of the pinned upstream driver and matching installed
kernel headers, verify application/build without installing or loading:

```bash
./scripts/verify-driver-build.sh /path/to/pinned-msi-ec-checkout "$(uname -r)"
```

The script requires upstream commit `d7fbbd88e6831e56801b860e46475cbf8ddbc7c1`
and verifies its tree/archive and patch digests. It builds only a temporary
copy, checks module identity/vermagic, and removes the temporary build. Source
checkout and installed modules remain untouched. This is a build check, not a
simulated EC transaction fault test or thermal compatibility claim.

CI resolves its build target from the installed `linux-headers-generic`
metapackage's exact dependency with `scripts/select-ci-kernel.sh`. It must not
pick the runner's running or highest-version kernel: a cloud/Azure kernel may
lack the ACPI battery-hook exports that `msi_ec` requires. The verifier checks
the header release, module/battery configuration and symbol exports before
building; it never suppresses unresolved-symbol failures or relaxes ABI pins.

Keep the production manager free of generic curve/address input. New profiles
must be named constants, reviewed, measured, and exact-platform gated.
