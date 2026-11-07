# Run, test, and build

Use Windows x64 and **Python 3.14 x64 with Tcl/Tk** to match the existing build
environment. All commands below run in PowerShell from the project root.
The source retains the `bridge` package layout; tests need no research workspace.

## Run from source

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r bridge/requirements.txt
.\.venv\Scripts\python.exe -m bridge
```

Using the venv interpreter directly avoids changing PowerShell's execution policy.
The GUI defaults to disconnected. The CMD launchers prefer the root release EXE,
then `dist/LP2Bridge.exe`, then the local venv, then Python on PATH.

```powershell
# Offline compiler, protocol and dependency check; no hardware is opened.
.\.venv\Scripts\python.exe -m bridge --self-test --report build/self-test.json

# Advanced headless operation; this explicitly connects to hardware.
.\.venv\Scripts\python.exe -m bridge --headless --transport usb --port COM3
```

Headless arguments are listed with `python -m bridge --headless --help`.
GUI state is stored in `LP2BridgeData/`; headless captures default to
`bridge/evidence/`, overridable with `--data-dir`. Both are ignored by Git.

## Offline tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s bridge/tests -t . -v
```

The suite uses fake serial/BLE devices, temporary directories, and loopback sockets.
It never opens a physical laser. Fixtures in `bridge/tests/fixtures/` retain only
small protocol replies, header goldens, and generated geometric jobs.

## Build the Windows EXE

```powershell
.\.venv\Scripts\python.exe -m pip install -r bridge/requirements-build.txt
.\.venv\Scripts\python.exe -m bridge.build_exe
```

The pinned requirements reproduce the dependency versions used by the development
build. PyInstaller creates a single windowed EXE with Python, Tk, pySerial, and
Bleak bundled. Output is `dist/LP2Bridge.exe` plus its SHA-256 file. Intermediate
files live in `build/`. A generated `bridge/build_manifest.json` records source
hashes and dependency versions and is embedded in the EXE. None of these generated
files should be committed.

```powershell
$test = Start-Process .\dist\LP2Bridge.exe -ArgumentList '--self-test --report build/exe-self-test.json' -Wait -PassThru
if ($test.ExitCode -ne 0) { throw 'EXE self-test failed' }
Get-Content .\build\exe-self-test.json

# Open and auto-close an isolated GUI without connecting or engraving.
Start-Process .\dist\LP2Bridge.exe -ArgumentList '--gui-smoke 2' -Wait

.\.venv\Scripts\python.exe -m bridge.package_release
```

Packaging checks source hashes and the EXE checksum, then writes
`dist/LP2-LightBurn-Bridge-Windows.zip` and its SHA-256 file. The ZIP contains the
app, launchers, build manifest, plain-text quick start, and guides. It contains no
saved settings, personal profiles, or job logs. Rebuild after changing runtime source.

## Code map

| Files | Responsibility |
| --- | --- |
| `__main__.py`, `entry.py`, `exe_entry.py` | Source/frozen launch and diagnostic dispatch |
| `gui.py`, `service.py` | Tk interface and the shared device-owning service |
| `server.py`, `runtime.py` | Local GRBL TCP endpoint, preflight, job ownership, completion and cancellation |
| `job.py`, `sequence.py` | G-code validation, vector compilation and ordered stage planning |
| `raster.py`, `raster_timing.py` | Binary scans, native bitmap conversion and exposure planning |
| `orientation.py`, `calibration.py` | Shared coordinate mapping and measured profiles |
| `speed.py`, `quality.py` | Native timer selection and diagnostic patterns |
| `transport.py`, `ble.py`, `codec/protocol.py` | USB/BLE IO and bounded native protocol |
| `build_exe.py`, `package_release.py` | Reproducible build inputs and release packaging |

Keep hardware experiments outside normal tests. Passing fake-device tests does
not establish optical power, physical dimensions, interlock operation, or behavior
on an untested device/firmware. Record physical checks separately and describe
their exact scope in release notes.
