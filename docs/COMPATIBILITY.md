# Supported features and limits

This is a GRBL-facing translator for stock LP2 uploads, rather than a full GRBL
motion controller. Development captures identify firmware **V3.7.6** and hardware
field **01321e86**. Other devices and firmware versions remain unqualified.

## Job support

| Feature | Behavior |
| --- | --- |
| Vector cuts/fills | G0/G1 paths, including curves exported as line segments |
| Mixed jobs | Cut, fill, and binary image stages in emitted G-code order |
| M3/M4 vector paths | Native constant power; dynamic M4 modulation is not emulated |
| Continuous images | Axis-aligned horizontal or vertical binary scans; source endpoints retained |
| Native bitmap images | Horizontal scans; resampling to an integer native command grid |
| True grayscale | Variable nonzero pixel power is unsupported; use binary/dithered output |
| Arc commands G2/G3 | Rejected; export linearized paths |
| Rotary, homing, jogging | Unsupported |
| Accessory/settings writes | Unsupported; unknown operations reject at preflight |
| Pause/resume | Not physically qualified; prior observations showed progress during Hold |
| Stop | Clears later stages; no successful completion response for a cancelled job |

Each image section needs one positive power and feed. Continuous scans reject
powered diagonals or cross-axis turns within a scan section. Native bitmap pitch
is `px / 40 mm` (25 microns or coarser, up to 1016 command DPI). Command DPI is
not measured optical resolution. Native bitmap exposure is quantized to 20 µs
steps; its minimum nominal image feed is 3000 mm/min (50 mm/s).

## Execution model

One LightBurn Start can become multiple stock LP2 jobs when power, speed, or job
type changes. All stages validate before the first upload. A continuous image
section is kept together if it fits; oversized images reject rather than silently
splitting into chunks that introduce restart seams.

The final M2 response waits for matching content ID, observed activity, and two
idle/error-free replies for every stage. Positions and modeled speed remain host
estimates without motion-sensor feedback. Vector speed is selected from discrete
stock timer settings; for example, axis-aligned F6000 models about 96.020 mm/s.
Power percentages are command values, not measured optical output.

GUI/CLI defaults use 180° rotation plus X reflection after calibration. With no PC
calibration, logical `(X, Y)` maps to device `(X, 100 - Y)`. Bitmap row-phase
compensation follows this mapping. Artwork stays in logical LightBurn coordinates.
The whole-job mirror correction is covered offline; physical orientation after
the scan-8 change still needs confirmation.

## Bounds and recovery

| Limit | Bound |
| --- | --- |
| Input | 16 MiB, 1,100,000 lines, 256 bytes per line |
| Stock stages | 512 per submitted job |
| Stock payload | 1 MiB per stage |
| Working command field | 100 × 100 mm |
| Execution budget | 600 s per stage by default; GUI permits 1–7200 s |

Native bitmap alternate-row compensation needs one horizontal pixel of margin
for an otherwise full-width image. Geometry changes can make an edge placement
unreachable even when the original design fitted.

A complete preflight rejection reports a reason and permits a corrected retry.
Malformed/partial input, overflow, link loss, or device errors can latch Alarm.
There is no automatic reconnect or job replay. An interrupted raw upload closes
the uncertain connection and may require restarting the LP2. Uploads use ordinary
device job storage and may replace cached jobs. No firmware, bootloader, or
factory-table write path is included.
