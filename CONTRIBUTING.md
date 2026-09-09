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
8. Failure injection and factory recovery.
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

Keep the production manager free of generic curve/address input. New profiles
must be named constants, reviewed, measured, and exact-platform gated.
