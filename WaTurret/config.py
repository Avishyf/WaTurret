import os
import json
import socket
import collections
import threading
from datetime import datetime
import cv2
import numpy as np
from ultralytics import YOLO

def ts():
    """Generates a timestamp string for console logging colored in ANSI light blue with 1 subsecond digit."""
    # ANSI Light Blue: \033[96m, Reset: \033[0m
    return f"\033[96m{datetime.now().strftime('[%H:%M:%S.%f]')[:-6] + ']'}\033[0m"

# --- Global Configurations & Variables ---
ESP_IP = None       # Discovered IP address of the ESP32-CAM
STREAM_URL = None   # Target MJPEG video stream URL
CONTROL_URL = None  # Target REST API controller URL (Port 81)
udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # UDP Socket for fast motor/MOSFET commands

TARGET_FPS = 10                  # Target frame rate for AI tracking loop
FRAME_DELAY = 1.0 / TARGET_FPS   # Minimum delay between processed frames

is_flipped = False               # Tracks camera/servo inversion state
invert_pan = False               # Invert D-pad & tracking pan direction
invert_tilt = False              # Invert D-pad & tracking tilt direction
conf_threshold = 0.25            # YOLO object detection confidence threshold (defaults to 0.25)
target_class = "bird"            # Default detection target class ("bird" or "human")
camera_resolution = 6            # Default camera resolution (VGA)
camera_quality = "medium"        # Default camera quality (low, medium, high)

enable_zoom_2x = False           # 2x Digital Zoom toggle
enable_scanning_mode = True      # Enable/disable 3-minute idle scanning sweeps
enable_image_improvement = False # Image sharpening & color boost toggle

enable_video_capture = False     # Auto-Record Detections settings state
is_recording = False             # Active recording state
record_trigger_time = 0.0        # Time when recording was triggered
pre_trigger_buffer = collections.deque(maxlen=300) # Sliding window queue of (timestamp, jpeg_bytes)
recording_frames = []            # List of (timestamp, jpeg_bytes) for active recording
current_pip_frame_bytes = None   # Cached JPEG bytes of the frame before firing for PiP overlay
camera_connected = False         # Connection status of the camera stream

# Frame sharing variables
latest_frame = None
frame_lock = threading.Lock()

# Generate a solid black startup placeholder frame (640x480) with centered text
placeholder_frame = None
try:
    _placeholder_img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(_placeholder_img, "INITIALIZING CAMERA FEED...", (90, 240), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    _, _buffer = cv2.imencode('.jpg', _placeholder_img)
    placeholder_frame = _buffer.tobytes()
except Exception as _e:
    pass

# Model variables
model = None
model_lock = threading.Lock()
current_model_file = "yolo26s.pt" # Default model

def load_yolo_model(model_name):
    global model, current_model_file
    print(f"{ts()} [System] Loading YOLO model ({model_name})...", flush=True)
    try:
        new_model = YOLO(model_name)
        with model_lock:
            model = new_model
            current_model_file = model_name
        print(f"{ts()} [System] YOLO model ({model_name}) loaded successfully!", flush=True)
        return True
    except Exception as e:
        print(f"{ts()} [System Error] Failed to load YOLO model ({model_name}): {e}", flush=True)
        return False

def save_settings(turret):
    settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
    try:
        data = {
            "is_flipped": is_flipped,
            "invert_pan": invert_pan,
            "invert_tilt": invert_tilt,
            "conf_threshold": conf_threshold,
            "target_class": target_class,
            "camera_resolution": camera_resolution,
            "camera_quality": camera_quality,
            "enable_video_capture": enable_video_capture,
            "enable_zoom_2x": enable_zoom_2x,
            "enable_scanning_mode": enable_scanning_mode,
            "enable_image_improvement": enable_image_improvement,
            "current_model_file": current_model_file,
            "pan_limit_ccw": turret.pan_limit_ccw,
            "pan_limit_cw": turret.pan_limit_cw,
            "tilt_limit_min": turret.tilt_limit_min,
            "tilt_limit_max": turret.tilt_limit_max,
            "home_pan_angle": turret.home_pan_angle,
            "home_tilt_angle": turret.home_tilt_angle
        }
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"{ts()} [System Error] Failed to save settings: {e}", flush=True)

def load_settings(turret):
    global is_flipped, invert_pan, invert_tilt, conf_threshold, target_class, camera_resolution, camera_quality, enable_video_capture, enable_zoom_2x, enable_scanning_mode, enable_image_improvement, current_model_file
    settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as f:
                data = json.load(f)
            is_flipped = data.get("is_flipped", False)
            invert_pan = data.get("invert_pan", False)
            invert_tilt = data.get("invert_tilt", False)
            conf_threshold = data.get("conf_threshold", 0.25)
            target_class = data.get("target_class", "bird")
            camera_resolution = data.get("camera_resolution", 6)
            camera_quality = data.get("camera_quality", "medium")
            if not isinstance(camera_quality, str):
                camera_quality = "medium"
            enable_video_capture = data.get("enable_video_capture", False)
            enable_zoom_2x = data.get("enable_zoom_2x", False)
            enable_scanning_mode = data.get("enable_scanning_mode", True)
            enable_image_improvement = data.get("enable_image_improvement", False)
            current_model_file = data.get("current_model_file", "yolo26s.pt")
            
            # Load and auto-migrate CCW/CW pan limits to 0-180 degree range
            turret.pan_limit_ccw = data.get("pan_limit_ccw", 10.0)
            if turret.pan_limit_ccw is None or not (0.0 <= turret.pan_limit_ccw <= 180.0):
                turret.pan_limit_ccw = 10.0
                
            turret.pan_limit_cw = data.get("pan_limit_cw", 170.0)
            if turret.pan_limit_cw is None or not (0.0 <= turret.pan_limit_cw <= 180.0):
                turret.pan_limit_cw = 170.0
                
            turret.tilt_limit_min = data.get("tilt_limit_min", 10.0)
            if turret.tilt_limit_min is None or not (0.0 <= turret.tilt_limit_min <= 180.0):
                turret.tilt_limit_min = 10.0
                
            turret.tilt_limit_max = data.get("tilt_limit_max", 170.0)
            if turret.tilt_limit_max is None or not (0.0 <= turret.tilt_limit_max <= 180.0):
                turret.tilt_limit_max = 170.0
                
            turret.home_pan_angle = data.get("home_pan_angle", 90.0)
            if turret.home_pan_angle is None or not (0.0 <= turret.home_pan_angle <= 180.0):
                turret.home_pan_angle = 90.0
                
            turret.home_tilt_angle = data.get("home_tilt_angle", 90.0)
            if turret.home_tilt_angle is None or not (0.0 <= turret.home_tilt_angle <= 180.0):
                turret.home_tilt_angle = 90.0
                
            # Update target controls inside the turret class as well
            turret.target_pan_angle = turret.home_pan_angle
            turret.target_tilt_angle = turret.home_tilt_angle
            turret.pan_angle = turret.home_pan_angle
            turret.tilt_angle = turret.home_tilt_angle
            
            print(f"{ts()} [System] Loaded persistent settings from settings.json. Initialized home: Pan={turret.home_pan_angle}°, Tilt={turret.home_tilt_angle}°", flush=True)
        except Exception as e:
            print(f"{ts()} [System Error] Failed to load settings: {e}", flush=True)
