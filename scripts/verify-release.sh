#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

verify_cache=$(mktemp -d)
cleanup() {
    rm -rf -- "$verify_cache"
}
trap cleanup EXIT

PYTHONPYCACHEPREFIX="$verify_cache" python3 -m py_compile \
    src/msi-fan-profile src/msi-fan-profiled src/msi-gpu-recover src/msi_fan_control.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$verify_cache" \
    python3 -m unittest discover -s tests -p 'test_*.py'
bash -n scripts/*.sh
if command -v shellcheck >/dev/null; then
    shellcheck scripts/*.sh
fi
python3 -m json.tool data/results.json >/dev/null
if git apply --check --reverse \
    patches/msi-ec-0.13.1-exact-fan-curve.patch >/dev/null 2>&1; then
    echo 'Unexpected: patch appears applied to the repository root.' >&2
    exit 1
fi

expected_patch=0b27ad8fb03613fe3bdbdc39598dc59c438c30f78c311eb125076f7562069f37
actual_patch=$(sha256sum patches/msi-ec-0.13.1-exact-fan-curve.patch | awk '{print $1}')
[[ $actual_patch == "$expected_patch" ]]

# Prohibit classes of files that must never be published.
if find . -path ./.git -prune -o -type f \
    \( -name '*.priv' -o -name '*.key' -o -name '*.pem' -o -name '*.der' \
       -o -name '*.bin' -o -name '*.ko' -o -name '*.log' -o -name '*.csv' \) \
    -print | grep -q .; then
    echo 'Forbidden private/binary/evidence file found.' >&2
    exit 1
fi

# All production constants must agree.
python3 - <<'PY'
import ast
from pathlib import Path
wanted = {
    "FACTORY": "58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75",
    "DEFAULT": "50 60 70 76 82 88 20 30 40 50 60 80 110 38 40 42 44 55 70 20 30 40 60 70 100 120",
}
for filename in ("src/msi-fan-profile", "src/msi-fan-profiled", "src/msi-gpu-recover"):
    tree = ast.parse(Path(filename).read_text())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    values[target.id] = ast.literal_eval(node.value)
    assert values == wanted, (filename, values)
recovery = ast.parse(Path("src/msi-gpu-recover").read_text())
recovery_curve_map = None
for node in recovery.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "FULL_COOLING_CURVES":
                recovery_curve_map = node.value
assert isinstance(recovery_curve_map, ast.Dict)
assert len(recovery_curve_map.keys) == len(recovery_curve_map.values) == 1
assert ast.literal_eval(recovery_curve_map.keys[0]) == "candidate14"
assert isinstance(recovery_curve_map.values[0], ast.Name)
assert recovery_curve_map.values[0].id == "DEFAULT"
print("profile constants: OK")

builds = {"LEGACY_SRCVERSION": "AB0BFAE2391B5ADD66E01BD",
          "SNAPSHOT_SRCVERSION": "9086C45007CBB7FA1264430"}
tree = ast.parse(Path("src/msi_fan_control.py").read_text())
actual = {target.id: ast.literal_eval(node.value)
          for node in tree.body if isinstance(node, ast.Assign)
          for target in node.targets
          if isinstance(target, ast.Name) and target.id in builds}
assert actual == builds, actual
for filename in ("scripts/preflight.sh", "scripts/uninstall-profile.sh"):
    script = Path(filename).read_text()
    assert all(source in script for source in builds.values()), filename
    assert "fan_control_snapshot" in script, filename
assert builds["SNAPSHOT_SRCVERSION"] in Path("scripts/verify-driver-build.sh").read_text()
print("exact driver ABI pins: OK")
PY

python3 - <<'PY'
from pathlib import Path

primary = Path("systemd/msi-fan-profile.service").read_text(encoding="utf-8")
keeper = Path("systemd/msi-fan-profile-keeper.service").read_text(encoding="utf-8")
assert "OnFailure=msi-fan-profile-keeper.service" in primary
assert "Restart=no" in primary
for setting in (
    "Conflicts=msi-fan-profile.service",
    "Before=msi-fan-profile.service",
    "ExecStart=/usr/local/libexec/msi-gpu-recover --keeper candidate14",
    "Restart=on-failure",
    "WatchdogSec=5s",
):
    assert setting in keeper, setting
assert "[Install]" not in keeper
print("failure keeper unit: OK")
PY

sha256sum -c SHA256SUMS >/dev/null

echo 'release verification: PASS'
