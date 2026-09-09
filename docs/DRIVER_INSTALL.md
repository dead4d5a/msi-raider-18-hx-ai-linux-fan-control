# Driver installation: signed `msi_ec 0.13.1`

This guide installs the exact local `msi_ec` extension required by the profile
service while keeping Ubuntu Secure Boot enabled.

> [!CAUTION]
> Stop if any identity differs. Never use the driver's `firmware=` override,
> `debug=1`, `ec_sys write_support=1`, or raw EC tools to force compatibility.

## 1. Exact preflight

```bash
cat /etc/os-release
uname -r
cat /sys/class/dmi/id/product_name
cat /sys/class/dmi/id/board_name
cat /sys/class/dmi/id/bios_version
mokutil --sb-state
```

Required hardware identity:

```text
Raider 18 HX AI A2XWIG
MS-1824
E1824IMS.310
SecureBoot enabled
```

The EC version is checked by the driver after loading and must be exactly:

```text
1824EMS1.108
```

Check for conflicts:

```bash
lsmod | grep -E '^(msi_ec|ec_sys)[[:space:]]' || true
sudo dkms status
modprobe -c | grep -Ei \
  '^(options|install|remove|blacklist|softdep)[[:space:]]+(msi[-_]ec|ec_sys)([[:space:]]|$)' || true
```

Do not continue with unexplained modules, options, source directories, or
existing fan-control software.

## 2. Prerequisites

```bash
sudo apt update
sudo apt install \
  git ca-certificates dkms build-essential "linux-headers-$(uname -r)" \
  mokutil shim-signed openssl kmod initramfs-tools-core
```

Verify matching headers:

```bash
test -e "/lib/modules/$(uname -r)/build/include"
```

This laptop originally had a local APT rule blocking all `dkms` packages to
avoid NVIDIA DKMS modules. If you have a similar rule, narrow it to
`nvidia-dkms-*`; do not replace Ubuntu's `dkms` package with `dh-dkms`.

## 3. Enroll Ubuntu's local MOK if needed

Inspect DKMS signing overrides first:

```bash
sudo grep -RnsE \
  '^[[:space:]]*(sign_file|mok_signing_key|mok_certificate|modprobe_on_install)[[:space:]]*=' \
  /etc/dkms/framework.conf /etc/dkms/framework.conf.d 2>/dev/null || true
```

This guide assumes Ubuntu's defaults:

```text
/var/lib/shim-signed/mok/MOK.priv
/var/lib/shim-signed/mok/MOK.der
```

If both are absent:

```bash
sudo update-secureboot-policy --new-key
```

If only one exists, stop. Do not overwrite an incomplete pair blindly.

Validate the private/public pair:

```bash
sudo openssl pkey \
  -in /var/lib/shim-signed/mok/MOK.priv -check -noout

KEY_HASH=$(
  sudo openssl pkey \
    -in /var/lib/shim-signed/mok/MOK.priv -pubout -outform DER |
  sha256sum | awk '{print $1}'
)
CERT_HASH=$(
  sudo openssl x509 -inform DER \
    -in /var/lib/shim-signed/mok/MOK.der -pubkey -noout |
  openssl pkey -pubin -outform DER |
  sha256sum | awk '{print $1}'
)
test "$KEY_HASH" = "$CERT_HASH"
```

Check enrollment:

```bash
sudo mokutil --test-key /var/lib/shim-signed/mok/MOK.der
```

Ubuntu `mokutil 0.6.0` may print an informative state with a nonzero status, so
read the text. If not enrolled or queued, run interactively:

```bash
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
```

Choose a temporary password privately, reboot, and select:

1. **Enroll MOK**
2. **Continue**
3. **Yes**
4. Enter the temporary password
5. Reboot

Do not disable Secure Boot. After boot:

```bash
mokutil --sb-state
sudo mokutil --test-key /var/lib/shim-signed/mok/MOK.der
```

## 4. Fetch and verify the pinned upstream source

From the cloned profile repository, save its absolute path first:

```bash
PROFILE_REPO=$(pwd)
DRIVER_DIR="$HOME/src/msi-ec-profile-driver"
mkdir -p "$HOME/src"
git clone https://github.com/BeardOverflow/msi-ec.git "$DRIVER_DIR"
git -C "$DRIVER_DIR" checkout --detach \
  d7fbbd88e6831e56801b860e46475cbf8ddbc7c1
cd "$DRIVER_DIR"
```

Expected upstream identity:

```text
commit: d7fbbd88e6831e56801b860e46475cbf8ddbc7c1
tree:   072c10a344c22372665a729add6c10faada7c6da
archive SHA-256: ee49828ce69a9aa9c500d3efcb923d0ba77b570e1a83351647e074a4c039eb6b
```

Verify:

```bash
test "$(git rev-parse HEAD)" = \
  d7fbbd88e6831e56801b860e46475cbf8ddbc7c1
test "$(git rev-parse 'HEAD^{tree}')" = \
  072c10a344c22372665a729add6c10faada7c6da
test "$(git archive --format=tar HEAD | sha256sum | awk '{print $1}')" = \
  ee49828ce69a9aa9c500d3efcb923d0ba77b570e1a83351647e074a4c039eb6b
```

Apply this repository's reviewed patch:

```bash
PATCH="$PROFILE_REPO/patches/msi-ec-0.13.1-exact-fan-curve.patch"
sha256sum "$PATCH"
git apply --check "$PATCH"
git apply "$PATCH"
```

Patch SHA-256:

```text
16b421b04dd40a3b26f14438a8f95f83c2602692ac8700d40ccc5c6e5bb14a2d
```

Verify before applying:

The preceding `sha256sum "$PATCH"` command must match that value.

## 5. Unprivileged test build

```bash
make clean
make -j"$(nproc)"
modinfo ./msi-ec.ko | grep -E \
  '^(name|version|srcversion|vermagic|description|parm):'
```

Required module metadata:

```text
name:       msi_ec
version:    0.13.1
srcversion: AB0BFAE2391B5ADD66E01BD
```

`vermagic` must begin with the current `uname -r`. Only `firmware` and `debug`
parameters should exist; do not set either.

## 6. Explicit DKMS registration/build/install

Do not use the upstream convenience uninstall target; it can match multiple
versions. Stage and manage only `msi_ec/0.13.1` explicitly.

```bash
sudo install -d -m 0755 /usr/src/msi_ec-0.13.1
sudo install -m 0644 \
  Makefile Makefile.vars msi-ec.c ec_memory_configuration.h \
  /usr/src/msi_ec-0.13.1/
sed 's/@VERSION@/0.13.1/' dkms.conf |
  sudo tee /usr/src/msi_ec-0.13.1/dkms.conf >/dev/null
sudo chown -R root:root /usr/src/msi_ec-0.13.1

sudo dkms add -m msi_ec -v 0.13.1
sudo dkms build -m msi_ec -v 0.13.1 -k "$(uname -r)"
sudo dkms install -m msi_ec -v 0.13.1 -k "$(uname -r)"
```

The build transcript must name the intended MOK paths. Verify:

```bash
sudo dkms status -m msi_ec -v 0.13.1 -k "$(uname -r)"
modinfo -n msi_ec
modinfo -F version msi_ec
modinfo -F srcversion msi_ec
modinfo -F signer msi_ec
modinfo -F sig_key msi_ec
```

Expected path contains:

```text
/lib/modules/<kernel>/updates/dkms/msi-ec.ko
```

Do not proceed unless the version/source identity and signer match.

## 7. Deliberate first load

Do not load an unknown module over an already loaded instance.

```bash
lsmod | grep '^msi_ec' && echo 'STOP: already loaded'
sudo modprobe -v msi_ec
```

Verify:

```bash
cat /sys/module/msi_ec/version
cat /sys/module/msi_ec/srcversion
cat /sys/devices/platform/msi-ec/fw_version
cat /sys/devices/platform/msi-ec/fan_curve
```

Required values:

```text
0.13.1
AB0BFAE2391B5ADD66E01BD
1824EMS1.108
58 64 70 76 82 88 0 25 35 44 58 70 75 52 58 64 70 76 82 0 25 35 44 58 70 75
```

The last line is the captured factory curve. Stop if it differs.

Enable boot loading only after validation:

```bash
printf 'msi_ec\n' |
  sudo tee /etc/modules-load.d/90-msi-ec-local.conf >/dev/null
sudo chmod 0644 /etc/modules-load.d/90-msi-ec-local.conf
```

Reboot and repeat all version, signer, firmware, curve, and Secure Boot checks.

## 8. Targeted driver rollback

First stop/remove the profile service using
[Rollback](ROLLBACK.md). Ensure factory `auto/off` is active.

```bash
sudo rm -f /etc/modules-load.d/90-msi-ec-local.conf
sudo modprobe -r msi_ec
sudo dkms remove -m msi_ec -v 0.13.1 --all
```

Only after successful DKMS removal:

```bash
sudo rm -rf /usr/src/msi_ec-0.13.1
sudo depmod -a
sudo reboot
```

Do not delete the enrolled MOK merely to remove this driver. Do not delete the
DKMS source directory after a failed `dkms remove`.

For emergency recovery, add both to the GRUB kernel line for one boot:

```text
module_blacklist=msi_ec modprobe.blacklist=msi_ec
```

Then remove the loader and exact DKMS registration. Do not manually load the
blacklisted module during that recovery boot.
