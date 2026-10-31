# SafeGuard AI — independent camera safety monitoring (2026 project update)

**Non-certified educational robotics prototype.** Nothing in this project can establish that a worker is injured or physically stop industrial machinery. It must **not** replace certified machine guarding, emergency stops, trained spotters, or occupational safety procedures.

## What changed in your uploaded directory

| Component | What the updated code does |
|---|---|
| `esp32/esp32.ino` | Independent HC-SR04 + buzzer + animated SH1106 eye; `/status`, plus optional *advisory* `POST /vision` feedback received from Python (OLED and distinct check pattern). No real machine stopping. |
| `esp32_cam/esp32_cam.ino` + `camera_dashboard.h` | Keep working RGB565-to-JPEG camera mode, `/capture`, `/status`, `/flash` and port-81 `/stream`. Includes mobile-friendly camera dashboard. |
| `vision_monitor.py` **NEW** | Independently polls camera (even without ultrasonic activation); detects motion; optional MediaPipe pose transition / hands in user-drawn image ROI; flags possible prolonged stillness **only if person recently tracked or supervisor confirms someone should be present**. |
| `m.py` | Existing Telegram, Gemini model fallback, supervisor video, alerts, Flask API; adds separate vision observer thread + event alerts + advisory feedback to ESP32. |
| `assets/backend_dashboard.html` | New responsive, light command center: annotated camera view, drag-to-draw ROI, worker expected setting, sensitivity and stillness controls, event timeline, device/Telegram/flash controls. |
| `.env.example` | New safe config template. Real secrets removed from shared ZIP. |
| `tests/test_vision.py` | Offline frame/state-machine tests for no-person behavior, stillness, motion, simulated fall and hands. |
| `requirements_vision_optional.txt` | Optional pose/hand detection install; base motion detection works without MediaPipe. |

The old duplicated root-level `esp32_cam.ino` was intentionally removed: **open only `esp32_cam/esp32_cam.ino` with `camera_dashboard.h` beside it.**

## 1. FIRST: Wiring and flashing

See the [complete physical wiring guide](WIRING.md) for every power/signal/ground connection, the ECHO divider, buzzer compatibility and camera programming wiring.

### ESP32 DevKit

- Board **ESP32 Dev Module**, Arduino-ESP32 **3.x**. Install Adafruit SH110X and Adafruit GFX libraries.
- OLED SDA GPIO21, SCL GPIO22, VCC 3V3 and GND GND; HC-SR04 TRIG GPIO5, ECHO GPIO18 **through a proper 5V-to-3.3V voltage divider**, VCC 5V, GND GND.
- Buzzer GPIO25. Code defaults to a **passive** buzzer. If yours is an active module set `PASSIVE_BUZZER=false` (pitch cannot be controlled). Power and driver must match the buzzer rating; never place 5V directly on GPIO.
- Edit `WIFI_PASSWORD` locally. Ensure hotspot is 2.4 GHz; if the IP is wrong disable `STATIC_IP` (use the IP printed by Serial Monitor).
- ESP32 camera observations use `POST /vision` over local Wi-Fi; missing Python/vision will show `CV OFFLINE` and **will never disable local ultrasonic alarms**.

### AI Thinker ESP32-CAM

- Open **`esp32_cam/esp32_cam.ino`**, together with `camera_dashboard.h` in its same folder.
- Select **AI Thinker ESP32-CAM**, Arduino-ESP32 3.x, PSRAM enabled, Serial Monitor 115200. Set same local Wi-Fi password.
- After flashing remove GPIO0-to-GND jumper if used, then reset the board. Use a stable 5V camera supply and **3.3V UART logic**.
- This actual camera previously reported `0x2145` and rejected JPEG (`0x106`). The firmware **uses RGB565 at 160×120 and software JPEG encoding**. Pose accuracy and MJPEG FPS are limited; replacing camera hardware with a native-JPEG OV2640 camera is the best hardware upgrade.
- Camera local page: `http://CAMERA_IP/`; photo `http://CAMERA_IP/capture`; status `http://CAMERA_IP/status`; MJPEG `http://CAMERA_IP:81/stream`. Stream is intended for **one client at a time**. Flash LED can overheat; avoid unattended/high brightness use.

## 2. Laptop Python setup (Windows)

Open Command Prompt **inside the repository root**:

```bat
py -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env`, fill in **new** Telegram bot token, Gemini API key, authorized Telegram numeric user IDs, and device IPs from Serial Monitor. Never share your filled `.env` or ZIP containing credentials. Old tokens, keys and hotspot password were exposed in prior chats; rotate them.

To enable actual **pose and hand landmarks** (optional; recommended Python **3.11 or 3.12**):

```bat
py -m pip install -r requirements_vision_optional.txt
```

MediaPipe installation depends on your operating system and Python version. If unavailable, the backend automatically degrades to **scene motion only**; it **will not claim falls or hands are detected**. You can run the CV-only monitoring demo with `WORKER_EXPECTED=true` after you visually verify a worker is in camera view. That flag does NOT detect a person.

Then:

```bat
py verify_devices.py
py m.py
```

Alternatively double-click `RUN_BACKEND.bat` (still needs Python and packages; first run can install packages).

**Open the redesigned vision dashboard:** `http://127.0.0.1:8765/` on the laptop. Camera page remains on its local IP. Python must remain running for camera vision and Telegram alerts.

## 3. Important vision controls

- **Visual observer:** runs completely independently from sensor triggering. Camera is polled roughly every `VISION_INTERVAL` seconds (default 2.5), in addition to sensor telemetry.
- **Worker expected in view:** default **OFF**. Enable only after a person is verified to be in the camera view. This allows stillness warnings even if low-res pose detection fails. A static *empty room* without this confirmation will not trigger a motionless-worker alert.
- **No-motion threshold:** default 16 seconds below a changed-pixel threshold. This is scene movement, NOT verified body motion. Shadows, camera vibration, clothing, poor lighting, flash changes and low FPS affect readings.
- **Possible fall:** with optional MediaPipe installed and person joints visible, a transition from upright torso to near-horizontal torso must appear in consecutive frames. It does **not** identify actual injury and may miss falls away from the camera.
- **Hand hazard zone:** use the dashboard's **Draw danger zone** button and drag a rectangle over the **processed camera image**. Hand landmark/wrist detections inside this region require consecutive frames. The drawn zone is in **image coordinates**, not calibrated centimeters or a safety-rated physical boundary.
- **Unusual stillness / distress:** unconfirmed prolonged scene stillness generates **"Check worker"** message. "Appears distressed" or "injured" cannot be diagnosed from a low-res image or a single Gemini analysis.
- **No camera visibility / darkness / worker behind object:** detection is not reliable. If the camera cannot be reached, backend reports `CAMERA UNAVAILABLE`.

Camera event alert -> Python -> Telegram alert + event photo (if permitted/configured). Optional Gemini on these CV events is OFF by default (`VISION_GEMINI_ON_ALERT=false`) to avoid quota/latency problems. Sensor critical alerts retain existing photo+optional Gemini behavior. Camera event advisory also goes over local Wi-Fi to ESP32 OLED/short buzzer alert without changing ultrasonic monitoring.

## 4. Telegram supervisor commands (private, allow-listed numeric IDs)

| Command | Outcome |
|---|---|
| `/start` or `/menu` | Buttons and instructions |
| `/ping`, `/test` | Check bot / device communication |
| `/status` | Ultrasonic + camera + independent camera CV summary |
| `/vision status` | Current motion/pose backend/state/stillness |
| `/vision on`, `/vision off` | Toggle independent visual observer |
| `/worker on`, `/worker off` | Confirm or remove supervisor-expectation of worker in view |
| `/still 20` | 20-second low-motion observation threshold (8–120) |
| `/zone 0.55 0.15 0.95 0.85` | Set normalized (x1 y1 x2 y2) image ROI |
| `/photo` | Fresh camera snapshot |
| `/record 8` | Capture approximately 8s (3–20s allowed); MP4 with no audio, quality limited by sensor FPS |
| `/flash on`, `/flash off`, `/flash auto` | Camera LED control |
| `/brightness 70` | LED level (0–180) |
| `/analyze` | Gemini single-image interpretation (may be wrong) |
| `/models` | Gemini model status |
| `/alerts on`, `/alerts off` | Remote notifications only, not buzzer |
| `/live` | Local network camera URL; remote internet live video **not** provided without a secured HTTPS relay |

**Only one Python process can poll the Telegram bot at once.** Each recipient must message the bot once. If no response check Telegram token, numeric IDs and polling `409` in terminal. Do not expose the backend publicly; it is bound to 127.0.0.1 by default.

## 5. Offline tests and validation

```bat
py -m unittest discover -s tests -v
```

The offline tests generate fake camera images/pose landmarks and test application logic. They do **not** establish accuracy on real injured workers and they do not prove that Arduino code compiles under your installed package. On-device commissioning checklist:

1. Verify DevKit `GET /status` (live changing distance, buzzer, OLED eye).
2. Verify ESP32-CAM `/status`, `/capture` valid JPEG, and **single-client** `:81/stream`.
3. Run `verify_devices.py`; ensure both network interfaces reachable, then `m.py`.
4. Backend `/api/health` should contain `vision` with frame count increasing and recent `last_frame_age`.
5. In dashboard, simulate empty camera scene with worker expected **OFF**. Wait >16 sec: **no "injury" or "stillness" alert**.
6. Place volunteer visibly in camera; mark **Worker expected** ON; have them stay still for 16–20 sec. Expect an **unverified check warning**, not injury claim. Move to reset inactivity period.
7. If MediaPipe works, test a safe acted posture change (no real falls!), and fingertip entry into marked image ROI. Observe that pose may not resolve at 160×120.
8. In Telegram: `/test`, `/vision status`, `/photo`, `/record 8`, `/alerts on`.
9. Test that camera power unplug is shown as unavailable, rather than silently marking everything safe.

## 6. API compatibility

- ESP32: `GET /status`, `POST /vision` form field `state` in `NORMAL, MOTION, STILL, FALL, HAND, CAM_OFFLINE, PAUSED`.
- ESP32-CAM: `GET /status`, `GET /capture?fresh=1`, `GET /flash?mode=off|on|auto&brightness=70`, `GET http://CAMERA:81/stream`.
- Laptop: `GET /api/health`, `GET /api/photo`, `GET /api/vision/status`, `GET /api/vision/frame`, `POST /api/vision/config`, `POST /api/control/flash`, `/api/control/alerts`, `/api/control/record`.
- Critical ultrasonic path is unchanged. The camera observer is an independent polling process. Nothing uploads video to the ESP32-CAM SD card.

## Critical limitations

This is not medical, fall-proof, injury-aware, or industrial-safety-certified. Visual confidence cannot be meaningfully quantified without a labeled evaluation dataset. Sensor and camera outages, occlusion, network failures, Telegram delays and API quotas can cause missed alerts. **Maintain independent real-world safety controls.**
