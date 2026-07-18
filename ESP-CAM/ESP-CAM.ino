#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include "esp_http_server.h"
#include <ESPmDNS.h>
#include <ESP32Servo.h>
#include <Preferences.h>

/**
 * ============================================================================
 *                          NETWORK WIRING & CREDENTIALS
 * ============================================================================
 */
// WiFi Access Point credentials. ESP32-CAM will connect to this network.
const char* ssid = "Avishai_Shira1";
const char* password = "0503663222";

// UDP Listener Configuration for AI processing integration
WiFiUDP udp;
const unsigned int udpPort = 5005; // Listening port for Python UDP tracking packets
char packetBuffer[255];            // Buffer to hold incoming UDP packets

/**
 * ============================================================================
 *                          HARDWARE GPIO PIN ASSIGNMENTS
 * ============================================================================
 */
#define FLASH_GPIO_NUM     33  // Redirected to onboard red status LED to free up GPIO 4
#define PAN_PIN            12  // Horizontal panning servo (360-degree continuous rotation)
#define TILT_PIN           13  // Vertical tilting servo (180-degree standard rotation)
#define PUMP_PIN           4   // Moved canon/pump MOSFET trigger pin to GPIO 4 (Flash)

// Servo driver objects from ESP32Servo library
Servo panServo;
Servo tiltServo;

/**
 * ============================================================================
 *                    AI-THINKER ESP32-CAM CAMERA PIN MAP
 * ============================================================================
 */
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

/**
 * ============================================================================
 *                    MJPEG VIDEO STREAM BOUNDARY HEADERS
 * ============================================================================
 */
#define PART_BOUNDARY "123456789000000000000987654321"
static const char* _STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char* _STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char* _STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %10u\r\n\r\n";

// HTTP Server Handles
httpd_handle_t stream_httpd = NULL;  // Server running on Port 80 for stream/dashboard
httpd_handle_t control_httpd = NULL; // Server running on Port 81 for REST API control
bool flash_state = false;            // Current state of the Flash LED
Preferences preferences;             // Persistent preferences storage
bool flip_state = false;             // Image and direction flip status
int volatile pending_quality = -1;
int volatile pending_framesize = -1;

// Edge-triggered pump shot & auto-shutoff safety timer variables
bool is_firing = false;
unsigned long pump_fire_start = 0;
bool pump_trigger_armed = true;
int last_pan_val = 90;
int last_tilt_val = 90;

// Watchdog safety heartbeat variables
unsigned long last_packet_time = 0;  // Holds milliseconds of last valid tracking packet
bool ai_active = false;              // Set true when tracking client connects

/**
 * ============================================================================
 *                    WEB DASHBOARD HTML, CSS & JAVASCRIPT
 * ============================================================================
 */
const char* INDEX_HTML = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>ESP32-CAM Turret Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        body { 
            font-family: Arial, Helvetica, sans-serif; 
            text-align: center; 
            margin: 0 auto; 
            padding: 20px 10px; 
            background: linear-gradient(135deg, #0f0f15 0%, #151525 100%); 
            color: #fff; 
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .container { 
            max-width: 700px; 
            margin: 0 auto; 
            padding: 25px; 
            background: rgba(30, 30, 45, 0.65); 
            border-radius: 16px; 
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6); 
        }
        h2 { 
            margin-top: 0; 
            margin-bottom: 5px; 
            font-weight: 700;
            letter-spacing: 0.5px;
            background: linear-gradient(90deg, #00d2ff 0%, #00a6ff 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        #status { 
            color: #00d2ff; 
            font-weight: bold; 
            margin-bottom: 20px; 
            height: 20px; 
            font-size: 14px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }
        .stream-window { 
            position: relative;
            background: #000; 
            border-radius: 12px; 
            border: 1px solid rgba(255, 255, 255, 0.1); 
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
            max-width: 640px;
            margin: 0 auto;
            overflow: hidden;
        }
        img { 
            width: 100%; 
            height: auto;
            border-radius: 12px; 
            display: block; 
        }
        
        /* Joystick / D-Pad Overlay Styles */
        .joystick-overlay {
            position: absolute;
            bottom: 15px;
            left: 15px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
            z-index: 10;
        }
        .horizontal-arrows {
            display: flex;
            gap: 40px; /* Spacing in the center of the D-Pad */
        }
        .arrow-btn {
            width: 44px;
            height: 44px;
            background: rgba(15, 15, 25, 0.55);
            border: 1.5px solid rgba(255, 255, 255, 0.25);
            color: rgba(255, 255, 255, 0.85);
            border-radius: 50%;
            font-size: 16px;
            cursor: pointer;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: center;
            user-select: none;
            -webkit-user-select: none;
            touch-action: manipulation;
            transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .arrow-btn:active {
            background: rgba(0, 166, 255, 0.6);
            border-color: rgba(0, 210, 255, 0.8);
            color: #fff;
            transform: scale(0.92);
            box-shadow: 0 0 10px rgba(0, 166, 255, 0.4);
        }
        
        /* Action Overlay Styles (Target Firing) */
        .action-overlay {
            position: absolute;
            bottom: 15px;
            right: 15px;
            display: flex;
            align-items: center;
            gap: 12px;
            z-index: 10;
        }
        .action-btn {
            width: 48px;
            height: 48px;
            background: rgba(15, 15, 25, 0.55);
            border: 1.5px solid rgba(255, 255, 255, 0.25);
            color: rgba(255, 255, 255, 0.85);
            border-radius: 8px; /* Square button */
            cursor: pointer;
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            display: flex;
            align-items: center;
            justify-content: center;
            user-select: none;
            -webkit-user-select: none;
            touch-action: manipulation;
            transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .action-btn:active, .action-btn.active {
            background: rgba(255, 75, 75, 0.65);
            border-color: rgba(255, 90, 90, 0.85);
            color: #fff;
            transform: scale(0.92);
            box-shadow: 0 0 12px rgba(255, 75, 75, 0.45);
        }
        .action-btn svg {
            transition: transform 0.15s;
        }
        .action-btn:active svg {
            transform: scale(0.9);
        }

        /* GPIO Pinout Wiring Guide Card */
        .wiring-guide {
            margin-top: 25px;
            padding: 20px;
            background: rgba(255, 255, 255, 0.04);
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.08);
            text-align: left;
            box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.05);
        }
        .wiring-guide h3 {
            margin-top: 0;
            margin-bottom: 12px;
            font-size: 15px;
            font-weight: 700;
            color: #00d2ff;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .wiring-guide ul {
            list-style: none;
            padding: 0;
            margin: 0;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 10px;
            font-size: 13.5px;
            color: #ccc;
        }
        .wiring-guide li {
            padding: 6px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            display: flex;
            justify-content: space-between;
        }
        .wiring-guide li:last-child {
            border-bottom: none;
        }
        .wiring-guide .pin-label {
            color: rgba(255, 255, 255, 0.85);
            font-weight: 600;
        }
        .wiring-guide .pin-num {
            background: rgba(0, 210, 255, 0.15);
            color: #00d2ff;
            padding: 2px 8px;
            border-radius: 4px;
            font-family: monospace;
            font-weight: bold;
            font-size: 12px;
            border: 1px solid rgba(0, 210, 255, 0.25);
        }
        
        /* Red target overlay in video stream */
        .red-target {
            position: absolute;
            top: 60%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 32px;
            height: 32px;
            border: 2px solid rgba(255, 59, 48, 0.85);
            border-radius: 50%;
            pointer-events: none;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 0 6px rgba(255, 59, 48, 0.45);
            z-index: 5;
        }
        .red-target::before {
            content: '';
            position: absolute;
            width: 44px;
            height: 2px;
            background: rgba(255, 59, 48, 0.85);
        }
        .red-target::after {
            content: '';
            position: absolute;
            width: 2px;
            height: 44px;
            background: rgba(255, 59, 48, 0.85);
        }
        .red-target-dot {
            width: 6px;
            height: 6px;
            background: #ff3b30;
            border-radius: 50%;
            z-index: 6;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>Dual-Axis Water Turret</h2>
        <div id="status">Ready</div>
        
        <div class="stream-window">
            <img src="/stream" id="stream">
            
            <!-- Red Target Overlaid (Bit lower than middle) -->
            <div class="red-target">
                <div class="red-target-dot"></div>
            </div>
            
            <!-- D-Pad Joystick Overlay (Bottom Left) -->
            <div class="joystick-overlay">
                <button class="arrow-btn" id="btn-up" 
                        onmousedown="startMove('up', event)" onmouseup="stopMove(event)" onmouseleave="stopMove(event)" 
                        ontouchstart="startMove('up', event)" ontouchend="stopMove(event)">&#9650;</button>
                <div class="horizontal-arrows">
                    <button class="arrow-btn" id="btn-left" 
                            onmousedown="startMove('left', event)" onmouseup="stopMove(event)" onmouseleave="stopMove(event)" 
                            ontouchstart="startMove('left', event)" ontouchend="stopMove(event)">&#9664;</button>
                    <button class="arrow-btn" id="btn-right" 
                            onmousedown="startMove('right', event)" onmouseup="stopMove(event)" onmouseleave="stopMove(event)" 
                            ontouchstart="startMove('right', event)" ontouchend="stopMove(event)">&#9654;</button>
                </div>
                <button class="arrow-btn" id="btn-down" 
                        onmousedown="startMove('down', event)" onmouseup="stopMove(event)" onmouseleave="stopMove(event)" 
                        ontouchstart="startMove('down', event)" ontouchend="stopMove(event)">&#9660;</button>
            </div>
            
            <!-- Actions Overlay (Bottom Right) -->
            <div class="action-overlay">
                <!-- Target Fire Button (Water Pump) -->
                <button class="action-btn" id="pumpBtn" onclick="firePump()" title="Fire 0.3-Second Water Burst">
                    <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2.5">
                        <circle cx="12" cy="12" r="9"></circle>
                        <circle cx="12" cy="12" r="5"></circle>
                        <circle cx="12" cy="12" r="1.5" fill="currentColor"></circle>
                        <line x1="12" y1="1" x2="12" y2="23"></line>
                        <line x1="1" y1="12" x2="23" y2="12"></line>
                    </svg>
                </button>
                <!-- Flip Orientation Toggle Button -->
                <button class="action-btn" id="flipBtn" onclick="toggleFlip()" title="Toggle Image & Direction Flip">
                    <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.5">
                        <path d="M23 4v6h-6"></path>
                        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                    </svg>
                </button>
            </div>
        </div>

        <!-- Transparent GPIO Pinout Wiring Guide Card -->
        <div class="wiring-guide">
            <h3>
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align: middle;">
                    <rect x="2" y="2" width="20" height="20" rx="4"></rect>
                    <line x1="6" y1="2" x2="6" y2="22"></line>
                    <line x1="18" y1="2" x2="18" y2="22"></line>
                    <line x1="2" y1="6" x2="22" y2="6"></line>
                    <line x1="2" y1="18" x2="22" y2="18"></line>
                </svg>
                Hardware Pinout Connections
            </h3>
            <ul>
                <li>
                    <span class="pin-label">Pan Servo (180° Angle control):</span>
                    <span class="pin-num">GPIO 12</span>
                </li>
                <li>
                    <span class="pin-label">Tilt Servo (180° Angle control):</span>
                    <span class="pin-num">GPIO 13</span>
                </li>
                <li>
                    <span class="pin-label">Water Pump MOSFET Trigger:</span>
                    <span class="pin-num">GPIO 4 (Flash)</span>
                </li>
                <li>
                    <span class="pin-label">Onboard Status LED:</span>
                    <span class="pin-num">GPIO 33 (Red LED)</span>
                </li>
            </ul>
        </div>
    </div>

    <script>
        const statusDiv = document.getElementById('status');
        const apiBase = `http://${window.location.hostname}:81/control`;
        
        let currentPan = 90;       // Starts centered (90 degrees)
        let panInterval = null;    // Interval reference for continuous pan movement
        let currentTilt = 90;      // Starts centered (90 degrees)
        let tiltInterval = null;   // Interval reference for continuous tilt movement
        let pumpTimeout = null;    // Timeout reference for water burst safety timer
        let isFlipped = false;     // Controls direction inversion and UI state

        // Query persistent flip state from ESP32 status endpoint
        function fetchStatus() {
            const statusUrl = `http://${window.location.hostname}:81/status`;
            fetch(statusUrl)
                .then(res => res.json())
                .then(data => {
                    isFlipped = (data.flip === 1);
                    updateFlipUI();
                })
                .catch(err => console.error("Error fetching status:", err));
        }

        function updateFlipUI() {
            const btn = document.getElementById('flipBtn');
            if (isFlipped) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        }

        function toggleFlip() {
            isFlipped = !isFlipped;
            sendCommand('flip', isFlipped ? 1 : 0);
            updateFlipUI();
        }

        // Helper to send HTTP control commands to ESP32 control server (Port 81)
        function sendCommand(variable, value) {
            fetch(`${apiBase}?var=${variable}&val=${value}`)
                .catch(err => {
                    statusDiv.innerText = "Connection lost";
                    console.error("Control error:", err);
                });
        }

        // Triggered when arrow button is pressed down (mouse or touch)
        function startMove(dir, e) {
            if (e && e.cancelable) e.preventDefault(); // Stop default scroll/magnification
            statusDiv.innerText = `Moving: ${dir.toUpperCase()}`;
            
            // Map arrow keys based on flip state
            let mappedDir = dir;
            if (isFlipped) {
                if (dir === 'left') mappedDir = 'right';
                else if (dir === 'right') mappedDir = 'left';
                else if (dir === 'up') mappedDir = 'down';
                else if (dir === 'down') mappedDir = 'up';
            }

            if (mappedDir === 'left') {
                if (panInterval) clearInterval(panInterval);
                // Gradually increment pan angle every 50ms while held (corrects reversed left direction)
                panInterval = setInterval(() => {
                    currentPan = Math.min(170, currentPan + 3);
                    sendCommand('pan', currentPan);
                }, 50);
            } else if (mappedDir === 'right') {
                if (panInterval) clearInterval(panInterval);
                // Gradually decrement pan angle every 50ms while held (corrects reversed right direction)
                panInterval = setInterval(() => {
                    currentPan = Math.max(10, currentPan - 3);
                    sendCommand('pan', currentPan);
                }, 50);
            } else if (mappedDir === 'up') {
                if (tiltInterval) clearInterval(tiltInterval);
                // Gradually increment tilt angle (tilts camera UP) every 50ms while held
                // Note: UP arrow increases the physical height/tilt value
                tiltInterval = setInterval(() => {
                    currentTilt = Math.min(170, currentTilt + 3);
                    sendCommand('tilt', currentTilt);
                }, 50);
            } else if (mappedDir === 'down') {
                if (tiltInterval) clearInterval(tiltInterval);
                // Gradually decrement tilt angle (tilts camera DOWN) every 50ms while held
                // Note: DOWN arrow decreases the physical height/tilt value
                tiltInterval = setInterval(() => {
                    currentTilt = Math.max(10, currentTilt - 3);
                    sendCommand('tilt', currentTilt);
                }, 50);
            }
        }

        // Triggered when arrow button is released or cursor leaves the button area
        function stopMove(e) {
            if (e && e.cancelable) e.preventDefault();
            statusDiv.innerText = "Ready";
            
            if (panInterval) {
                clearInterval(panInterval);
                panInterval = null;
            }
            if (tiltInterval) {
                clearInterval(tiltInterval);
                tiltInterval = null;
            }
        }

        // Fires a safe, automatic 0.3-second burst of water
        function firePump() {
            if (pumpTimeout) return; // Ignore input if a burst is already in progress
            
            statusDiv.innerText = "FIRING WATER BURST!";
            const btn = document.getElementById('pumpBtn');
            btn.classList.add('active');
            sendCommand('pump', 1);
            
            pumpTimeout = setTimeout(() => {
                sendCommand('pump', 0);
                btn.classList.remove('active');
                statusDiv.innerText = "Ready";
                pumpTimeout = null;
            }, 300);
        }

        // Sync flip status on page load
        fetchStatus();
    </script>
</body>
</html>
)rawliteral";

/**
 * ============================================================================
 *                          HTTP SERVER HANDLERS
 * ============================================================================
 */
// Serves the glassmorphic manual control dashboard UI (Port 80 /)
static esp_err_t index_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html; charset=utf-8"); // Enforces UTF-8 symbol parsing
    return httpd_resp_send(req, INDEX_HTML, strlen(INDEX_HTML));
}

// Serves the live camera MJPEG stream chunk-by-chunk (Port 80 /stream)
static esp_err_t stream_handler(httpd_req_t *req) {
    camera_fb_t * fb = NULL;
    esp_err_t res = ESP_OK;
    size_t _jpg_buf_len = 0;
    uint8_t * _jpg_buf = NULL;
    char * part_buf[64];

    res = httpd_resp_set_type(req, _STREAM_CONTENT_TYPE);
    if(res != ESP_OK) return res;

    while(true) {
        // Apply pending settings in the stream thread before capturing the next frame
        if (pending_framesize != -1 || pending_quality != -1) {
            sensor_t * s = esp_camera_sensor_get();
            if (s) {
                if (pending_framesize != -1) {
                    s->set_framesize(s, (framesize_t)pending_framesize);
                    pending_framesize = -1;
                    delay(100); // Allow sensor to stabilize
                }
                if (pending_quality != -1) {
                    s->set_quality(s, pending_quality);
                    pending_quality = -1;
                    delay(50); // Allow sensor to stabilize
                }
            }
        }
        
        fb = esp_camera_fb_get(); // Grab frame from camera sensor
        if (!fb) {
            res = ESP_FAIL;
        } else {
            _jpg_buf_len = fb->len;
            _jpg_buf = fb->buf;
        }
        
        // Send boundary chunk dividers
        if(res == ESP_OK) {
            size_t hlen = snprintf((char *)part_buf, 64, _STREAM_PART, _jpg_buf_len);
            res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
            if(res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
            if(res == ESP_OK) res = httpd_resp_send_chunk(req, _STREAM_BOUNDARY, strlen(_STREAM_BOUNDARY));
        }
        
        if(fb) {
            esp_camera_fb_return(fb); // Release frame buffer back to camera driver
            fb = NULL;
            _jpg_buf = NULL;
        } 
        if(res != ESP_OK) break;
        delay(1); 
    }
    return res;
}

// Centralized pump control to toggle MOSFET
void setPumpState(int val) {
    digitalWrite(PUMP_PIN, val == 1 ? HIGH : LOW);
}

// Processes HTTP API REST requests for manual overrides (Port 81 /control?var=...&val=...)
static esp_err_t control_handler(httpd_req_t *req) {
    char buf[128];
    char variable[32] = {0,};
    char value[32] = {0,};

    // Allow requests from all origins (CORS) for external Python client integration
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    if (httpd_req_get_url_query_str(req, buf, sizeof(buf)) == ESP_OK) {
        if (httpd_query_key_value(buf, "var", variable, sizeof(variable)) == ESP_OK &&
            httpd_query_key_value(buf, "val", value, sizeof(value)) == ESP_OK) {
            
            int val = atoi(value);
            sensor_t * s = esp_camera_sensor_get(); 

            if (strcmp(variable, "framesize") == 0) {
                pending_framesize = val;
            } 
            else if (strcmp(variable, "quality") == 0) {
                pending_quality = val;
            }
            else if (strcmp(variable, "flash") == 0) {
                flash_state = (val == 1);
                digitalWrite(FLASH_GPIO_NUM, flash_state ? HIGH : LOW);
            }
            else if (strcmp(variable, "pan") == 0) {
                // Drives the 360-degree pan servo: supports degrees (0-180) and microseconds (1000-2000)
                if ((val >= 0 && val <= 180) || (val >= 1000 && val <= 2000)) {
                    panServo.write(val);
                    last_pan_val = val;
                }
            }
            else if (strcmp(variable, "tilt") == 0) {
                // Drives the standard 180-degree tilt servo: supports degrees (0-180) and microseconds (500-2400)
                if ((val >= 0 && val <= 180) || (val >= 500 && val <= 2400)) {
                    tiltServo.write(val);
                    last_tilt_val = val;
                }
            }
            else if (strcmp(variable, "pump") == 0) {
                // Actuates the IRLZ44N MOSFET of the water pump with edge-triggering
                if (val == 1) {
                    if (pump_trigger_armed && !is_firing) {
                        is_firing = true;
                        pump_fire_start = millis();
                        pump_trigger_armed = false;
                        setPumpState(1);
                    }
                } else {
                    pump_trigger_armed = true;
                }
                Serial.printf("Pump state manually set to: %d\n", val);
            }
            else if (strcmp(variable, "flip") == 0) {
                flip_state = (val == 1);
                preferences.putBool("flip", flip_state);
                if (s) {
                    s->set_vflip(s, flip_state ? 1 : 0);
                    s->set_hmirror(s, flip_state ? 0 : 1); // Correct mirroring for the new sensor
                }
                Serial.printf("Flip state set to: %d and saved to preferences\n", flip_state);
            }
        }
    }
    return httpd_resp_send(req, NULL, 0);
}

/**
 * ============================================================================
 *                          SERVER INITIALIZATION
 * ============================================================================
 */
// Serves the current system status (Port 81 /status)
static esp_err_t status_handler(httpd_req_t *req) {
    char json_response[64];
    snprintf(json_response, sizeof(json_response), "{\"flip\":%d}", flip_state ? 1 : 0);
    
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, json_response, strlen(json_response));
}

/**
 * ============================================================================
 *                          SERVER INITIALIZATION
 * ============================================================================
 */
void startCameraServer() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    
    // Server Endpoints on Port 80
    httpd_uri_t index_uri = { .uri = "/", .method = HTTP_GET, .handler = index_handler, .user_ctx = NULL };
    httpd_uri_t stream_uri = { .uri = "/stream", .method = HTTP_GET, .handler = stream_handler, .user_ctx = NULL };
    // Server Endpoints on Port 81
    httpd_uri_t control_uri = { .uri = "/control", .method = HTTP_GET, .handler = control_handler, .user_ctx = NULL };
    httpd_uri_t status_uri = { .uri = "/status", .method = HTTP_GET, .handler = status_handler, .user_ctx = NULL };

    // Start Port 80 HTTP server for stream and dashboard UI
    config.server_port = 80;
    config.ctrl_port = 32768; 
    if (httpd_start(&stream_httpd, &config) == ESP_OK) {
        httpd_register_uri_handler(stream_httpd, &index_uri);
        httpd_register_uri_handler(stream_httpd, &stream_uri);
    }

    // Start Port 81 HTTP server for REST API
    config.server_port = 81;
    config.ctrl_port = 32769; 
    if (httpd_start(&control_httpd, &config) == ESP_OK) {
        httpd_register_uri_handler(control_httpd, &control_uri);
        httpd_register_uri_handler(control_httpd, &status_uri);
    }
}

/**
 * ============================================================================
 *                          ARDUINO SETUP ROUTINE
 * ============================================================================
 */
void setup() {
    Serial.begin(115200);
    
    // Initialize Preferences and load persistent flip state
    preferences.begin("turret", false);
    flip_state = preferences.getBool("flip", false);
    
    // Initialize Flash LED pin
    pinMode(FLASH_GPIO_NUM, OUTPUT);
    digitalWrite(FLASH_GPIO_NUM, LOW);
    
    // Initialize Water Pump MOSFET pin
    pinMode(PUMP_PIN, OUTPUT);
    digitalWrite(PUMP_PIN, LOW); // Safe default (pump off)

    // Assign PWM timers for ESP32 hardware servo generation (allocate Timer 2 and Timer 3 to avoid conflicts with Camera XCLK on Timer 0/1)
    ESP32PWM::allocateTimer(2);
    ESP32PWM::allocateTimer(3);
    
    // Setup PWM frequency (standard 50Hz for analog/digital hobby servos)
    panServo.setPeriodHertz(50);
    tiltServo.setPeriodHertz(50);
    
    // Attach servo pin controls:
    // - Standard 180-degree pan servo (attach full 500us-2400us range)
    // - Standard 180-degree tilt servo (attach full 500us-2400us range)
    panServo.attach(PAN_PIN, 500, 2400); 
    tiltServo.attach(TILT_PIN, 500, 2400);
    
    // Command default centring values (Pan = 90 neutral stop, Tilt = 90 degrees center)
    panServo.write(90);
    tiltServo.write(90);
    
    // Setup camera structure mapping pins
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_sscb_sda = SIOD_GPIO_NUM;
    config.pin_sscb_scl = SIOC_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;
    
    config.xclk_freq_hz = 10000000; // Camera clock frequency (10MHz)
    config.pixel_format = PIXFORMAT_JPEG;
    
    // Configure resolution based on board PSRAM hardware availability
    if(psramFound()){
        config.frame_size = FRAMESIZE_VGA; 
        config.jpeg_quality = 12;
        config.fb_count = 1; 
    } else {
        config.frame_size = FRAMESIZE_CIF;
        config.jpeg_quality = 12;
        config.fb_count = 1;
    }

    // Initialize camera driver
    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("Camera init failed with error 0x%x\n", err);
        delay(1000);
        ESP.restart(); // Safe reboot and retry
    }
    
    // Retrieve the pointer to the camera sensor interface
    sensor_t * s = esp_camera_sensor_get();
    if (s) {
        s->set_vflip(s, flip_state ? 1 : 0);
        s->set_hmirror(s, flip_state ? 0 : 1); // Correct mirroring for the new sensor
        Serial.printf("Camera sensor initialized. Flip state: %d\n", flip_state);
    }
    
    // Establish network connection
    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) { 
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nWiFi Connected!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());

    // Advertise on local network using mDNS (resolves esp32cam.local)
    if (MDNS.begin("esp32cam")) {
        MDNS.addService("http", "tcp", 80);
    }
    
    // Boot HTTP servers and start UDP receiver
    startCameraServer();
    udp.begin(udpPort); 
}

/**
 * ============================================================================
 *                          ARDUINO BACKGROUND LOOP
 * ============================================================================
 */
void loop() {
    // Check for incoming AI UDP control packets
    int packetSize = udp.parsePacket();
    if (packetSize) {
        int len = udp.read(packetBuffer, 255);
        if (len > 0) packetBuffer[len] = 0;
        
        int p_val, t_val, w_val, f_val;
        // Parse CSV command packet structure: "pan,tilt,pump,flash"
        if (sscanf(packetBuffer, "%d,%d,%d,%d", &p_val, &t_val, &w_val, &f_val) == 4) {
            // Set pump state with edge-triggering
            if (w_val == 1) {
                if (pump_trigger_armed && !is_firing) {
                    is_firing = true;
                    pump_fire_start = millis();
                    pump_trigger_armed = false;
                    setPumpState(1);
                }
            } else {
                pump_trigger_armed = true;
            }

            // Write incoming speeds/angles to pan/tilt hardware pins directly
            if ((p_val >= 0 && p_val <= 180) || (p_val >= 500 && p_val <= 2400)) {
                panServo.write(p_val);
                last_pan_val = p_val;
            }
            if ((t_val >= 0 && t_val <= 180) || (t_val >= 500 && t_val <= 2400)) {
                tiltServo.write(t_val);
                last_tilt_val = t_val;
            }
            digitalWrite(FLASH_GPIO_NUM, f_val == 1 ? HIGH : LOW);
            flash_state = (f_val == 1);
            
            last_packet_time = millis(); // Refresh watchdog heartbeat timestamp
            ai_active = true;            // Mark AI client as online
        }
        // Fallback backward-compatible legacy parser (single char flash trigger)
        else if (packetBuffer[0] == '1') {
            digitalWrite(FLASH_GPIO_NUM, HIGH);
            flash_state = true;
            last_packet_time = millis();
            ai_active = true;
        } else if (packetBuffer[0] == '0') {
            digitalWrite(FLASH_GPIO_NUM, LOW);
            flash_state = false;
            last_packet_time = millis();
            ai_active = true;
        }
    }
    
    // AI Connection Watchdog: Safety shut down of water pump MOSFET and LED
    // if Python tracking client disconnects or freezes for more than 2 seconds (2000ms)
    if (ai_active && (millis() - last_packet_time > 2000)) {
        setPumpState(0);       // Disengage water pump MOSFET
        digitalWrite(FLASH_GPIO_NUM, LOW);  // Disengage Flash LED
        flash_state = false;
        ai_active = false;                 // Reset client status
        Serial.println("[WATCHDOG] AI connection lost! Safely shut down pump and flash.");
    }
    
    // Auto-shutoff safety timer for edge-triggered pump shot
    if (is_firing && (millis() - pump_fire_start >= 300)) {
        is_firing = false;
        setPumpState(0);
        Serial.println("[Timer] Auto-shutoff activated after 300ms burst.");
    }
    
    delay(10); 
}