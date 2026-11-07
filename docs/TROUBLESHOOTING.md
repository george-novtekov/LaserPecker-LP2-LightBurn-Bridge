# Troubleshooting

## USB port is missing or refused

Check the LP2's external power and USB data cable, then inspect **Device Manager →
Ports (COM & LPT)**. The current USB implementation accepts the observed CH340
device identifier `VID 1A86 / PID 7523` and uses 460800 baud internally. Click
**Refresh** and select its COM port; the port number can vary.

If Windows lacks a driver, obtain the appropriate CH340 driver from the device or
chip vendor. Close other software using the port. A different USB identifier is
outside the current implementation; do not bypass the check without verifying
the transport.

## Bluetooth connects but uploads fail

Unplug the USB cable while leaving the normal LP2 power supply connected. The
observed firmware chooses USB for bulk upload when USB remains attached. Close
phone/vendor connections, use **Scan**, and select the correct BLE address. If
there are several LP2 candidates, choose explicitly rather than relying on discovery.

## LightBurn does not connect

Connect the bridge first. Select the generic GRBL TCP device in LightBurn with
address `127.0.0.1`, port `23230`, and a 100 × 100 mm field. The address is local
to the same PC. One bridge listener and one LightBurn client own the endpoint.
If the port is already in use, close the duplicate bridge instance. A changed
bridge TCP port must also be changed in LightBurn.

## Mirrored, rotated, or incorrectly sized output

Start with bridge rotation **180°** and **Correct left/right mirror (whole job)**
enabled. Keep artwork unmirrored in LightBurn, use front-left origin and Absolute
Coordinates, and verify a small asymmetric design. Disconnect before changing
locked connection options. The diagnostic axis-swap option affects only the
diagnostic pattern; it does not transform incoming LightBurn jobs.

For scale or edge bow, follow [calibration](CALIBRATION.md). Keep focus/head height
consistent and avoid combining PC correction with another artwork scale correction.

## Image row-grid error, unsupported G-code, or Error:20

Check that you are running **scan-8** rather than an older executable. Older
versions rejected image scans that did not use a 0.1 mm row grid. The current
continuous mode accepts arbitrary source spacing with axis-aligned X or Y scans;
native bitmap mode resamples horizontal binary scans.

Read the bridge's rejection message before the generic GRBL error. Use
binary/dithered output with a single nonzero power/feed per image section. G2/G3,
true variable-power grayscale, and unsupported accessory commands fail preflight.
See [compatibility](COMPATIBILITY.md).

## Images take too long or exceed a bound

Continuous image transfers can be large, especially over Bluetooth. Try a smaller
crop or **Native bitmap** for faster transfer; check the changed dither/texture.
The 1 MiB stock payload bound still applies. The **Job budget s** field controls
each stage's execution timeout (initially 600; at most 7200), not the payload limit.
Only increase it when a stage is expected to take longer.

## Alarm or interrupted transfer

Read the reason in **Logs**. A complete preflight error permits a corrected retry.
After a link/device error, disconnect and resolve the cause before reconnecting.
If a raw upload was interrupted or the log reports an uncertain transfer, restart
the LP2 before retrying. The bridge does not resume or replay a job automatically.

## App does not open or a download is blocked

Extract the ZIP fully to a writable local folder and run the extracted EXE. Check
`LP2BridgeData/application.log` if present. If Windows or antivirus blocks the app,
inspect the message and verify the download source/checksum; report a suspected
false positive instead of disabling protection. No code-signing certificate is
included in this project.

## Collect diagnostics

Run this from PowerShell in the extracted app folder. The self-test is offline;
it does not connect to or engrave with the LP2.

```powershell
$test = Start-Process .\LP2Bridge.exe -ArgumentList '--self-test --report self-test.json' -Wait -PassThru
$test.ExitCode
Get-Content .\self-test.json
```

For discovery only (no engraving):

```powershell
Start-Process .\LP2Bridge.exe -ArgumentList '--scan-bluetooth --report bluetooth-scan.json' -Wait
Get-Content .\bluetooth-scan.json
```

When reporting an issue, include version, Windows version, device firmware,
transport, exact error text, and minimal reproducible settings/design. Captures
in `LP2BridgeData` can contain artwork and Bluetooth addresses. Redact personal
data and share a minimal excerpt rather than the entire folder.
