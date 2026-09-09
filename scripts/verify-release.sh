#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
    src/msi-fan-profile src/msi-fan-profiled src/msi-gpu-recover
rm -rf src/__pycache__
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

expected_patch=16b421b04dd40a3b26f14438a8f95f83c2602692ac8700d40ccc5c6e5bb14a2d
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
for filename in ("src/msi-fan-profile", "src/msi-fan-profiled"):
    tree = ast.parse(Path(filename).read_text())
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    values[target.id] = ast.literal_eval(node.value)
    assert values == wanted, (filename, values)
print("profile constants: OK")
PY

sha256sum -c SHA256SUMS >/dev/null

echo 'release verification: PASS'
