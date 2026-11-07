# Offline regression fixtures

These compact fixtures keep the test suite independent of the research workspace.
They are inputs to fake devices and parsers, not user engraving examples.

| File | Origin and purpose |
| --- | --- |
| `metadata-responses.json` | Raw status/settings/file-list reply hex from the original read-only metadata query; log paths and capture metadata removed |
| `identity-responses.json` | Raw status/version reply hex from the original read-only identity query; capture metadata removed |
| `oracle-raster-header.json` | Expected 64-byte bitmap headers from an offline vendor-function oracle; retains source/function hashes, not vendor code |
| `rectangle-lightburn.gc` | Small generated rectangle used to verify LightBurn syntax and vector conversion |
| `mixed-lightburn.gc` | Small generated cut/fill/binary-scan job used to verify stage ordering |

Protocol reply bytes, including ambiguous file-list fields, are retained exactly
because parser regressions depend on them. No vendor executable, firmware image,
full personal-job capture, or saved calibration profile is included. Tests never
send these inputs to physical hardware.
