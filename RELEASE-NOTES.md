# LP2 LightBurn Bridge scan-8 — Windows

A portable Windows x64 app for using LightBurn with a stock LaserPecker LP2 over
USB or Bluetooth. This release packages the curated application source and guides.

## Download

Download **LP2-LightBurn-Bridge-Windows.zip** under Assets, extract it, then run
**LP2Bridge.exe**. Python is bundled. Read **START-HERE.txt** for connection and
LightBurn settings; the full README and calibration/troubleshooting guides are included.

## Included

- Native GUI for USB/Bluetooth connection, status, logs, and calibration.
- Mixed vector cut/fill/binary-image jobs, whole-job preflight, and staged execution.
- Continuous horizontal/vertical images, optional native bitmap transfers, and
  whole-image payload handling that avoids arbitrary upload/restart seams.
- Shared left/right correction for vectors, images, fills, and positioning;
  enabled by default with 180° device rotation. Keep artwork unmirrored in LightBurn.
- Six-span caliper workflow for local scale/edge-bow correction.
- Small diagnostic patch default at 1% / 100 mm/s; patterns never start automatically.

## Scope and known limits

Experimental, independently developed bridge. Development hardware is LP2 firmware
V3.7.6 / hardware field 01321e86; other models/firmware remain unqualified. No
firmware or factory-table writes are included. M4 vector output is constant power,
true variable-power grayscale and G2/G3 are unsupported, and pause/resume remains
physically unqualified. Position/speed are host estimates. The whole-job scan-8
mirror correction is verified offline; corrected physical orientation still needs
confirmation. See docs/COMPATIBILITY.md for bounds and recovery behavior.

Bluetooth requires USB unplugged while the normal external power remains connected.
Logs and profiles live in LP2BridgeData beside the app and can contain private artwork.

## Verification

All **187 offline regression tests** passed on Windows with Python 3.14. The source
and frozen-app self-tests passed, and the packaged GUI opened and closed without
connecting to a device. No new physical engraving was performed for this packaging
release.
