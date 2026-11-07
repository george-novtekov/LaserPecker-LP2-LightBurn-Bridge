# LaserPecker LP2 LightBurn Bridge

Use LightBurn with a stock LaserPecker LP2 through a portable Windows application.
The bridge accepts LightBurn's GRBL G-code on your PC and converts it into native
LP2 jobs sent over USB or Bluetooth. No firmware replacement is required.

**[Download the Windows app](https://github.com/george-novtekov/LaserPecker-LP2-LightBurn-Bridge/releases/latest)** ·
[Calibration](docs/CALIBRATION.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) ·
[Build from source](docs/DEVELOPMENT.md)

This is an independent, experimental project. Development hardware runs LP2
firmware V3.7.6 with hardware field `01321e86`; other firmware and LaserPecker models
have not been qualified. Current application version: **scan-8**.

## Disclaimer and limitation of liability

**USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.** It is provided **"AS IS"** and
**"AS AVAILABLE"**, without warranties of any kind, express or implied, including
merchantability, fitness for a particular purpose, and non-infringement.

To the maximum extent permitted by applicable law, the author and contributors
shall not be liable for any claim, injury, loss, expense, or damage of any kind
arising from or related to using, misusing, or being unable to use this software.
This includes direct, indirect, incidental, special, and consequential damages;
personal injury; fire; damage to the laser, computer, materials, or other property;
and loss of data, income, or profits, regardless of the basis of the claim and even
if advised of the possibility of such damage. You are responsible for operating
your equipment safely and assessing whether the software is suitable for your use.

## What it does

- A Windows GUI for connection, job status, logs, calibration, and print diagnostics.
- USB and Bluetooth connections using the same LightBurn device profile.
- Vector cuts, fills, and binary/dithered images in mixed jobs.
- Continuous image scanning and an optional native bitmap transfer mode.
- PC-side scale and symmetric edge-bow correction from six caliper measurements.
- Whole-job orientation correction shared by vectors, fills, images, and positioning.
- Whole-job validation before upload, with completion reported after the LP2 finishes.

The LP2 executes uploaded jobs in stages. Live position and speed reports are host
estimates, and GRBL dynamic M4 power modulation is not emulated. See
[supported features and limits](docs/COMPATIBILITY.md) before planning a job.

## 1. Download and run

You need a Windows x64 PC, LightBurn with GRBL/TCP device support, and a LaserPecker
LP2 with its normal power supply. USB requires the LP2's CH340 serial port; Bluetooth
requires a working Bluetooth adapter on the PC.

1. Open the **[latest release](https://github.com/george-novtekov/LaserPecker-LP2-LightBurn-Bridge/releases/latest)**.
2. Under **Assets**, download **`LP2-LightBurn-Bridge-Windows.zip`**.
   GitHub's automatically generated **Source code** archives are for developers.
3. Right-click the ZIP and choose **Extract All**. Keep it in a folder you can write
   to, such as Documents. Run the extracted files, rather than opening them in the ZIP.
4. Double-click **`LP2Bridge.exe`**. No Python installation is needed.

The application saves settings, calibration profiles, and logs in `LP2BridgeData`
beside the EXE. Opening it does not start an engraving.

## 2. Connect the LP2

Close other applications or phone connections using the LP2. Only one bridge
instance should own the device.

### USB

1. Power the LP2 normally and connect its USB cable to the PC.
2. In the bridge's **Connection** tab, select **USB**.
3. Click **Refresh** and select the LP2's CH340 COM port. `COM3` is an example,
   not a requirement. You can check the port in Windows Device Manager.
4. Click **Connect** and wait for **Connected - Idle**.

If the port is missing, see [USB troubleshooting](docs/TROUBLESHOOTING.md#usb-port-is-missing-or-refused).

### Bluetooth

1. Keep the LP2's external power supply connected and **unplug its USB cable**.
   With USB attached, the observed stock firmware selects USB for bulk uploads
   even when Bluetooth status queries succeed.
2. Select **Bluetooth**, click **Scan**, and select the LP2's BLE address.
3. Click **Connect** and wait for **Connected - Idle**.

Both transports use `127.0.0.1:23230` in LightBurn. Bluetooth transfers can take
longer, especially with continuous images.

## 3. Set up LightBurn

Create a device manually in LightBurn using these settings. Names and placement of
controls can vary with the LightBurn version.

| Setting | Value |
| --- | --- |
| Device type | Generic **GRBL** |
| Connection | **Ethernet/TCP** |
| Address | `127.0.0.1` |
| TCP port | `23230` |
| Device name | `LP2 USB BRIDGE` (used for both USB and Bluetooth) |
| Working area | `100 × 100 mm` |
| Origin | Front-left / lower-left |
| Start From | **Absolute Coordinates** |
| Auto-home on startup | **Off** |
| S-value max | **1000** |

Select this device in LightBurn while the bridge is connected. The bridge should
report a LightBurn connection, and LightBurn should show the GRBL device as idle.

Keep the bridge's default **Rotation = 180** and **Correct left/right mirror
(whole job) = enabled** for the current device mapping. Keep artwork unmirrored in
LightBurn. Check a small asymmetric design before a full job; the scan-8 correction
has offline coverage, while its physical orientation still needs confirmation.

## 4. Make a first test

Use the LP2's normal enclosure/protection and material-handling precautions. Keep
the job supervised; software Stop and estimated status are not physical interlocks.

1. Set focus and place suitable scrap material in the working field.
2. In the bridge, leave **Image scan = Continuous (quality)** selected.
3. Use **Print quality → Engrave patch 1** at its initial **1% / 100 mm/s** settings
   to make a small diagnostic mark. These are diagnostic defaults, not a material
   recipe. Inspect the result before increasing exposure or trying all eight patches.
4. In LightBurn, make a small asymmetric design near the center of the bed and
   choose power/speed appropriate to the material. Check Preview and bounds.
5. Press **Start** in LightBurn. Wait for the bridge and device to finish all stages.

The bridge accepts the full job before uploading it. LightBurn may finish sending
most lines before the physical engraving is complete; the final completion response
waits for device activity followed by matching idle replies. Use the bridge's status
and logs to follow execution.

**Stop job** cancels the current job and clears later stages. A cancelled or failed
job is not automatically replayed. Disconnecting or closing the bridge ends its
service; an interrupted bulk upload may require restarting the LP2.

## Images and calibration

Use **Continuous (quality)** to preserve binary scan runs at the source spacing.
Horizontal and vertical image scans are supported. Select **Native bitmap** for
smaller transfers; it resamples onto the LP2's integer command-pixel grid and
supports horizontal scans only. True variable-power grayscale is unsupported;
use binary/dithered output with one nonzero power and feed per image section.

For dimensional correction, follow the **[calibration guide](docs/CALIBRATION.md)**:
engrave a baseline, enter six measured spans, calculate/save/apply a profile, and
engrave a separate check. Profiles are local to the PC; no factory calibration
table is written. Keep focus and head height consistent.

## Help and source

- [Troubleshooting](docs/TROUBLESHOOTING.md): ports, Bluetooth, alarms, images, logs.
- [Compatibility](docs/COMPATIBILITY.md): supported commands and execution limits.
- [Development](docs/DEVELOPMENT.md): Python setup, tests, EXE build, packaging.
- [Maintainer guide](docs/PUBLISHING.md): upload clean source and Windows releases.
- [Report a problem](https://github.com/george-novtekov/LaserPecker-LP2-LightBurn-Bridge/issues):
  include app version, Windows version, firmware, transport, settings, and the exact
  error. Logs can contain artwork; share only the relevant redacted excerpt.

The repository contains the application, offline tests, compact fixtures, and guides.
Generated executables belong in release assets. Research archives, vendor binaries,
personal captures, saved device settings, and old prototypes are excluded.
