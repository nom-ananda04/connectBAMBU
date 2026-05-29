import os
import json
import time
import subprocess
import ssl
import threading
import connect_python
import paho.mqtt.client as mqtt
from datetime import datetime

# --- Slicing config (paths set up during the CLI debugging session) ---
ORCA_BIN   = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
FLAT_DIR   = "/Users/ananda/connectBAMBU/flat_profiles"
SLICE_DIR  = "/Users/ananda/connectBAMBU/sliced"
MACHINE_PROFILE  = os.path.join(FLAT_DIR, "machine_flat.json")
PROCESS_PROFILE  = os.path.join(FLAT_DIR, "process_flat.json")
FILAMENT_PROFILE = os.path.join(FLAT_DIR, "filament_flat.json")
BED_CENTER = (128.0, 128.0)  # X1C bed center in mm


def center_stl(in_path, out_path):
    """Move an STL so its XY bounding-box center sits at the bed center."""
    from stl import mesh
    m = mesh.Mesh.from_file(in_path)
    cx = (m.x.min() + m.x.max()) / 2.0
    cy = (m.y.min() + m.y.max()) / 2.0
    m.x += (BED_CENTER[0] - cx)
    m.y += (BED_CENTER[1] - cy)
    m.save(out_path)


def slice_stl(stl_path):
    """Center + slice an STL into a printable .gcode.3mf. Returns output path or None."""
    os.makedirs(SLICE_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(stl_path))[0].replace(" ", "_")
    centered = os.path.join(SLICE_DIR, base + "_centered.stl")
    out_3mf  = os.path.join(SLICE_DIR, base + ".gcode.3mf")

    print(f"Centering {os.path.basename(stl_path)}...", flush=True)
    center_stl(stl_path, centered)

    print("Slicing (this can take a moment)...", flush=True)
    # Run from a neutral cwd with an absolute export path so the path doesn't double up
    result = subprocess.run([
        ORCA_BIN,
        "--arrange", "0",
        "--load-settings", MACHINE_PROFILE,
        "--load-settings", PROCESS_PROFILE,
        "--load-filaments", FILAMENT_PROFILE,
        "--slice", "0",
        "--export-3mf", out_3mf,
        centered,
    ], cwd=os.path.expanduser("~"))

    if result.returncode == 0 and os.path.exists(out_3mf):
        print(f"Sliced -> {out_3mf}", flush=True)
        return out_3mf
    print(f"Slicing failed (return code {result.returncode}).", flush=True)
    return None

@connect_python.main
def main(nominal_client: connect_python.Client):

    PRINTER_IP  = nominal_client.get_value("printer_ip")
    ACCESS_CODE = nominal_client.get_value("access_code")
    SERIAL      = nominal_client.get_value("serial_number")

    if not all([PRINTER_IP, ACCESS_CODE, SERIAL]):
        print("Please fill in Printer IP, Access Code, and Serial Number in Settings.")
        return

    print("Ready!", flush=True)

    latest = {"nozzle_temp": None, "bed_temp": None, "remaining_time": None}
    latest_lock = threading.Lock()

    def make_mqtt_client():
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set("bblp", ACCESS_CODE)
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
        tls_ctx.load_verify_locations("/Users/ananda/connectBAMBU/printer.cer")
        client.tls_set_context(tls_ctx)
        return client

    # --- Telemetry ---
    def telemetry_loop():
        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                print_data = payload.get("print", {})
                with latest_lock:
                    if "nozzle_temper" in print_data:
                        latest["nozzle_temp"] = float(print_data["nozzle_temper"])
                    if "bed_temper" in print_data:
                        latest["bed_temp"] = float(print_data["bed_temper"])
                    if "mc_remaining_time" in print_data:
                        latest["remaining_time"] = float(print_data["mc_remaining_time"])
            except Exception:
                pass

        def on_connect(client, userdata, flags, rc, properties=None):
            print(f"Telemetry connected, rc={rc}", flush=True)
            client.subscribe(f"device/{SERIAL}/report")
            client.publish(f"device/{SERIAL}/request", json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

        while True:
            try:
                client = make_mqtt_client()
                client.on_message = on_message
                client.on_connect = on_connect
                client.connect(PRINTER_IP, 8883, 60)
                client.loop_forever()
            except Exception as e:
                print(f"Telemetry reconnecting... ({e})", flush=True)
                time.sleep(5)

    threading.Thread(target=telemetry_loop, daemon=True).start()

    # --- Camera via RTSPS (port 322) using ffmpeg, raw RGB frames ---
    CAM_WIDTH = 640
    CAM_HEIGHT = 360
    # RGB24 = 3 bytes per pixel
    FRAME_SIZE = CAM_WIDTH * CAM_HEIGHT * 3  

    def camera_loop():
        rtsp_url = f"rtsps://bblp:{ACCESS_CODE}@{PRINTER_IP}:322/streaming/live/1"
        while True:
            try:
                # ffmpeg outputs raw RGB24 frames scaled to fixed size
                proc = subprocess.Popen([
                    "/opt/homebrew/bin/ffmpeg",
                    "-rtsp_transport", "tcp",
                    "-i", rtsp_url,
                    "-f", "rawvideo",
                    "-pix_fmt", "rgb24",
                    "-vf", f"scale={CAM_WIDTH}:{CAM_HEIGHT}",
                    "-r", "1",
                    "-"
                ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

                while True:
                    # Read exactly one full frame worth of bytes
                    raw = proc.stdout.read(FRAME_SIZE)
                    if len(raw) < FRAME_SIZE:
                        break
                    nominal_client.stream_rgb("frame_buffer", time.time(), CAM_WIDTH, raw)
            except Exception as e:
                print(f"Camera error: {e}, retrying...", flush=True)
                time.sleep(5)

    threading.Thread(target=camera_loop, daemon=True).start()

    # --- Print job ---
    def start_print(filename):
        print(f"Connecting to MQTT...", flush=True)
        client = make_mqtt_client()
        client.connect(PRINTER_IP, 8883, 60)
        client.loop_start()
        # project_file command works with .3mf files and supports AMS mapping
        command = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "url": f"ftp:///cache/{filename}",
                "subtask_name": filename,
                "file": "",
                "md5": "",
                "task_id": "0",
                "project_id": "0",
                "profile_id": "0",
                "subtask_id": "0",
                "timelapse": False,
                "bed_type": "auto",
                "bed_levelling": True,
                "flow_cali": False,
                "vibration_cali": False,
                "layer_inspect": False,
                "use_ams": True,
                "ams_mapping": [0]
            }
        }
        topic = f"device/{SERIAL}/request"
        result = client.publish(topic, json.dumps(command))
        print(f"Published to {topic}: {result.rc}", flush=True)
        time.sleep(2)
        client.loop_stop()
        client.disconnect()
        print(f"Print command sent for {filename}!", flush=True)

    def send_print_job(local_path):
        raw_name = os.path.basename(local_path).replace(" ", "_")
        print(f"Uploading {raw_name} to cache/...", flush=True)
        result = subprocess.run([
            "curl", "--ftp-pasv", "--insecure",
            "-T", local_path,
            f"ftps://{PRINTER_IP}:990/cache/{raw_name}",
            "--user", f"bblp:{ACCESS_CODE}"
        ])
        print(f"Return code: {result.returncode}", flush=True)
        if result.returncode == 0:
            print(f"Uploaded {raw_name}!", flush=True)
            start_print(raw_name)
        else:
            print(f"Upload failed: {result.returncode}", flush=True)

    # --- Main loop: stream telemetry + watch for print triggers ---
    last_send_state = False
    while True:
        now = datetime.now()
        with latest_lock:
            n, b, r = latest["nozzle_temp"], latest["bed_temp"], latest["remaining_time"]
        try:
            if n is not None:
                nominal_client.stream("nozzle_temp", now, n)
            if b is not None:
                nominal_client.stream("bed_temp", now, b)
            if r is not None:
                nominal_client.stream("remaining_time", now, r)
        except Exception as e:
            print(f"Stream error: {e}", flush=True)

        send = nominal_client.get_value("send_print_job")
        if send and not last_send_state:
            file_path = (nominal_client.get_value("gcode_file_path") or "").strip().strip("'\"")
            print(f"Send triggered. Path: '{file_path}'", flush=True)
            if not file_path:
                print("No file path specified.", flush=True)
            elif not os.path.exists(file_path):
                print(f"File not found: '{file_path}'", flush=True)
            else:
                try:
                    # If given an STL, slice it to a .gcode.3mf first.
                    # If already a .3mf/.gcode.3mf, upload it as-is.
                    if file_path.lower().endswith(".stl"):
                        sliced = slice_stl(file_path)
                        if sliced:
                            send_print_job(sliced)
                    else:
                        send_print_job(file_path)
                except Exception as e:
                    import traceback
                    print(f"Failed: {e}", flush=True)
                    traceback.print_exc()
        last_send_state = bool(send)
        time.sleep(0.5)

if __name__ == "__main__":
    main()