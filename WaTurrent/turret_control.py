import time
import socket
import threading
import config

class TurretController:
    def __init__(self):
        # Target motor control variables
        self.pan_neutral = 90.0
        self.tilt_neutral = 90.0
        
        self.target_pan_angle = self.pan_neutral   # Target Pan angle in degrees (0 to 180)
        self.target_tilt_angle = self.tilt_neutral # Target Tilt angle in degrees (0 to 180)
        
        # Smoothed active angles (EMA outputs)
        self.pan_angle = self.pan_neutral
        self.tilt_angle = self.tilt_neutral
        
        # Slew-Rate / Velocity Limit Settings to prevent brownouts
        self.last_state_update_time = time.time()
        self.max_speed_dps = 75.0                 # Maximum pan/tilt speed in degrees per second
        
        # Proportional tracking coefficients (gains)
        self.Kp_pan = 0.05
        self.Kp_tilt = 0.04
        
        # Firing alignment tolerances (deadzones in fraction of frame size)
        self.deadzone_ratio_x = 0.09 # Pan deadzone (9% of width)
        self.deadzone_ratio_y = 0.09 # Tilt deadzone (9% of height)
        
        # EMA smoothing factor (0.0 to 1.0; lower = smoother, higher = faster/noisier)
        self.smoothing_factor = 0.35
        
        # Hardware actuator states
        self.pump_is_on = False
        
        # Automated firing burst control and 3-second cooldown variables
        self.is_firing_burst = False
        self.last_fire_time = 0.0
        self.cooldown_until = 0.0
        
        # State tracking variables for redundant packet filtering
        self.last_sent_state = None
        self.last_sent_time = 0.0
        
        # Calibrated absolute limits & home positions (loaded/saved via settings.json)
        self.pan_position = 90           # Current pan position in degrees
        self.pan_limit_ccw = 10.0        # Physical CCW limit (angle in degrees)
        self.pan_limit_cw = 170.0        # Physical CW limit (angle in degrees)
        
        self.tilt_limit_min = 10.0       # Physical minimum tilt angle (default 10)
        self.tilt_limit_max = 170.0      # Physical maximum tilt angle (default 170)
        
        # Idle Return-to-Home Timer
        self.last_detection_time = time.time()
        self.return_to_home_disabled = True # Suspends return-to-home on boot/manual nudge until first detection
        self.home_pan_angle = 90.0       # Target Pan angle for home position
        self.home_tilt_angle = 90.0      # Target Tilt angle for home position
        
        # Idle Scanning State Variables
        self.is_scanning = False
        self.abort_scan = False
        self.last_scan_time = time.time()

    def set_pump(self, turn_on, target_cls=None, confidence=None):
        """Toggles the local state of the water pump IRLZ44N MOSFET, printing state changes to console."""
        if self.pump_is_on == turn_on:
            return
            
        self.pump_is_on = turn_on
        if turn_on:
            target_info = ""
            if target_cls is not None and confidence is not None:
                # ANSI Grey color: \033[90m, Reset: \033[0m
                target_info = f" \033[90m(target: {target_cls}, conf: {confidence:.2f})\033[0m"
            # ANSI Red color: \033[91m, Reset: \033[0m
            print(f"{config.ts()} [WEAPON] WATER PUMP: \033[91mFIRING\033[0m{target_info}", flush=True)
        else:
            # ANSI Green color: \033[92m, Reset: \033[0m
            print(f"{config.ts()} [WEAPON] WATER PUMP: \033[92mOFF\033[0m", flush=True)

    def send_udp(self, pan, tilt, pump, flash):
        """Sends UDP packet to ESP32 only on state changes or watchdog heartbeat (~800ms)."""
        if not config.ESP_IP:
            return
        current_state = (int(pan), int(tilt), 1 if pump else 0, 1 if flash else 0)
        current_time = time.time()
        
        if (current_state != self.last_sent_state) or (current_time - self.last_sent_time > 0.8):
            packet_str = f"{current_state[0]},{current_state[1]},{current_state[2]},{current_state[3]}"
            try:
                config.udp_socket.sendto(packet_str.encode(), (config.ESP_IP, 5005))
                self.last_sent_state = current_state
                self.last_sent_time = current_time
            except Exception as e:
                # Silently catch socket anomalies to prevent AI thread termination
                pass

    def send_state_immediate(self, pan_val=None):
        """For manual override or immediate adjustments bypassing slew limit."""
        if pan_val is not None:
            self.target_pan_angle = float(pan_val)
        self.pan_angle = self.target_pan_angle
        self.tilt_angle = self.target_tilt_angle
        self.pan_position = int(self.pan_angle)
        self.send_udp(self.pan_angle, self.tilt_angle, self.pump_is_on, False)

    def send_state(self):
        """Broadcasts the current smoothed and speed-limited state to the ESP32 (throttled)."""
        current_time = time.time()
        dt = current_time - self.last_state_update_time
        self.last_state_update_time = current_time
        
        # Clamp dt to prevent massive jumps if thread lags
        if dt > 0.2:
            dt = 0.1
            
        # Calculate maximum allowed angle changes for this time step based on speed limit (75 dps)
        max_change = self.max_speed_dps * dt
        
        # Slew-rate limit Pan axis
        pan_diff = self.target_pan_angle - self.pan_angle
        if abs(pan_diff) > max_change:
            next_pan = self.pan_angle + (max_change if pan_diff > 0 else -max_change)
        else:
            next_pan = self.target_pan_angle
            
        # Slew-rate limit Tilt axis
        tilt_diff = self.target_tilt_angle - self.tilt_angle
        if abs(tilt_diff) > max_change:
            next_tilt = self.tilt_angle + (max_change if tilt_diff > 0 else -max_change)
        else:
            next_tilt = self.target_tilt_angle
            
        # Apply Exponential Moving Average (EMA) smoothing to pan & tilt coordinates
        self.pan_angle = (self.smoothing_factor * next_pan) + ((1.0 - self.smoothing_factor) * self.pan_angle)
        self.tilt_angle = (self.smoothing_factor * next_tilt) + ((1.0 - self.smoothing_factor) * self.tilt_angle)
        
        self.pan_position = int(self.pan_angle)
        self.send_udp(self.pan_angle, self.tilt_angle, self.pump_is_on, False)

    def start_idle_scan(self):
        """Sweeps the camera slowly between limits in a background thread when no targets are detected."""
        if self.is_scanning or not config.enable_scanning_mode:
            return
            
        self.is_scanning = True
        self.abort_scan = False
        self.last_scan_time = time.time()
        
        # Cache start position to return to if scan finishes without detection
        start_pan = self.target_pan_angle
        
        # Retrieve calibrated limit bounds (fallback to default 10-170 range)
        min_limit = self.pan_limit_ccw if self.pan_limit_ccw is not None else 10.0
        max_limit = self.pan_limit_cw if self.pan_limit_cw is not None else 170.0
        
        print(f"{config.ts()} [Scan] Starting idle scan sweep. Sweeping {min_limit}° to {max_limit}° (neutral return: {start_pan}°)", flush=True)
        
        def sleep_and_check(secs):
            """Sleeps in small steps, aborting instantly if target is detected or scan disabled."""
            for _ in range(int(secs * 10)):
                if self.abort_scan or not config.enable_scanning_mode:
                    self.abort_scan = True
                    return True
                time.sleep(0.1)
            return False
            
        def get_steps(from_ang, to_ang, step=5.0):
            """Generates degrees step sequence for scanning sweeps."""
            if from_ang < to_ang:
                curr = from_ang
                while curr <= to_ang:
                    yield curr
                    curr += step
            else:
                curr = from_ang
                while curr >= to_ang:
                    yield curr
                    curr -= step

        try:
            # 1. Scan from start position to max_limit
            if not self.abort_scan:
                for angle in get_steps(start_pan, max_limit):
                    if self.abort_scan:
                        break
                    self.target_pan_angle = float(angle)
                    self.send_state()
                    if sleep_and_check(1.0):
                        break
                        
            # 2. Scan from max_limit to min_limit
            if not self.abort_scan:
                for angle in get_steps(max_limit, min_limit):
                    if self.abort_scan:
                        break
                    self.target_pan_angle = float(angle)
                    self.send_state()
                    if sleep_and_check(1.0):
                        break
                        
            # 3. Scan from min_limit back to start_pan
            if not self.abort_scan:
                for angle in get_steps(min_limit, start_pan):
                    if self.abort_scan:
                        break
                    self.target_pan_angle = float(angle)
                    self.send_state()
                    if sleep_and_check(1.0):
                        break
            
            # Finish up
            if self.abort_scan:
                print(f"{config.ts()} [Scan] Idle scan aborted (target detected or manual input).", flush=True)
            else:
                self.target_pan_angle = start_pan
                self.send_state()
                print(f"{config.ts()} [Scan] Idle scan completed successfully.", flush=True)
                
        finally:
            self.is_scanning = False
            self.abort_scan = False
            self.last_scan_time = time.time()

    def update_tracking(self, center_x, center_y, frame_w, frame_h, target_cls=None, confidence=None):
        """
        Computes tracking errors and performs a step-wise 'twitch pulse' for horizontal pan control.
        - Triggers the pump if target sits in the central deadzone.
        - Pulses the continuous pan servo with a microsecond nudge for a short duration, then stops it.
        - Calculates the absolute target angle for the standard 180 Tilt servo.
        """
        frame_cx = frame_w // 2
        frame_cy = frame_h // 2
        
        # Calculate horizontal and vertical offset error vectors
        error_x = center_x - frame_cx
        error_y = center_y - frame_cy
        
        deadzone_x = int(frame_w * self.deadzone_ratio_x)
        deadzone_y = int(frame_h * self.deadzone_ratio_y)
        
        # --- THE FIRING LOGIC ---
        target_locked = (abs(error_x) <= deadzone_x) and (abs(error_y) <= deadzone_y)
        
        now = time.time()
        if target_locked:
            if self.is_firing_burst:
                # We are currently in the middle of a 0.3s burst
                if now - self.last_fire_time >= 0.3:
                    # Burst completed! Shut off pump and initiate rest/cooldown
                    self.set_pump(False)
                    self.is_firing_burst = False
                else:
                    # Continue firing
                    self.set_pump(True, target_cls, confidence)
            else:
                # We are not firing. Check if we are in the 3.0s cooldown rest period
                if now >= self.cooldown_until:
                    # Cooldown expired! Start a new 0.3s burst
                    self.is_firing_burst = True
                    self.last_fire_time = now
                    self.cooldown_until = now + 3.3  # 0.3s burst + 3.0s rest cooldown
                    self.set_pump(True, target_cls, confidence)
                else:
                    # Resting/cooldown
                    self.set_pump(False)
        else:
            # Not locked: interrupt any active burst immediately and ensure pump is off
            if self.is_firing_burst:
                self.is_firing_burst = False
            self.set_pump(False)
        
        # --- TILT MOVEMENT LOGIC ---
        if abs(error_y) > deadzone_y:
            # Standard Tilt servo uses absolute angles. We increment/decrement towards the target.
            step_y = error_y * self.Kp_tilt
            
            # Apply step deadband: ignore tiny steps (< 0.05 degree) to prevent limit-cycle bouncing
            if abs(step_y) >= 0.05:
                # Calculate tilt inversion sign
                tilt_sign = -1 if config.invert_tilt else 1
                # Invert tilt direction if the turret is physically mounted upside down or inverted tilt is active
                new_tilt = self.target_tilt_angle + step_y * tilt_sign if config.is_flipped else self.target_tilt_angle - step_y * tilt_sign
                # Apply boundaries!
                self.target_tilt_angle = max(self.tilt_limit_min, min(self.tilt_limit_max, new_tilt))

        # --- PAN MOVEMENT LOGIC ---
        if abs(error_x) > deadzone_x:
            # Standard Pan servo uses absolute angles. We increment/decrement towards the target.
            step_x = error_x * self.Kp_pan
            
            # Apply step deadband: ignore tiny steps (< 0.05 degree) to prevent limit-cycle bouncing
            if abs(step_x) >= 0.05:
                # Calculate pan inversion sign
                pan_sign = -1 if config.invert_pan else 1
                # Invert pan direction based on flip state and pan inversion setting
                new_pan = self.target_pan_angle + step_x * pan_sign if config.is_flipped else self.target_pan_angle - step_x * pan_sign
                # Apply boundaries!
                self.target_pan_angle = max(self.pan_limit_ccw if self.pan_limit_ccw is not None else 10.0, 
                                            min(self.pan_limit_cw if self.pan_limit_cw is not None else 170.0, new_pan))
