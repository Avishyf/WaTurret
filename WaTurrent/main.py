import os
import time
import socket
socket.setdefaulttimeout(10.0)
import threading
import collections
import traceback
from datetime import datetime
import cv2
import numpy as np
import requests
from flask import Flask, Response, request, send_from_directory, render_template
from zeroconf import Zeroconf, ServiceBrowser

import config
from turret_control import TurretController
from image_utils import improve_image_cv

# --- Suppress Flask Log Spam ---
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# Initialize Turret Controller and load configurations
turret = TurretController()
config.load_settings(turret)
config.load_yolo_model(config.current_model_file)

def get_quality_val(q_str):
    """Maps low/medium/high quality strings to ESP32 register values (lower = higher quality)."""
    if q_str == "high":
        return 10
    elif q_str == "low":
        return 45
    else:  # medium
        return 20

def sync_camera_settings_to_esp():
    if config.CONTROL_URL:
        try:
            qual_val = get_quality_val(config.camera_quality)
            print(f"{config.ts()} [System] Syncing camera settings to ESP32: flip={config.is_flipped}, resolution={config.camera_resolution}, quality={config.camera_quality} ({qual_val})", flush=True)
            requests.get(f"{config.CONTROL_URL}?var=flip&val={1 if config.is_flipped else 0}", timeout=2)
            requests.get(f"{config.CONTROL_URL}?var=framesize&val={config.camera_resolution}", timeout=2)
            requests.get(f"{config.CONTROL_URL}?var=quality&val={qual_val}", timeout=2)
        except Exception as e:
            print(f"{config.ts()} [System Warning] Failed to sync camera settings to ESP32: {e}", flush=True)

def cleanup_old_videos(directory):
    try:
        files = [os.path.join(directory, f) for f in os.listdir(directory) if f.startswith("detection_") and f.endswith(".mp4")]
        files.sort(key=os.path.getmtime)
        while len(files) > 10:
            oldest = files.pop(0)
            try:
                os.remove(oldest)
                oldest_thumb = oldest.replace(".mp4", ".jpg")
                if os.path.exists(oldest_thumb):
                    os.remove(oldest_thumb)
                print(f"{config.ts()} [Video Recorder] Rotated out oldest video and thumbnail: {os.path.basename(oldest)}", flush=True)
            except Exception as e:
                print(f"{config.ts()} [Video Recorder Warning] Failed to delete oldest video or thumbnail {oldest}: {e}", flush=True)
    except Exception as e:
        print(f"{config.ts()} [Video Recorder Error] Cleanup failed: {e}", flush=True)

def save_video_thread(frames, record_trigger_time=None, pip_bytes=None):
    if not frames:
        return
    try:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_videos")
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        duration = frames[-1][0] - frames[0][0]
        fps = len(frames) / duration if duration > 0 else 20.0
        fps = max(5.0, min(40.0, fps))
        
        timestamp_str = datetime.fromtimestamp(frames[0][0]).strftime("%Y%m%d_%H%M%S")
        filename = f"detection_{timestamp_str}.mp4"
        filepath = os.path.join(output_dir, filename)
        
        first_frame_bytes = frames[0][1]
        first_frame = cv2.imdecode(np.frombuffer(first_frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if first_frame is None:
            return
        h, w = first_frame.shape[:2]
        
        pip_small = None
        if pip_bytes is not None:
            pip_img = cv2.imdecode(np.frombuffer(pip_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if pip_img is not None:
                pip_w = w // 4
                pip_h = h // 4
                pip_small = cv2.resize(pip_img, (pip_w, pip_h), interpolation=cv2.INTER_AREA)
                cv2.rectangle(pip_small, (0, 0), (pip_w - 1, pip_h - 1), (0, 0, 255), 2)
        
        fourcc = cv2.VideoWriter_fourcc(*'avc1')
        out = cv2.VideoWriter(filepath, fourcc, fps, (w, h))
        if not out.isOpened():
            print(f"{config.ts()} [Video Recorder] Error opening video writer for {filepath}", flush=True)
            return
            
        print(f"{config.ts()} [Video Recorder] Compiling {len(frames)} frames into {filename} at {fps:.2f} FPS...", flush=True)
        for timestamp, f_bytes in frames:
            img = cv2.imdecode(np.frombuffer(f_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.write(img)
        out.release()
        
        thumb_filename = filename.replace(".mp4", ".jpg")
        thumb_filepath = os.path.join(output_dir, thumb_filename)
        if pip_small is not None:
            cv2.imwrite(thumb_filepath, pip_small)
        else:
            thumb_w = w // 4
            thumb_h = h // 4
            thumb_img = cv2.resize(first_frame, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA)
            cv2.rectangle(thumb_img, (0, 0), (thumb_w - 1, thumb_h - 1), (128, 128, 128), 2)
            cv2.imwrite(thumb_filepath, thumb_img)
            
        print(f"{config.ts()} [Video Recorder] Video and thumbnail saved successfully: {filename}", flush=True)
        cleanup_old_videos(output_dir)
    except Exception as e:
        print(f"{config.ts()} [Video Recorder Error] Failed to save video: {e}", flush=True)

class ESP32Discoverer:
    def __init__(self):
        self.found_ip = None

    def update_service(self, zc, type_, name): pass
    def remove_service(self, zc, type_, name): pass

    def add_service(self, zc, type_, name):
        if "esp32cam" in name.lower():
            info = zc.get_service_info(type_, name)
            if info and info.addresses:
                self.found_ip = ".".join(map(str, info.addresses[0]))
                print(f"{config.ts()} [Discovery] Found ESP32-CAM at IP: {self.found_ip}", flush=True)

def discover_esp32_ip(timeout=10):
    print(f"{config.ts()} [Discovery] Scanning local network for 'esp32cam.local'...", flush=True)
    try:
        zeroconf = Zeroconf()
        listener = ESP32Discoverer()
        browser = ServiceBrowser(zeroconf, "_http._tcp.local.", listener)
        
        start_time = time.time()
        while listener.found_ip is None and (time.time() - start_time) < timeout:
            time.sleep(0.1)
        zeroconf.close()
        return listener.found_ip
    except Exception as e:
        print(f"{config.ts()} [Discovery Warning] mDNS discovery failed: {e}", flush=True)
        return None

def update_placeholder_text(text):
    try:
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.8
        thickness = 2
        (w, h), _ = cv2.getTextSize(text, font, scale, thickness)
        cx = (640 - w) // 2
        cy = (480 + h) // 2
        cv2.putText(img, text, (cx, cy), font, scale, (255, 255, 255), thickness)
        _, buffer = cv2.imencode('.jpg', img)
        config.placeholder_frame = buffer.tobytes()
    except Exception:
        pass

def autonomous_ai_loop():
    print(f"{config.ts()} [AI] Autonomous tracking thread started.", flush=True)
    
    while True:
        if not config.ESP_IP:
            update_placeholder_text("DISCOVERING ESP32-CAM...")
            config.ESP_IP = discover_esp32_ip(timeout=8)
            if not config.ESP_IP:
                print(f"{config.ts()} [Discovery] ESP32-CAM not found. Retrying in 5s...", flush=True)
                update_placeholder_text("ESP32-CAM NOT FOUND. RETRYING...")
                time.sleep(5)
                continue
            
            config.STREAM_URL = f"http://{config.ESP_IP}:80/stream"     
            config.CONTROL_URL = f"http://{config.ESP_IP}:81/control"
            print(f"{config.ts()} [Discovery] Turret Online! IP: {config.ESP_IP}", flush=True)
            sync_camera_settings_to_esp()
        
        try:
            update_placeholder_text("CONNECTING TO STREAM...")
            config.camera_connected = False
            stream = requests.get(config.STREAM_URL, stream=True, timeout=(8, 10))
            if stream.status_code != 200:
                print(f"{config.ts()} [AI] Stream connection failed. Retrying in 2s...")
                update_placeholder_text("CONNECTION FAILED. RETRYING...")
                time.sleep(2)
                continue
                
            # Force raw stream socket timeout to prevent iter_content hangs
            try:
                if hasattr(stream.raw, 'connection') and stream.raw.connection:
                    sock = stream.raw.connection.sock
                    if sock:
                        sock.settimeout(10.0)
            except Exception as se:
                print(f"[AI] Failed to set stream socket timeout: {se}")
                
            print(f"{config.ts()} [AI] Connected to ESP32-CAM video stream!", flush=True)
            config.camera_connected = True
            bytes_buffer = bytes()
            last_processed_time = time.time()
            last_frame_time = time.time()
            last_firing_record_time = 0.0
            
            for chunk in stream.iter_content(chunk_size=1024):
                if time.time() - last_frame_time > 10.0:
                    print(f"{config.ts()} [AI] No valid frame received for > 10s. Forcing reconnect...", flush=True)
                    raise Exception("Frame timeout (> 10s)")
                
                bytes_buffer += chunk
                
                while True:
                    a = bytes_buffer.find(b'\xff\xd8')
                    if a == -1:
                        break
                    
                    b = bytes_buffer.find(b'\xff\xd9', a)
                    if b == -1:
                        break
                    
                    jpg = bytes_buffer[a:b+2]
                    bytes_buffer = bytes_buffer[b+2:]
                    
                    current_time = time.time()
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    
                    if frame is None:
                        continue
                        
                    if config.enable_zoom_2x:
                        h, w = frame.shape[:2]
                        y1, y2 = h // 4, 3 * h // 4
                        x1, x2 = w // 4, 3 * w // 4
                        frame = cv2.resize(frame[y1:y2, x1:x2], (w, h), interpolation=cv2.INTER_LINEAR)

                    if config.enable_image_improvement:
                        frame = improve_image_cv(frame)
                    
                    last_frame_time = current_time
                    frame_h, frame_w = frame.shape[:2]
                    cx, cy = frame_w // 2, frame_h // 2
                    dz_x, dz_y = int(frame_w * turret.deadzone_ratio_x), int(frame_h * turret.deadzone_ratio_y)
                    
                    should_run_yolo = (current_time - last_processed_time) >= config.FRAME_DELAY
                    
                    target_detected = False
                    if should_run_yolo:
                        last_processed_time = current_time
                        if config.target_class == "human":
                            target_cls_idx = 0
                        elif config.target_class == "bird":
                            target_cls_idx = 14
                        elif config.target_class == "cat":
                            target_cls_idx = 15
                        elif config.target_class == "dog":
                            target_cls_idx = 16
                        else:
                            target_cls_idx = 14
                        with config.model_lock:
                            if config.model is not None:
                                results = config.model(frame, classes=[target_cls_idx], conf=config.conf_threshold, verbose=False)
                                boxes = results[0].boxes
                            else:
                                boxes = []
                        
                        if len(boxes) > 0:
                            target_detected = True
                            box = boxes[0]
                            coords = box.xyxy[0].tolist()
                            conf = float(box.conf[0])
                            x1, y1, x2, y2 = int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3])
                            target_cx = int((x1 + x2) / 2)
                            target_cy = int((y1 + y2) / 2)
                            
                            if turret.pump_is_on:
                                cv2.putText(frame, "FIRING!", (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                            else:
                                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                                
                            cv2.circle(frame, (target_cx, target_cy), 5, (0, 0, 255), -1)
                            cv2.line(frame, (cx, cy), (target_cx, target_cy), (0, 255, 255), 1)
                            
                            if turret.is_scanning:
                                turret.abort_scan = True
                                
                            turret.update_tracking(target_cx, target_cy, frame_w, frame_h, config.target_class, conf)
                            turret.last_detection_time = current_time
                            turret.return_to_home_disabled = False
                        else:
                            turret.set_pump(False)
                            turret.is_firing_burst = False
                            
                            if not turret.return_to_home_disabled and (current_time - turret.last_detection_time > 30.0):
                                turret.target_pan_angle = turret.home_pan_angle
                                turret.target_tilt_angle = turret.home_tilt_angle
                                
                            if config.enable_scanning_mode and (not turret.is_scanning) and (current_time - turret.last_detection_time >= 180.0) and (current_time - turret.last_scan_time >= 180.0):
                                threading.Thread(target=turret.start_idle_scan, daemon=True).start()
                                        
                        turret.send_state()
                    
                    cv2.rectangle(frame, (cx - dz_x, cy - dz_y), (cx + dz_x, cy + dz_y), (255, 0, 0), 1)
                    
                    ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55 if config.camera_quality == "medium" else (20 if config.camera_quality == "low" else 90)])
                    if ret:
                        jpeg_bytes = buffer.tobytes()
                        with config.frame_lock:
                            config.latest_frame = jpeg_bytes
                            
                        config.pre_trigger_buffer.append((current_time, jpeg_bytes))
                        while config.pre_trigger_buffer and (current_time - config.pre_trigger_buffer[0][0] > 5.0):
                            config.pre_trigger_buffer.popleft()
                        
                        if config.enable_video_capture:
                            if turret.pump_is_on:
                                last_firing_record_time = current_time
                                if not config.is_recording:
                                    config.is_recording = True
                                    config.record_trigger_time = current_time
                                    if len(config.pre_trigger_buffer) >= 2:
                                        config.current_pip_frame_bytes = config.pre_trigger_buffer[-2][1]
                                    elif len(config.pre_trigger_buffer) >= 1:
                                        config.current_pip_frame_bytes = config.pre_trigger_buffer[-1][1]
                                    else:
                                        config.current_pip_frame_bytes = jpeg_bytes
                                    config.recording_frames = list(config.pre_trigger_buffer)
                                    print(f"{config.ts()} [Video Recorder] Recording triggered by firing! Pre-buffered {len(config.recording_frames)} frames.", flush=True)
                            
                            if config.is_recording:
                                config.recording_frames.append((current_time, jpeg_bytes))
                                if (current_time - last_firing_record_time >= 10.0) or (current_time - config.record_trigger_time >= 60.0):
                                    config.is_recording = False
                                    print(f"{config.ts()} [Video Recorder] Recording complete. Total frames: {len(config.recording_frames)}. Compiling...", flush=True)
                                    threading.Thread(target=save_video_thread, args=(config.recording_frames.copy(), config.record_trigger_time, config.current_pip_frame_bytes), daemon=True).start()
                                    config.recording_frames = []
                        else:
                            if config.is_recording:
                                config.is_recording = False
                                if len(config.recording_frames) > 10:
                                    threading.Thread(target=save_video_thread, args=(config.recording_frames.copy(), config.record_trigger_time, config.current_pip_frame_bytes), daemon=True).start()
                                config.recording_frames = []
                            
        except Exception as e:
            print(f"{config.ts()} [AI] Stream interrupted: {e}. Reconnecting...")
            config.camera_connected = False
            turret.set_pump(False)
            turret.is_firing_burst = False
            turret.target_pan_angle = turret.pan_neutral
            turret.send_state()
            update_placeholder_text("STREAM LOST. RECONNECTING...")
            time.sleep(2)

def sync_status_loop():
    while True:
        if config.CONTROL_URL:
            try:
                status_url = config.CONTROL_URL.replace('/control', '/status')
                r = requests.get(status_url, timeout=2)
                if r.status_code == 200:
                    data = r.json()
                    esp_flip = (data.get("flip", 0) == 1)
                    if esp_flip != config.is_flipped:
                        requests.get(f"{config.CONTROL_URL}?var=flip&val={1 if config.is_flipped else 0}", timeout=2)
            except Exception:
                pass
        time.sleep(2)

@app.route('/api/videos')
def api_list_videos():
    try:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_videos")
        if not os.path.exists(output_dir):
            return {"videos": []}
        files = [f for f in os.listdir(output_dir) if f.startswith("detection_") and f.endswith(".mp4")]
        files.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
        
        video_list = []
        for f in files:
            full_path = os.path.join(output_dir, f)
            stat = os.stat(full_path)
            size_mb = stat.st_size / (1024 * 1024)
            mtime = os.path.getmtime(full_path)
            time_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            video_list.append({
                "filename": f,
                "size_mb": round(size_mb, 2),
                "timestamp": time_str
            })
        return {"videos": video_list}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/videos/<path:filename>')
def serve_video(filename):
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_videos")
    return send_from_directory(output_dir, filename, as_attachment=False)

@app.route('/api/delete_video/<filename>', methods=['POST'])
def api_delete_video(filename):
    try:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_videos")
        filepath = os.path.join(output_dir, filename)
        if os.path.dirname(os.path.abspath(filepath)) != os.path.abspath(output_dir):
            return {"status": "error", "message": "Access denied"}, 403
        if os.path.exists(filepath):
            os.remove(filepath)
            thumb_filepath = filepath.replace(".mp4", ".jpg")
            if os.path.exists(thumb_filepath):
                try:
                    os.remove(thumb_filepath)
                except Exception:
                    pass
            print(f"{config.ts()} [Web API] Deleted video file and thumbnail: {filename}", flush=True)
            return {"status": "success"}
        else:
            return {"status": "error", "message": "File not found"}, 404
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/status')
def api_status():
    if config.is_flipped:
        screen_left = turret.pan_limit_cw
        screen_right = turret.pan_limit_ccw
        screen_top = turret.tilt_limit_max
        screen_bottom = turret.tilt_limit_min
    else:
        screen_left = turret.pan_limit_ccw
        screen_right = turret.pan_limit_cw
        screen_top = turret.tilt_limit_min
        screen_bottom = turret.tilt_limit_max

    return {
        "flip": 1 if config.is_flipped else 0, 
        "confidence": config.conf_threshold,
        "target_class": config.target_class,
        "resolution": config.camera_resolution,
        "quality": config.camera_quality,
        "video_capture": config.enable_video_capture,
        "zoom_2x": config.enable_zoom_2x,
        "scanning_mode": config.enable_scanning_mode,
        "image_improvement": config.enable_image_improvement,
        "model_file": config.current_model_file,
        "is_recording": config.is_recording,
        "pan_position": turret.pan_position,
        "pan_limit_left": screen_left,
        "pan_limit_right": screen_right,
        "tilt_limit_top": screen_top,
        "tilt_limit_bottom": screen_bottom,
        "tilt_angle": turret.target_tilt_angle,
        "firing": turret.pump_is_on,
        "camera_connected": config.camera_connected,
        "invert_pan": config.invert_pan,
        "invert_tilt": config.invert_tilt
    }

@app.route('/api/set_model', methods=['POST'])
def api_set_model():
    try:
        data = request.get_json(silent=True) or {}
        if not data or 'model' not in data:
            return {"status": "error", "message": "Missing model value"}, 400
        model_name = data['model']
        if model_name not in ["yolo26n.pt", "yolo26s.pt"]:
            return {"status": "error", "message": "Invalid model name"}, 400
            
        def bg_load():
            config.load_yolo_model(model_name)
            config.save_settings(turret)
            
        threading.Thread(target=bg_load, daemon=True).start()
        return {"status": "success", "model": model_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/toggle_zoom', methods=['POST'])
def api_toggle_zoom():
    config.enable_zoom_2x = not config.enable_zoom_2x
    print(f"{config.ts()} [Web API] POST /api/toggle_zoom - Action: Toggle 2x Zoom (New state: {config.enable_zoom_2x})", flush=True)
    config.save_settings(turret)
    return {"zoom_2x": config.enable_zoom_2x}

@app.route('/api/toggle_scanning', methods=['POST'])
def api_toggle_scanning():
    config.enable_scanning_mode = not config.enable_scanning_mode
    print(f"{config.ts()} [Web API] POST /api/toggle_scanning - Action: Toggle Scanning Mode (New state: {config.enable_scanning_mode})", flush=True)
    config.save_settings(turret)
    return {"scanning_mode": config.enable_scanning_mode}

@app.route('/api/toggle_image_improvement', methods=['POST'])
def api_toggle_image_improvement():
    config.enable_image_improvement = not config.enable_image_improvement
    print(f"{config.ts()} [Web API] POST /api/toggle_image_improvement - Action: Toggle Image Improvement (New state: {config.enable_image_improvement})", flush=True)
    config.save_settings(turret)
    return {"image_improvement": config.enable_image_improvement}

@app.route('/api/set_video_capture', methods=['POST'])
def api_set_video_capture():
    try:
        data = request.get_json(silent=True) or {}
        val = data.get("enable", False)
        config.enable_video_capture = val
        print(f"{config.ts()} [Web API] POST /api/set_video_capture - Action: Set Video Capture (New state: {config.enable_video_capture})", flush=True)
        config.save_settings(turret)
        return {"status": "success", "enable_video_capture": config.enable_video_capture}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/toggle_flip', methods=['POST'])
def api_toggle_flip():
    config.is_flipped = not config.is_flipped
    print(f"{config.ts()} [Web API] POST /api/toggle_flip - Action: Toggle Orientation Flip (New state: {config.is_flipped})", flush=True)
    sync_camera_settings_to_esp()
    config.save_settings(turret)
    return {"flip": 1 if config.is_flipped else 0}

@app.route('/api/toggle_invert_pan', methods=['POST'])
def api_toggle_invert_pan():
    config.invert_pan = not config.invert_pan
    print(f"{config.ts()} [Web API] POST /api/toggle_invert_pan - Action: Toggle Pan Inversion (New state: {config.invert_pan})", flush=True)
    config.save_settings(turret)
    return {"invert_pan": config.invert_pan}

@app.route('/api/toggle_invert_tilt', methods=['POST'])
def api_toggle_invert_tilt():
    config.invert_tilt = not config.invert_tilt
    print(f"{config.ts()} [Web API] POST /api/toggle_invert_tilt - Action: Toggle Tilt Inversion (New state: {config.invert_tilt})", flush=True)
    config.save_settings(turret)
    return {"invert_tilt": config.invert_tilt}

@app.route('/api/set_confidence', methods=['POST'])
def api_set_confidence():
    try:
        data = request.get_json(silent=True) or {}
        val = float(data.get("confidence", 0.25))
        config.conf_threshold = max(0.01, min(1.00, val))
        print(f"{config.ts()} [Web API] POST /api/set_confidence - Action: Set Confidence Threshold (New state: {config.conf_threshold:.2f})", flush=True)
        config.save_settings(turret)
        return {"status": "success", "confidence": config.conf_threshold}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 400

@app.route('/api/set_target_class', methods=['POST'])
def api_set_target_class():
    try:
        data = request.get_json(silent=True) or {}
        val = data.get("target", data.get("target_class", "bird")).strip().lower()
        if val in ["bird", "human"]:
            config.target_class = val
            print(f"{config.ts()} [Web API] POST /api/set_target_class - Action: Set Target Class (New state: {config.target_class})", flush=True)
            config.save_settings(turret)
            return {"status": "success", "target_class": config.target_class}
        else:
            return {"status": "error", "message": "Invalid target class"}, 400
    except Exception as e:
        return {"status": "error", "message": str(e)}, 400

@app.route('/api/fire_pump', methods=['POST'])
def api_fire_pump():
    try:
        data = request.get_json(silent=True) or {}
        duration_ms = int(data.get("duration", 300))
        duration_ms = max(50, min(2000, duration_ms))
        
        if turret.is_scanning:
            turret.abort_scan = True
            
        def trigger_burst():
            turret.set_pump(True)
            turret.send_state_immediate()
            time.sleep(duration_ms / 1000.0)
            turret.set_pump(False)
            turret.send_state_immediate()
            
        threading.Thread(target=trigger_burst, daemon=True).start()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/set_resolution', methods=['POST'])
def api_set_resolution():
    try:
        data = request.get_json(silent=True) or {}
        res_val = int(data.get("resolution", 6))
        config.camera_resolution = res_val
        print(f"{config.ts()} [Web API] POST /api/set_resolution - Action: Set Resolution Index (New state: {config.camera_resolution})", flush=True)
        sync_camera_settings_to_esp()
        config.save_settings(turret)
        return {"status": "success", "resolution": config.camera_resolution}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/set_quality', methods=['POST'])
def api_set_quality():
    try:
        data = request.get_json(silent=True) or {}
        qual_val = data.get("quality", "medium")
        if qual_val in ["low", "medium", "high"]:
            config.camera_quality = qual_val
            print(f"{config.ts()} [Web API] POST /api/set_quality - Action: Set Quality Profile (New state: {config.camera_quality})", flush=True)
            sync_camera_settings_to_esp()
            config.save_settings(turret)
            return {"status": "success", "quality": config.camera_quality}
        else:
            return {"status": "error", "message": "Invalid quality value"}, 400
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/manual_nudge', methods=['POST'])
def api_manual_nudge():
    try:
        data = request.get_json(silent=True) or {}
        direction = data.get("dir")
        
        if turret.is_scanning:
            turret.abort_scan = True
            
        step = 5.0
        pan_sign = -1 if config.invert_pan else 1
        tilt_sign = -1 if config.invert_tilt else 1
        
        if direction == "left":
            new_pan = turret.target_pan_angle - step * pan_sign if config.is_flipped else turret.target_pan_angle + step * pan_sign
            turret.target_pan_angle = max(turret.pan_limit_ccw if turret.pan_limit_ccw is not None else 10.0, 
                                          min(turret.pan_limit_cw if turret.pan_limit_cw is not None else 170.0, new_pan))
            print(f"{config.ts()} [Web API] Nudge Left - Target Pan: {turret.target_pan_angle}°", flush=True)
        elif direction == "right":
            new_pan = turret.target_pan_angle + step * pan_sign if config.is_flipped else turret.target_pan_angle - step * pan_sign
            turret.target_pan_angle = max(turret.pan_limit_ccw if turret.pan_limit_ccw is not None else 10.0, 
                                          min(turret.pan_limit_cw if turret.pan_limit_cw is not None else 170.0, new_pan))
            print(f"{config.ts()} [Web API] Nudge Right - Target Pan: {turret.target_pan_angle}°", flush=True)
        elif direction == "up":
            new_tilt = turret.target_tilt_angle - step * tilt_sign if config.is_flipped else turret.target_tilt_angle + step * tilt_sign
            turret.target_tilt_angle = max(turret.tilt_limit_min, min(turret.tilt_limit_max, new_tilt))
            print(f"{config.ts()} [Web API] Nudge Up - Target Tilt: {turret.target_tilt_angle}°", flush=True)
        elif direction == "down":
            new_tilt = turret.target_tilt_angle + step * tilt_sign if config.is_flipped else turret.target_tilt_angle - step * tilt_sign
            turret.target_tilt_angle = max(turret.tilt_limit_min, min(turret.tilt_limit_max, new_tilt))
            print(f"{config.ts()} [Web API] Nudge Down - Target Tilt: {turret.target_tilt_angle}°", flush=True)
            
        turret.send_state_immediate()
        return {"status": "success", "pan_position": turret.pan_position, "tilt_angle": turret.target_tilt_angle}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/set_limit', methods=['POST'])
def api_set_limit():
    try:
        if turret.is_scanning:
            return {"status": "error", "message": "Cannot modify limits during active scan"}, 400
            
        data = request.get_json(silent=True) or {}
        limit_type = data.get("type")
        
        if limit_type == "left":
            if config.is_flipped:
                turret.pan_limit_cw = float(turret.pan_position)
                print(f"{config.ts()} [Web API] Calibrated CW Pan Limit (Left): {turret.pan_limit_cw}°", flush=True)
            else:
                turret.pan_limit_ccw = float(turret.pan_position)
                print(f"{config.ts()} [Web API] Calibrated CCW Pan Limit (Left): {turret.pan_limit_ccw}°", flush=True)
        elif limit_type == "right":
            if config.is_flipped:
                turret.pan_limit_ccw = float(turret.pan_position)
                print(f"{config.ts()} [Web API] Calibrated CCW Pan Limit (Right): {turret.pan_limit_ccw}°", flush=True)
            else:
                turret.pan_limit_cw = float(turret.pan_position)
                print(f"{config.ts()} [Web API] Calibrated CW Pan Limit (Right): {turret.pan_limit_cw}°", flush=True)
        elif limit_type == "top":
            if config.is_flipped:
                turret.tilt_limit_max = float(turret.target_tilt_angle)
                print(f"{config.ts()} [Web API] Calibrated Max Tilt Limit (Top): {turret.tilt_limit_max}°", flush=True)
            else:
                turret.tilt_limit_min = float(turret.target_tilt_angle)
                print(f"{config.ts()} [Web API] Calibrated Min Tilt Limit (Top): {turret.tilt_limit_min}°", flush=True)
        elif limit_type == "bottom":
            if config.is_flipped:
                turret.tilt_limit_min = float(turret.target_tilt_angle)
                print(f"{config.ts()} [Web API] Calibrated Min Tilt Limit (Bottom): {turret.tilt_limit_min}°", flush=True)
            else:
                turret.tilt_limit_max = float(turret.target_tilt_angle)
                print(f"{config.ts()} [Web API] Calibrated Max Tilt Limit (Bottom): {turret.tilt_limit_max}°", flush=True)
                
        config.save_settings(turret)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route('/api/clear_limits', methods=['POST'])
def api_clear_limits():
    if turret.is_scanning:
        return {"status": "error", "message": "Cannot modify limits during active scan"}, 400
        
    turret.pan_limit_ccw = 10.0
    turret.pan_limit_cw = 170.0
    turret.tilt_limit_min = 10.0
    turret.tilt_limit_max = 170.0
    print(f"{config.ts()} [Web API] Limits Reset to defaults (10° - 170°)", flush=True)
    config.save_settings(turret)
    return {"status": "success"}

@app.route('/api/go_home', methods=['POST'])
def api_go_home():
    if turret.is_scanning:
        return {"status": "error", "message": "Cannot return to home during active scan"}, 400
        
    turret.target_pan_angle = turret.home_pan_angle
    turret.target_tilt_angle = turret.home_tilt_angle
    turret.send_state_immediate()
    print(f"{config.ts()} [Web API] Return to home manually triggered", flush=True)
    return {"status": "success"}

@app.route('/api/set_home', methods=['POST'])
def api_set_home():
    if turret.is_scanning:
        return {"status": "error", "message": "Cannot set home during active scan"}, 400
        
    turret.home_pan_angle = float(turret.pan_position)
    turret.home_tilt_angle = float(turret.target_tilt_angle)
    print(f"{config.ts()} [Web API] Calibration Saved - Set Home: Pan={turret.home_pan_angle}°, Tilt={turret.home_tilt_angle}°", flush=True)
    config.save_settings(turret)
    return {"status": "success", "pan_position": turret.home_pan_angle, "home_tilt_angle": turret.home_tilt_angle}

@app.route('/')
def index():
    """Serves the premium, responsive dashboard UI."""
    return render_template('index.html')

def stream_to_browser():
    """Generator function that yields the latest JPEG frame bytes as MJPEG boundary chunks."""
    while True:
        with config.frame_lock:
            frame = config.latest_frame
            
        if frame is None:
            frame = config.placeholder_frame
            
        if frame is not None:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.1) 

@app.route('/video_feed')
def video_feed():
    """Serves the MJPEG stream to the index template image source."""
    print(f"{config.ts()} [Web API] GET /video_feed - Action: Start Video Streaming Feed", flush=True)
    return Response(stream_to_browser(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    import sys
    
    # Check if manual IP argument or environment variable is provided to bypass Zeroconf discovery
    env_ip = os.environ.get("ESP_IP")
    if len(sys.argv) > 1:
        config.ESP_IP = sys.argv[1]
    elif env_ip:
        config.ESP_IP = env_ip

    if config.ESP_IP:
        config.STREAM_URL = f"http://{config.ESP_IP}:80/stream"
        config.CONTROL_URL = f"http://{config.ESP_IP}:81/control"
        print(f"{config.ts()} [System] Manual ESP32 IP override configured: {config.ESP_IP}", flush=True)
        sync_camera_settings_to_esp()

    # Start the daemon background threads
    threading.Thread(target=autonomous_ai_loop, daemon=True).start()
    threading.Thread(target=sync_status_loop, daemon=True).start()
    
    # Start local Flask HTTP listener on port 5001
    print(f"{config.ts()} [System] Initializing Flask dashboard server...", flush=True)
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
