# Candidate 14 profile

## EC representation

The MSI advanced curve contains seven speed values and six temperature
thresholds for each controller:

```text
CPU temperatures[6], CPU speeds[7], GPU temperatures[6], GPU speeds[7]
```

Candidate 14 is:

```text
50 60 70 76 82 88
20 30 40 50 60 80 110
38 40 42 44 55 70
20 30 40 60 70 100 120
```

| Side | Temperature thresholds | Speed values |
|---|---|---|
| CPU fan | 50, 60, 70, 76, 82, 88°C | 20, 30, 40, 50, 60, 80, 110 |
| GPU/shared fan | 38, 40, 42, 44, 55, 70°C | 20, 30, 40, 60, 70, 100, 120 |

The captured factory curve is:

```text
CPU: 58 64 70 76 82 88 -> 0 25 35 44 58 70 75
GPU: 52 58 64 70 76 82 -> 0 25 35 44 58 70 75
```

## Why the sides differ

The two fans share chassis heat paths, but MSI's stored advanced curve treats
them independently. Under CPU-only load, the original GPU-side fan could stop
because its local temperature remained below the first threshold, while the CPU
fan saturated.

Candidate 14:

- maintains modest nonzero airflow at idle;
- recruits the shared/GPU-side fan earlier;
- preserves a steep CPU escalation;
- increases fan 2 strongly only above 55°C;
- reserves still higher values for the upper bins.

## Speed values are not ordinary percentages

The interface permits values through 150. Empirical RPM mapping on the tested
unit was approximately:

| EC value | Observed RPM class |
|---:|---:|
| 20 | 1,400–1,500 RPM |
| 30 | 1,800–2,000 RPM |
| 45 | 2,750–2,850 RPM |
| 60 | 3,600–3,700 RPM |
| 90+ | fan/model-dependent; fan 1 approached physical maximum |

These values vary by side, temperature bin, EC slew, and prior fan state. They
are not guaranteed linear PWM percentages.

## No arbitrary profile input

The public manager intentionally accepts no curve parameter. Candidate 14 and
the factory curve are compiled into the reviewed root-owned tools. Supporting a
new firmware/model should be a reviewed code change with new measurements—not a
runtime address/value override.
