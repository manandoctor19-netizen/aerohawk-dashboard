#!/usr/bin/env python3
"""
AeroHawk manual control GUI (Tkinter, any Linux desktop).

Steering AND drive are wired to the real hardware through Angel's hardware.py
(SteeringServo + MotorIBT2 on the PCA9685). Settings come from drive_params.yaml.

!! Don't run this while drive_node / robot.launch.py is running: both would
!! talk to the same PCA9685 and fight over the motor. Stop the ROS stack first.

Run ON THE JETSON (it needs the I2C bus). From your PC:
    ssh -X robotics@<jetson-ip>
    python3 GUI.py

    sudo apt install python3-tk      # once, if Tkinter is missing

If hardware.py or the PCA9685 can't be reached, the GUI still opens in
"DRY RUN" mode and just prints what it would do.

Keyboard shortcuts (click the window first so it has focus):
    A / D       steer left / right (step)
    S           center steering
    W / X       hold for forward / reverse (release = stop)
    Space       stop motor
    Esc         emergency stop (instant, no ramp)
Safety: the motor also stops if the window loses focus or no drive key/button is held.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

STEER_STEP = 0.1     # how far one A/D press or Left/Right click moves the steering
DRIVE_SPEED = 0.5    # hold-to-drive speed, 0.0-1.0 of the capped power (max_duty in the YAML)
UPDATE_MS = 20       # MotorIBT2.update() must run ~20+ times a second for the ramp
RELEASE_GRACE_MS = 80  # Linux key auto-repeat sends release+press pairs; ignore those

# ==========================================================================
# Find and load hardware.py
# ==========================================================================
# The folder that CONTAINS hardware.py. Both spellings are tried, since the
# workspace may or may not have the usual src/ folder.
HARDWARE_DIRS = [
    "~/ros2_ws/Aerohawk/aerohawk_drive/aerohawk_drive",
    "~/ros2_ws/src/Aerohawk/aerohawk_drive/aerohawk_drive",
]

for _d in HARDWARE_DIRS:
    _d = os.path.expanduser(_d)
    if os.path.isfile(os.path.join(_d, "hardware.py")):
        sys.path.insert(0, _d)
        break

board = steering = motor = None
hardware_error = None

try:
    from hardware import build_hardware, load_settings, DEFAULT_SETTINGS_PATH

    settings = load_settings(DEFAULT_SETTINGS_PATH)
    board, steering, motor = build_hardware(settings)

    # The servo must get 50 Hz. If the YAML was left at a motor-test
    # frequency (e.g. 1500), refuse to drive the servo instead of hurting it.
    if abs(board.frequency - 50.0) > 10.0:
        hardware_error = (f"PCA9685 is at {board.frequency:.0f} Hz - set pwm_frequency: 50.0 "
                          f"in drive_params.yaml before using the servo")
        board.close()
        board = steering = motor = None
except Exception as exc:  # missing file, no I2C permission, board not found, ...
    hardware_error = f"{type(exc).__name__}: {exc}"
    board = steering = motor = None

HARDWARE_OK = steering is not None and motor is not None


# ==========================================================================
# Control functions - steering and drive both go to the real hardware
# ==========================================================================
def set_steering(value):
    """value: -1.0 (full left) .. 0.0 (center) .. +1.0 (full right)"""
    if HARDWARE_OK:
        steering.set_steering(value)
    else:
        print(f"[dry run] set_steering({value:+.2f})")


def center_steering():
    if HARDWARE_OK:
        steering.center()
    else:
        print("[dry run] center_steering()")


def drive_forward(speed=DRIVE_SPEED):
    """Ramp up to forward speed. The ramp happens in motor_tick()."""
    if HARDWARE_OK:
        motor.set_speed(abs(speed))
    else:
        print(f"[dry run] drive_forward({speed:.2f})")


def drive_reverse(speed=DRIVE_SPEED):
    """Ramp to reverse. MotorIBT2 itself passes through zero and pauses first."""
    if HARDWARE_OK:
        motor.set_speed(-abs(speed))
    else:
        print(f"[dry run] drive_reverse({speed:.2f})")


def stop_motor():
    """Normal stop: ramp down to zero (smooth, protects the gearbox)."""
    if HARDWARE_OK:
        motor.set_speed(0.0)
    else:
        print("[dry run] stop_motor()")


def motor_tick():
    """Advance the motor ramp one step. Called every UPDATE_MS by the GUI."""
    if HARDWARE_OK:
        motor.update()


def motor_speed():
    return motor.speed if HARDWARE_OK else 0.0


def emergency_stop():
    """Instant stop: PWM off and IBT-2 disabled (no ramp), steering centered."""
    if HARDWARE_OK:
        motor.stop()
        steering.center()
    else:
        print("[dry run] emergency_stop()")


def on_close():
    """Window closing: stop the motor and turn every PCA9685 output off."""
    if motor is not None:
        motor.stop()
    if board is not None:
        board.close()
        print("all outputs off")


# ==========================================================================
# GUI
# ==========================================================================
class ControlApp:
    def __init__(self, root):
        self.root = root
        self.steering = 0.0
        self._driving = None      # "forward", "reverse", or None
        self._release_job = None  # pending stop after a key release

        root.title("AeroHawk Manual Control")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        pad = {"padx": 8, "pady": 6}

        # ---- Hardware status banner ----
        if HARDWARE_OK:
            banner_text = f"Hardware connected - PCA9685 at {board.frequency:.0f} Hz"
            banner_color = "#2e7d32"
        else:
            banner_text = f"DRY RUN - {hardware_error}"
            banner_color = "#ef6c00"
        tk.Label(root, text=banner_text, bg=banner_color, fg="white",
                 wraplength=360, justify="left", padx=6, pady=4).grid(
            row=0, column=0, sticky="ew", **pad)

        # ---- Steering ----
        steer = ttk.LabelFrame(root, text="Steering")
        steer.grid(row=1, column=0, sticky="ew", **pad)

        self.steer_var = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(steer, from_=-1.0, to=1.0, orient="horizontal",
                                length=320, variable=self.steer_var,
                                command=self._slider_moved)
        self.slider.grid(row=0, column=0, columnspan=3, **pad)

        self.steer_label = ttk.Label(steer, text="Steering: +0.00 (center)")
        self.steer_label.grid(row=1, column=0, columnspan=3)

        ttk.Button(steer, text="◀ Left (A)", command=self.steer_left).grid(row=2, column=0, **pad)
        ttk.Button(steer, text="Center (S)", command=self.steer_center).grid(row=2, column=1, **pad)
        ttk.Button(steer, text="Right (D) ▶", command=self.steer_right).grid(row=2, column=2, **pad)

        # ---- Drive (hold-to-drive: release = stop) ----
        drive = ttk.LabelFrame(root, text="Drive (hold button)")
        drive.grid(row=2, column=0, sticky="ew", **pad)

        fwd = ttk.Button(drive, text="▲ Forward (W)")
        rev = ttk.Button(drive, text="▼ Reverse (X)")
        stop = ttk.Button(drive, text="Stop (Space)", command=self.stop)
        fwd.grid(row=0, column=0, **pad)
        stop.grid(row=0, column=1, **pad)
        rev.grid(row=0, column=2, **pad)
        fwd.bind("<ButtonPress-1>", lambda e: self.start_drive("forward"))
        fwd.bind("<ButtonRelease-1>", lambda e: self.stop())
        rev.bind("<ButtonPress-1>", lambda e: self.start_drive("reverse"))
        rev.bind("<ButtonRelease-1>", lambda e: self.stop())

        self.speed_var = tk.DoubleVar(value=DRIVE_SPEED)
        ttk.Label(drive, text="Speed").grid(row=1, column=0, sticky="e")
        ttk.Scale(drive, from_=0.1, to=1.0, orient="horizontal", length=200,
                  variable=self.speed_var).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

        # ---- Emergency stop ----
        tk.Button(root, text="EMERGENCY STOP (Esc)", bg="#c62828", fg="white",
                  activebackground="#8e0000", activeforeground="white",
                  font=("TkDefaultFont", 14, "bold"), height=2,
                  command=self.estop).grid(row=3, column=0, sticky="ew", **pad)

        # ---- Status ----
        self.status = ttk.Label(root, text="Ready", anchor="w")
        self.status.grid(row=4, column=0, sticky="ew", **pad)

        # ---- Keyboard ----
        root.bind("<KeyPress-a>", lambda e: self.steer_left())
        root.bind("<KeyPress-d>", lambda e: self.steer_right())
        root.bind("<KeyPress-s>", lambda e: self.steer_center())
        root.bind("<KeyPress-w>", lambda e: self.start_drive("forward"))
        root.bind("<KeyRelease-w>", lambda e: self._key_released())
        root.bind("<KeyPress-x>", lambda e: self.start_drive("reverse"))
        root.bind("<KeyRelease-x>", lambda e: self._key_released())
        root.bind("<space>", lambda e: self.stop())
        root.bind("<Escape>", lambda e: self.estop())
        root.bind("<FocusOut>", lambda e: self.stop())   # clicked away = stop driving

        # Start with the wheels straight, then keep the motor ramp running
        self.steer_center()
        self._tick()

    # ---- motor ramp loop ----
    def _tick(self):
        self._safe(motor_tick)
        if self._driving:
            self.status.config(text=f"Driving {self._driving}  ·  output {motor_speed():+.2f}")
        self.root.after(UPDATE_MS, self._tick)

    # ---- steering helpers ----
    def _show_steering(self, value):
        side = "center" if value == 0 else ("left" if value < 0 else "right")
        self.steer_label.config(text=f"Steering: {value:+.2f} ({side})")

    def _apply_steering(self, value):
        value = max(-1.0, min(1.0, round(value, 2)))
        self.steering = value
        self.steer_var.set(value)
        self._show_steering(value)
        self._safe(set_steering, value)

    def _slider_moved(self, _):
        value = round(self.steer_var.get(), 2)
        if value != self.steering:
            self._apply_steering(value)

    def steer_left(self):
        self._apply_steering(self.steering - STEER_STEP)

    def steer_right(self):
        self._apply_steering(self.steering + STEER_STEP)

    def steer_center(self):
        self.steering = 0.0
        self.steer_var.set(0.0)
        self._show_steering(0.0)
        self._safe(center_steering)

    # ---- drive helpers ----
    def start_drive(self, direction):
        if self._release_job:            # key auto-repeat: cancel the pending stop
            self.root.after_cancel(self._release_job)
            self._release_job = None
        if self._driving == direction:   # ignore key auto-repeat
            return
        self._driving = direction
        speed = round(self.speed_var.get(), 2)
        self.status.config(text=f"Driving {direction}")
        self._safe(drive_forward if direction == "forward" else drive_reverse, speed)

    def _key_released(self):
        if self._release_job:
            self.root.after_cancel(self._release_job)
        self._release_job = self.root.after(RELEASE_GRACE_MS, self.stop)

    def stop(self):
        self._release_job = None
        if self._driving is None:
            return
        self._driving = None
        self.status.config(text="Stopped")
        self._safe(stop_motor)

    def estop(self):
        self._driving = None
        self.steering = 0.0
        self.steer_var.set(0.0)
        self._show_steering(0.0)
        self.status.config(text="EMERGENCY STOP")
        self._safe(emergency_stop)

    # ---- error handling: an I2C hiccup shows in the status bar, not a crash ----
    def _safe(self, func, *args):
        try:
            func(*args)
        except Exception as exc:
            self.status.config(text=f"Error: {type(exc).__name__}: {exc}")
            print(f"error in {func.__name__}: {exc}")

    def quit(self):
        try:
            on_close()
        finally:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ControlApp(root)
    root.mainloop()
