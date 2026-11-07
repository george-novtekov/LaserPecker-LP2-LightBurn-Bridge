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

Create a dedicated LightBurn device profile for the bridge. LightBurn connects to
the bridge on the **same Windows PC** over TCP; the bridge then handles the LP2's
USB or Bluetooth connection. Use the same profile for either transport.

### A. Create the device profile

1. Start **LP2Bridge.exe**, connect to the LP2, and wait for **Connected - Idle**.
2. Open LightBurn's **Laser** window. If it is hidden, enable it from the **Window**
   menu. Click **Devices → Create Manually**.
3. Choose **GRBL** as the device type and click **Next**. This bridge presents a
   generic GRBL interface, so use that device type even though the LP2 uses galvos.
4. Choose **Ethernet or Wifi/TCP** as the connection type and click **Next**.
   This applies even when the bridge connects to the LP2 by USB or Bluetooth.
5. Enter **`127.0.0.1`** as the IP address/hostname, then click **Next**.
6. Name the profile **`LP2 USB BRIDGE`**. Set **Width = 100 mm** and
   **Height = 100 mm**.
7. Select the **front-left / lower-left origin**. Turn **Auto-home on startup**
   **off**, then continue to the summary.
8. Click **Finish**, then **OK** in the Devices window to save the profile.
   Select **LP2 USB BRIDGE** in the Laser window's device dropdown.

Wizard labels can vary with the LightBurn version. See LightBurn's official
[manual device creation guide](https://docs.lightburnsoftware.com/2.1/GetStarted/CreateManually/)
for the wizard screens.

### B. Set the TCP port and power scale

With **LP2 USB BRIDGE** selected, open **Edit → Device Settings → Basic Settings**:

1. Set **Network Port = `23230`**. LightBurn's default network port is 23; change
   it to the bridge's port. Enter the address and port in their separate fields.
2. Set **S-value Max = `1000`** so LightBurn's 100% output maps to S1000.
3. Keep **Auto-Home on Startup** disabled and confirm the work area and origin.
4. Use **Buffered** transfer mode. Leave any target-buffer override disabled so
   LightBurn can use the buffer size reported by the bridge.
5. Keep **Query Regularly for Laser Position** enabled for status polling.
6. Click **OK** to save. If the connection does not refresh, right-click the
   **Devices** button in the Laser window to reconnect.

LightBurn documents the network-port and power-scale controls under
[Device Settings: Basic Settings](https://docs.lightburnsoftware.com/2.2/Reference/DeviceSettings/BasicSettings/).

### C. Set job coordinates and orientation

In the **Laser** window, set **Start From → Absolute Coords** (also labeled
**Absolute Coordinates** in some versions). Place the complete artwork inside the
100 × 100 mm workspace and check its bounds in Preview.

In the bridge's **Connection** tab, keep **Rotation = 180** and **Correct left/right
mirror (whole job) = enabled** for the current device mapping. Keep artwork
unmirrored in LightBurn. Check a small asymmetric design before a full job; the
scan-8 correction has offline coverage, while its physical orientation still needs
confirmation. Disconnect the bridge before changing locked connection settings.

### Profile settings reference

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
| Transfer mode | **Buffered** |
| Target-buffer override | **Off** |
| Query regularly for laser position | **On** |

### D. Verify the connection and save a backup

1. Confirm the bridge shows **Connected - Idle** and reports that LightBurn is
   connected. LightBurn should show **Ready** or **Idle**, depending on the panel.
2. Open LightBurn's **Console** tab (enable **Window → Console** if hidden).
   Enter **`$I`** and press Enter. The bridge should return a version line containing
   **`LP2-USB-BRIDGE:host-scan-8`**, followed by `ok`.
3. Enter **`?`** to request status. An idle bridge returns a line beginning
   **`<Idle|`**. These two checks query information and do not start engraving.
4. Open **Devices**, select the profile, and click **Export** to keep a backup.
   Use **Import** to restore a saved `.lbdev` or `.lbzip` device profile, depending
   on your LightBurn version. See the official
   [Devices guide](https://docs.lightburnsoftware.com/latest/Reference/Devices/).

If LightBurn stays disconnected, check that the bridge is running, the selected
device uses TCP, and **Network Port** is **23230**. If you changed the bridge's
TCP port, enter that same port in LightBurn. The LP2 COM port is selected inside
the bridge; LightBurn uses the local TCP endpoint. See
[connection troubleshooting](docs/TROUBLESHOOTING.md#lightburn-does-not-connect).

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
