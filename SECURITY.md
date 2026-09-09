# Security policy

## Supported configuration

Security and safety reports are accepted only for the exact configuration in the
README unless the issue concerns repository tooling independent of hardware.

## Reporting a vulnerability or unsafe behavior

Do not publish an exploit or potentially unsafe EC write sequence in a public
issue before maintainers have reviewed it. Use GitHub's private vulnerability
reporting feature for:

- an EC write escaping the fixed allowlist;
- identity-gate bypass;
- unsafe curve acceptance;
- lock/override race;
- watchdog or recovery bypass;
- privilege escalation in install/manager scripts;
- a state that can leave cooling disabled or under-responsive.

For an immediate thermal event:

1. Stop the workload.
2. Use the firmware Cooler Boost keyboard shortcut if available.
3. Power off if physical fan response is abnormal.
4. Do not experiment with additional EC writes.

## Hard safety boundaries

This project intentionally does not support:

- `msi_ec firmware=` overrides;
- `msi_ec debug=1`;
- generic raw EC writes;
- `ec_sys write_support=1`;
- runtime user-provided curves;
- automatic restart after a profile service failure.

Pull requests weakening these defaults require exceptional evidence and will
normally be rejected.
