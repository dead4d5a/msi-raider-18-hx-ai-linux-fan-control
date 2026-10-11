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
bash -n scripts/*.sh kernel/postinst.d/zz-msi-fan-profile-readiness
shellcheck scripts/*.sh kernel/postinst.d/zz-msi-fan-profile-readiness
python3 -m json.tool data/results.json >/dev/null
./scripts/verify-release.sh
```

Run the release checks as a normal user; `verify-release.sh` refuses root. The
install and upgrade scripts drop privileges before running it.

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
./scripts/verify-driver-build.sh /path/to/pinned-msi-ec-checkout "$(uname -r)" legacy
```

The optional third argument selects the current `snapshot` patch (default) or
the deployed `legacy` patch. The script requires upstream commit
`d7fbbd88e6831e56801b860e46475cbf8ddbc7c1` and verifies its tree/archive and
the selected patch's digest. It builds only a temporary copy, checks module
identity/vermagic, and removes the temporary build. Source checkout and
installed modules remain untouched. This is a build check, not a simulated EC
transaction fault test or thermal compatibility claim.

CI builds both patches against the 24.04 GA headers (`linux-headers-generic`)
and the HWE series this laptop tracks (`linux-headers-generic-hwe-24.04`), on
every push and pull request and weekly, so a newly published kernel is built
without a push. `scripts/select-ci-kernel.sh [METAPACKAGE]` resolves the target
from that installed metapackage's exact dependency; it accepts only those two
metapackages. It must not pick the runner's running or highest-version kernel:
a cloud/Azure kernel may lack the ACPI battery-hook exports that `msi_ec`
requires. The verifier checks the header release, module/battery configuration
and symbol exports before building; it never suppresses unresolved-symbol
failures or relaxes runtime ABI pins. Kbuild's source-version calculation
includes the local configuration header with the absolute dependency layout of
kernels before 6.13 (CI's 6.8 GA headers), but not with the relative layout of
6.13 and later, so the verifier accepts exactly one checksum per patch and
layout:

| Patch | 6.13+ relative layout (runtime pin) | Before 6.13, absolute layout (CI only) |
| --- | --- | --- |
| snapshot | `9086C45007CBB7FA1264430` | `123635EB33D32BA9FCFABCA` |
| legacy | `AB0BFAE2391B5ADD66E01BD` | `518BD36E89E2B3026A56490` |

The legacy absolute-layout value was computed with the unchanged Linux checksum
implementation from the 7.0 headers, which reproduces the other three measured
values from the same sources. Archive/patch digests, name, version, and target
vermagic remain mandatory. The CI checksums are **not** added to the
controller's runtime allowlist, and a successful CI build is not hardware
validation or permission to deploy a different module.

Keep the production manager free of generic curve/address input. New profiles
must be named constants, reviewed, measured, and exact-platform gated.
