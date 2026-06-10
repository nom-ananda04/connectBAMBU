# Bambu X1-Carbon → Nominal Connect Integration

A [Nominal Connect](https://nominal.io) app that controls and monitors a **Bambu Lab X1-Carbon** 3D printer over the local network. It streams live telemetry and camera into Nominal, and slices + uploads + starts print jobs straight from the Connect UI — no cloud account required.

## What it does

- **Live telemetry** — nozzle temp, bed temp, and remaining print time streamed to Nominal via MQTT.
- **Live camera feed** — the printer's chamber camera (RTSPS) rendered in a Connect video panel.
- **Headless print pipeline** — point it at an `.stl`, hit a button, and it centers → slices → uploads → starts the print, feeding filament from the AMS automatically.
- **Pre-sliced files too** — if you give it a `.gcode.3mf` instead of an `.stl`, it skips slicing and uploads directly.

## Requirements

- macOS (Apple Silicon paths assumed; see *Configuration* to adapt).
- Python 3.13 with a virtualenv.
- [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) installed at `/Applications/OrcaSlicer.app` (used for headless slicing).
- `ffmpeg` (`brew install ffmpeg`) for the camera feed.
- A Bambu X1-Carbon on the same LAN, with:
  - **LAN Mode** enabled
  - **Developer Mode** enabled (disables MQTT command auth so prints can be triggered locally)
  - **LAN Mode Liveview** enabled (for the camera)
  - An **SD card inserted** (the FTP `cache/` directory lives on it)
- The printer's X.509 cert saved locally as `printer.cer` (extracted from Bambu Connect / Bambu Studio).

### Python packages

```bash
pip install paho-mqtt numpy numpy-stl connect-python
```

(`connect-python` is Nominal's edge SDK; the import name is `connect_python`. The app only runs when launched from inside the Nominal Connect app, not as a standalone `python3` script.)

## Files

| File | Purpose |
|------|---------|
| `bambuPrint.py` | The Connect app script: telemetry loop, camera loop, slice/upload/print logic. |
| `bambu_printer.connect.yaml` | The Connect app layout: gauges, camera panel, settings inputs, and the script runner. |
| `flat_profiles/` | Flattened OrcaSlicer profiles (machine, process, filament) used for headless slicing — see *Slicing*. |
| `printer.cer` | Printer TLS cert for MQTT (kept out of git). |

## Setup

1. Fill in the **Settings** tab in the Connect app:
   - **Printer IP**, **Access Code**, **Serial Number**
   - **Stream to Nominal** + **Dataset RID** if you want telemetry persisted
   - only things you need to change in the code include the:
     - ORCA_BIN   = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
     - FLAT_DIR   = "/Users/ananda/connectBAMBU/flat_profiles"
     - SLICE_DIR  = "/Users/ananda/connectBAMBU/sliced"
     - all you should need to do is change ananda -> usrmac (specific to your computer) other than that you need to install orcaslicer and then paho.mqtt.client to talk with the printer
to send a print first put the IP, Access code, and Serial number in the boxes:
IP = 10.113.0.210
Access = 7e11b21e
Serial = 00M09D541301301
2. Generate the flattened slicing profiles (see *Slicing* below).
3. Start the **Bambu Printer Stream** script from the bottom panel. You should see `Ready!` and `Telemetry connected` in the logs, and the gauges should begin updating.


## Usage

1. In **Settings**, set **STL File Path** to any `.stl` on disk.
2. Tick **Slice & Send Print Job**.
3. The logs will show: centering → slicing → uploading → print command sent. The printer feeds filament from AMS slot A1 and begins printing.

## How it works (protocol notes)

These are the specifics that make the X1-Carbon work over LAN — most are undocumented and were found by trial and error.

### Telemetry (MQTT)
- TLS on port **8883**, username `bblp`, password = access code, cert = `printer.cer`.
- Subscribe to `device/{SERIAL}/report`. On connect, publish a `pushall` command to force a full status report (the printer otherwise only sends incremental updates).
- Parse `print.nozzle_temper`, `print.bed_temper`, `print.mc_remaining_time`.
- **The MQTT callback runs on a background thread, but `nominal_client.stream()` must be called from the main thread.** The callback writes to a lock-protected dict; the main loop reads it and streams. Streaming off-thread throws `KeyError: 'values'`.
- `nominal_client.stream()` takes **positional** args: `stream(stream_id, timestamp, value)`. Passing `value=` as a keyword hits the multi-channel code path and fails.

### Camera (RTSPS)
- X1-Carbon uses **RTSPS on port 322**: `rtsps://bblp:{ACCESS_CODE}@{IP}:322/streaming/live/1` (port 6000 is for P1/A1 series, not X1C).
- `ffmpeg` pulls the stream and outputs raw RGB24 frames (`-pix_fmt rgb24 -f rawvideo`, scaled to 640×360). Each frame is pushed with `nominal_client.stream_rgb("frame_buffer", ts, width, data)`.
- `ffmpeg` is called by absolute path (`/opt/homebrew/bin/ffmpeg`) because Connect doesn't inherit the shell PATH.

### File upload (FTP)
- Implicit FTPS on port **990** (port 21 is refused). Python's `ftplib` hangs on the implicit-TLS handshake, so uploads go through `curl`:
  `curl --ftp-pasv --insecure -T <file> ftps://{IP}:990/cache/<name> --user bblp:{CODE}`
- Files go in `cache/` (created once with `MKD cache`). The directory lives on the SD card.
- Don't use `subprocess` `capture_output=True` — it hangs. Let curl stream to the logs.

### Print trigger (MQTT)
- Publish to `device/{SERIAL}/request` with the `project_file` command (not `gcode_file`, and not raw M23/M24 — those are rejected):
  - `url: ftp:///cache/{filename}` (triple slash)
  - `param: Metadata/plate_1.gcode`
  - `use_ams: true`, `ams_mapping: [0]` (maps file color 0 → AMS slot A1)
- This is rejected with *"MQTT command verification failed"* unless **Developer Mode** is on.

### AMS auto-feed
Two conditions must both hold for the printer to pull from the AMS instead of asking for a manual feed:
1. The gcode must be a properly sliced `.gcode.3mf` (a raw `.gcode` has no AMS metadata).
2. The print command must include `use_ams: true` and a valid `ams_mapping`.

## Slicing

Headless slicing uses the OrcaSlicer CLI. **The stock Bambu/Orca profiles crash the CLI on macOS** at `update_values_to_printer_extruders_for_multiple_filaments` — the modern X1C profile defines two nozzle *variants* (Standard + High Flow) but one physical extruder, and the CLI reads past the end of the per-extruder arrays.

The workaround is to **flatten the profiles to a single variant**: resolve the `inherits` chain, collapse every 2-element variant array to its first element, and set the extruder-variant fields to a single entry. The flattening script lives in the project (regenerate it any time you change nozzle or filament).

Other CLI gotchas baked into the working invocation:
- Use `--slice 0` (all plates), never `--slice 1`.
- Pass each profile as a **separate** `--load-settings` flag, not one semicolon-joined string.
- Disable arrange (`--arrange 0`) and pre-center the STL on the bed (128, 128) with numpy-stl — the interactive arrange/ensure-on-bed steps need GUI context and segfault headless.
- Use an **absolute** `--export-3mf` path and run from a neutral working directory, or the path doubles up and the write fails.
- Set `curr_bed_type` to a plate the filament supports (e.g. `Textured PEI Plate` for PETG — Cool Plate is rejected).

### Current profile

The flattened profiles target: **0.4mm nozzle**, **0.20mm Standard** layer height, **Generic PETG HF**, **Textured PEI Plate**. Changing nozzle or filament means regenerating the three flat profiles from the matching source files.

## Known limitations

- **Absolute paths.** `bambuPrint.py` hardcodes `/Users/ananda/connectBAMBU/...` paths to the slicer, profiles, and cert. The app runs only on this machine for this user as written. Parameterize those constants to make it portable.
- **Slicing is macOS-CLI-fragile.** The flattening workaround is specific to the current OrcaSlicer build's profile schema. A slicer update could change the schema and require re-flattening. Slicing on Linux (Docker/VM) avoids the macOS segfault entirely and is the more robust long-term path.
- **No cloud.** This is intentionally LAN-only. It does not use the Bambu cloud API.
