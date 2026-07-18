# 🎯 WaTurrent — AI-Powered Water Sentry Turret

An intelligent sentry turret system built on an **ESP32-CAM** and a **Python YOLO AI backend**.
WaTurrent automatically detects, tracks, and sprays selected targets (supporting Humans, Birds, Cats, and Dogs) while providing web dashboard for manual override and remote configuration.

---

## 🚀 Key Features

* 🧠 **YOLOv26 Target Detection**: Real-time object detection and classification targeting Humans, Birds, Cats, or Dogs.
* 🔄 **Autonomous Target Tracking**: Automatically centers and tracks targets smoothly using PID-like servo adjustments.
* 🔫 **Auto-Firing Water Sentry**: Actuates a 5V DC water pump via an N-channel logic-level MOSFET switch.
* 🎥 **Auto-Record Detections**: Saves a high-quality video recording of targets as they are sprayed, playable directly from the built-in video archive on the dashboard.
* 📱 **Premium Web Dashboard**: Dark-mode glassmorphic interface with:
  * Live camera stream and interactive overlays (Digital Zoom, Manual Fire Burst).
  * Independent **Movement Settings** panel (Invert Pan, Invert Tilt, Auto-Scanning Sweeps).
  * Interactive **Question Mark Tooltips** next to settings for instant explanations.
* 📦 **Docker & Portainer Ready**: Fully containerized setup support using Docker Compose.

---

## 🛠️ Hardware Requirements

1. **ESP32-CAM** board (AI-Thinker model recommended).
2. **Pan Servo**: 180° standard or 360° continuous rotation servo.
3. **Tilt Servo**: 180° standard servo.
4. **5V DC Water Pump** (or solenoid valve).
5. **IRLZ44N N-Channel MOSFET** (logic-level gate).
6. **1N4007 Diode** (flyback protection for the pump).
7. **Resistors**: `220Ω` (Gate current limit) and `10kΩ` (Gate pull-down).
8. **Capacitor**: 1100uF, 220uF for motor powering and esp smooth operation.
9. **Power Supply**: 5V/3A power adapter.

---

## 🔌 Wiring & MOSFET Diagram


added full schematics for the project

---

## 📦 Installation & Setup

### 1. ESP32-CAM Firmware
1. Open the [ESP-CAM.ino](ESP-CAM/ESP-CAM.ino) sketch in the Arduino IDE.
2. Configure your Wi-Fi credentials in the sketch:
   ```cpp
   const char* ssid = "YOUR_WIFI_SSID";
   const char* password = "YOUR_WIFI_PASSWORD";
   ```
3. Set your camera board select to **`CAMERA_MODEL_AI_THINKER`**.
4. Compile and flash the code to your ESP32-CAM.

### 2. Native Python Setup (Windows)
1. Install Python 3.10+.
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the application locally by double-clicking the [run_turret.bat](run_turret.bat) startup script, or run:
   ```bash
   python WaTurrent/main.py
   ```

### 3. Docker Container Setup

If you prefer running the turret in a containerized environment, you can either pull the prebuilt image directly from Docker Hub or build it locally from the source.

#### Option A: Deploy using Docker Hub (No local build needed)
Simply pull and run the prebuilt image from Docker Hub (`avishyf/waturrent:latest`):

* **Using Docker CLI**:
  ```bash
  docker run -d \
    --name waturrent \
    --network host \
    --restart unless-stopped \
    avishyf/waturrent:latest
  ```
  *(Note: If you are on Windows/macOS where `--network host` is not supported, pass the camera IP explicitly via environment variables):*
  ```bash
  docker run -d \
    --name waturrent \
    -p 5001:5001 \
    -e ESP_IP=192.168.1.150 \
    --restart unless-stopped \
    avishyf/waturrent:latest
  ```

* **Using Docker Compose**:
  Use the following stack configuration:
  ```yaml
  version: '3.8'
  services:
    waturrent:
      image: avishyf/waturrent:latest
      container_name: waturrent
      network_mode: host # Comment out and use ports/ESP_IP on Windows/macOS
      restart: unless-stopped
  ```
  And launch it:
  ```bash
  docker compose up -d
  ```

#### Option B: Build and run locally (For code modifications)
If you made changes to the python files or webpage styles:
1. Rebuild the container locally from the root folder:
   ```bash
   docker compose build --no-cache
   ```
2. Start the container stack:
   ```bash
   docker compose up -d
   ```
3. Access the dashboard web portal in your browser at `http://localhost:5001`.

---

## 🖥️ Web Dashboard API Endpoints

The Flask server hosts several REST endpoints:
* `POST /api/manual_nudge`: Triggers manual pan/tilt steps. Bypasses software smoothing limits for instant mechanical response.
* `POST /api/fire_pump`: Triggers a configured millisecond burst fire.
* `POST /api/toggle_zoom`: Switches between standard and 2x digital zoom.
* `POST /api/toggle_invert_pan` / `POST /api/toggle_invert_tilt`: Persistent axes inversion settings.
* `GET /api/status`: Returns current turret coordinates, connection health, and active settings values.
