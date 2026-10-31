
"""SafeGuard AI: ESP32 + ESP32-CAM + Gemini + Telegram.
Python 3.10+. Educational monitoring prototype only.
Not a certified industrial machine safety system.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from vision_monitor import VisionMonitor, VisionSettings

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("safeguard")


def valid_secret(s):
    return bool(s and s.strip()) and not s.strip().upper().startswith(
        ("YOUR_", "REPLACE_", "ENTER_")
    )


def envflag(name, default=False):
    return os.getenv(name, str(default)).lower().strip() in (
        "1", "true", "yes", "on"
    )


ESP32 = os.getenv(
    "ESP32_URL", "http://10.221.145.179"
).rstrip("/")

CAMERA = os.getenv(
    "CAMERA_URL", "http://10.221.145.74"
).rstrip("/")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

RAW_MODELS = (
    os.getenv("GEMINI_MODELS", "").strip()
    or os.getenv("GEMINI_MODEL", "auto").strip()
)

ALLOWED = {
    int(s.strip())
    for s in os.getenv("TELEGRAM_USER_IDS", "").split(",")
    if s.strip().isdigit()
}

HOST = os.getenv("BACKEND_HOST", "127.0.0.1")
PORT = int(os.getenv("BACKEND_PORT", "8765"))

MONITOR_INTERVAL = max(
    0.7, float(os.getenv("MONITOR_INTERVAL", "1.2"))
)
COOLDOWN = max(
    10, int(os.getenv("COOLDOWN_SECONDS", "35"))
)
AUTO_RECORD_SECONDS = max(
    0, min(20, int(os.getenv("AUTO_RECORD_SECONDS", "0")))
)

# Camera analysis is independent of ultrasonic sensor alerts.
VISION_INTERVAL = max(1.3, float(os.getenv("VISION_INTERVAL", "2.5")))
VISION_GEMINI_ON_ALERT = envflag("VISION_GEMINI_ON_ALERT", False)
VISION_MONITOR = VisionMonitor(settings=VisionSettings(
    enabled=envflag("VISION_ENABLED", True),
    worker_expected=envflag("WORKER_EXPECTED", False),
    still_seconds=max(8, float(os.getenv("STILL_SECONDS", "16"))),
    min_motion_percent=float(os.getenv("MIN_MOTION_PERCENT", "1.8"))
))

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{TOKEN}"
    if valid_secret(TOKEN) else ""
)


# ===========================================
# GEMINI MODEL PARSING
# ===========================================

def parse_models(raw):
    """
    Converts a comma-separated .env value into
    individual model names.

    NEVER sends the entire CSV string as one model.
    """
    if not raw or raw.strip().lower() == "auto":
        return []

    names = []

    for piece in re.split(r"[,;\n]+", raw):
        name = (
            piece.strip()
            .strip("\"'")
            .removeprefix("models/")
        )

        if name.lower() == "auto":
            continue

        valid_name = re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", name
        )

        if name and valid_name and name not in names:
            names.append(name)
        elif name:
            log.warning(
                "Ignoring invalid Gemini model name: %r",
                name
            )

    return names


MODELS = parse_models(RAW_MODELS)

STOP = threading.Event()

ALERTS = threading.Event()
ALERTS.set()

BOT_READY = threading.Event()

RECORD_LOCK = threading.Lock()
ALERT_LOCK = threading.Lock()
PHOTO_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()

STATUS = {
    "sensor": {},
    "camera": {},
    "sensor_error": "Waiting for ESP32",
    "camera_error": "Waiting for ESP32-CAM",
    "gemini_error": "",
    "telegram_state": "starting",
    "telegram_error": "",
    "telegram_last_success": "",
    "telegram_polling": False,
}


def safe_telegram_error(exc):
    """Return a useful error message without exposing API secrets."""
    text = str(exc)
    if TOKEN:
        text = text.replace(TOKEN, "<redacted-token>")
    if API_KEY:
        text = text.replace(API_KEY, "<redacted-key>")
    # Requests exceptions can print the full Bot API URL.
    text = re.sub(
        r"(https?://api\.telegram\.org/bot)[^/\s]+",
        r"\1<redacted-token>", text
    )
    return text[:280]


def update_telegram_status(state, error="", polling=False):
    with STATE_LOCK:
        STATUS["telegram_state"] = state
        STATUS["telegram_error"] = safe_telegram_error(error) if error else ""
        STATUS["telegram_polling"] = bool(polling)
        if state == "connected":
            STATUS["telegram_last_success"] = datetime.now().isoformat(timespec="seconds")


LAST_MODEL = None
LAST_ALERT = 0.0
PREVIOUS_DANGER = False
BOT_USERNAME = ""


# ===========================================
# HARDWARE COMMUNICATION
# ===========================================

def fetch_json(url, timeout=4):
    r = requests.get(
        url,
        timeout=timeout,
        headers={"Cache-Control": "no-store"}
    )

    r.raise_for_status()
    data = r.json()

    if not isinstance(data, dict):
        raise ValueError("Expected device JSON object")

    return data


def sensor_status():
    data = fetch_json(ESP32 + "/status", 3)

    if (
        data.get("device") != "SAFEGUARD_01"
        or not isinstance(data.get("sensor_ok"), bool)
    ):
        raise ValueError(
            "Wrong ESP32 response or incompatible firmware"
        )

    return data


def camera_status():
    data = fetch_json(CAMERA + "/status", 4)

    if data.get("device") != "SAFEGUARD_CAM_01":
        raise ValueError(
            "Wrong camera response or incompatible firmware"
        )

    return data


def camera_photo(fresh=True):
    with PHOTO_LOCK:
        r = requests.get(
            CAMERA + "/capture",
            params={"fresh": "1"} if fresh else None,
            timeout=15,
            headers={"Cache-Control": "no-store"}
        )

        r.raise_for_status()

        content_type = r.headers.get(
            "content-type", ""
        ).lower()

        if (
            "image/jpeg" not in content_type
            or not r.content.startswith(b"\xff\xd8")
        ):
            raise RuntimeError(
                "Camera returned no valid JPEG"
            )

        return r.content


def flash_control(mode=None, brightness=None):
    args = {}

    if mode is not None:
        if mode not in ("on", "off", "auto"):
            raise ValueError("Use on, off or auto")

        args["mode"] = mode

    if brightness is not None:
        brightness = int(brightness)

        if not 0 <= brightness <= 180:
            raise ValueError(
                "Brightness must be 0-180"
            )

        args["brightness"] = brightness

    url = (
        CAMERA + "/flash?"
        + requests.compat.urlencode(args)
    )

    return fetch_json(url, 6)


# ===========================================
# TELEGRAM API
# ===========================================

def tg(method, data=None, files=None, timeout=45):
    if not TELEGRAM_URL:
        raise RuntimeError(
            "Configure TELEGRAM_BOT_TOKEN"
        )

    r = requests.post(
        TELEGRAM_URL + "/" + method,
        data=data,
        files=files,
        timeout=timeout
    )

    try:
        body = r.json()
    except ValueError:
        body = {
            "description": r.text[:200]
        }

    if not r.ok or not body.get("ok"):
        raise RuntimeError(
            f"Telegram {method}: "
            f"{r.status_code} "
            f"{body.get('description', 'unknown error')}"
        )

    return body


MENU = {
    "inline_keyboard": [
        [
            {
                "text": "📊 Status",
                "callback_data": "status"
            },
            {
                "text": "📸 Photo",
                "callback_data": "photo"
            }
        ],
        [
            {
                "text": "🧪 Test",
                "callback_data": "test"
            },
            {
                "text": "🎥 8s video",
                "callback_data": "record:8"
            }
        ],
        [
            {
                "text": "💡 Flash ON",
                "callback_data": "flash:on"
            },
            {
                "text": "🌗 AUTO",
                "callback_data": "flash:auto"
            },
            {
                "text": "🌑 OFF",
                "callback_data": "flash:off"
            }
        ],
        [
            {
                "text": "👁 Vision", "callback_data": "vision:status"
            },
            {
                "text": "🏃 Worker expected", "callback_data": "worker:on"
            }
        ],
        [
            {
                "text": "🧠 Analyze",
                "callback_data": "analyze"
            },
            {
                "text": "🖥 Live URL",
                "callback_data": "live"
            }
        ],
        [
            {
                "text": "🔔 Alerts ON",
                "callback_data": "alerts:on"
            },
            {
                "text": "🔕 Alerts OFF",
                "callback_data": "alerts:off"
            }
        ]
    ]
}

HELP = (
    "🛡 SafeGuard AI Supervisor\n"
    "/status - sensor and camera\n"
    "/photo - camera snapshot\n"
    "/record 8 - 3 to 20 sec clip\n"
    "/flash on|off|auto - LED\n"
    "/brightness 70 - LED brightness\n"
    "/analyze - Gemini AI\n"
    "/models - Gemini configuration\n"
    "/vision status|on|off - independent camera monitoring\n"
    "/worker on|off - manually confirm worker expected\n"
    "/zone 0.55 0.15 0.95 0.85 - marked image region\n"
    "/still 16 - low-motion warning delay (8-120 s)\n"
    "/alerts on|off - notifications\n"
    "/live - camera dashboard\n"
    "/test - device connections\n"
    "/chatid - numeric Telegram ID\n"
    "/menu - buttons\n\n"
    "Prototype only. No actual machine stop."
)


def send_text(chat, body, menu=False):
    data = {
        "chat_id": chat,
        "text": str(body)[:4000]
    }

    if menu:
        data["reply_markup"] = json.dumps(MENU)

    tg("sendMessage", data)


def send_photo(
    chat,
    jpg,
    caption="SafeGuard AI snapshot"
):
    tg(
        "sendPhoto",
        {
            "chat_id": chat,
            "caption": caption[:900]
        },
        {
            "photo": (
                "snapshot.jpg",
                io.BytesIO(jpg),
                "image/jpeg"
            )
        },
        timeout=80
    )


def send_clip(chat, filename, caption):
    try:
        with open(filename, "rb") as fp:
            tg(
                "sendVideo",
                {
                    "chat_id": chat,
                    "caption": caption
                },
                {
                    "video": (
                        "safeguard.mp4",
                        fp,
                        "video/mp4"
                    )
                },
                timeout=140
            )

    except Exception as exc:
        log.warning(
            "Video preview failed: %s",
            str(exc)[:120]
        )

        with open(filename, "rb") as fp:
            tg(
                "sendDocument",
                {
                    "chat_id": chat,
                    "caption": caption
                },
                {
                    "document": (
                        "safeguard.mp4",
                        fp,
                        "video/mp4"
                    )
                },
                timeout=140
            )


# ===========================================
# GEMINI MODEL DISCOVERY
# ===========================================

def list_gemini_models(client):
    """
    Discovers models accessible to the API key.
    Only tries likely generateContent models.
    """
    available = []

    for model in client.models.list():
        name = str(
            getattr(model, "name", "") or ""
        ).split("/")[-1]

        actions = getattr(
            model, "supported_actions", None
        )

        if not name.startswith("gemini"):
            continue

        if any(
            x in name
            for x in ("embedding", "image", "tts")
        ):
            continue

        if actions:
            supported = any(
                "generatecontent" in str(action)
                .replace("_", "")
                .lower()
                for action in actions
            )

            if not supported:
                continue

        available.append(name)

    return sorted(
        set(available),
        key=lambda n: (
            0 if "flash" in n else 1,
            "preview" in n,
            n
        )
    )


def retry_with_next_model(exc):
    status = (
        getattr(exc, "code", None)
        or getattr(exc, "status_code", None)
    )

    text = str(exc).lower()

    if status is None:
        found = re.search(
            r"\b(400|401|403|404|429|500|502|503|504)\b",
            text
        )

        status = (
            int(found.group(1))
            if found else 0
        )

    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0

    if status == 400:
        return any(
            phrase in text
            for phrase in (
                "model",
                "not supported",
                "invalid argument"
            )
        )

    return status in (
        404, 429, 500, 502, 503, 504
    )


# ===========================================
# GEMINI VISION ANALYSIS
# ===========================================

def analyze(jpg, sensor):
    global LAST_MODEL

    if not valid_secret(API_KEY):
        raise RuntimeError(
            "GEMINI_API_KEY is not configured"
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=API_KEY)

    prompt = (
        "Describe this low-resolution SafeGuard AI "
        "demo camera image in under 90 words. "
        "Ultrasonic telemetry: "
        + json.dumps(sensor)
        + ". Mention visible people or objects "
        "and possible proximity risks. "
        "State uncertainty. Do not claim "
        "actual injury or actual machine stop."
    )

    errors = []

    try:
        order = list(
            dict.fromkeys(
                ([LAST_MODEL] if LAST_MODEL else [])
                + MODELS
            )
        )

        discovered = False

        if not order:
            order = list_gemini_models(client)
            discovered = True

        index = 0

        while index < len(order):
            model = order[index]
            index += 1

            try:
                log.info(
                    "Gemini trying %s", model
                )

                # ONE model per API call.
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(
                            data=jpg,
                            mime_type="image/jpeg"
                        )
                    ]
                )

                text = getattr(
                    resp, "text", None
                )

                if not text:
                    raise RuntimeError(
                        f"{model} returned no text"
                    )

                LAST_MODEL = model

                with STATE_LOCK:
                    STATUS["gemini_error"] = ""

                log.info(
                    "Gemini OK: %s", model
                )

                return text[:1600]

            except Exception as exc:
                errors.append(
                    f"{model}: {str(exc)[:120]}"
                )

                if not retry_with_next_model(exc):
                    raise

                log.warning(
                    "Model %s unavailable: %s",
                    model,
                    str(exc)[:140]
                )

                if LAST_MODEL == model:
                    LAST_MODEL = None

                if (
                    index >= len(order)
                    and not discovered
                ):
                    discovered = True

                    try:
                        discovered_models = (
                            list_gemini_models(client)
                        )

                        order += [
                            name
                            for name in discovered_models
                            if name not in order
                        ]

                    except Exception as discovery_error:
                        log.warning(
                            "ListModels failed: %s",
                            discovery_error
                        )

        raise RuntimeError(
            "No usable Gemini model. "
            + " | ".join(errors[-3:])
        )

    except Exception as exc:
        with STATE_LOCK:
            STATUS["gemini_error"] = (
                str(exc)[:240]
            )

        raise

    finally:
        client.close()


def analysis_job(chat):
    try:
        image = camera_photo()

        send_photo(
            chat,
            image,
            "Image analyzed by Gemini"
        )

        try:
            telemetry = sensor_status()

        except Exception:
            telemetry = {
                "sensor_ok": False,
                "state": "UNAVAILABLE"
            }

        result = analyze(
            image,
            telemetry
        )

        send_text(
            chat,
            "🧠 Gemini result:\n" + result
        )

    except Exception as exc:
        log.warning(
            "Analysis failed: %s", exc
        )

        send_text(
            chat,
            "❌ Analysis failed: "
            + str(exc)[:300]
        )


# ===========================================
# CAMERA VIDEO RECORDING
# ===========================================

def record_clip(seconds):
    import cv2
    import numpy as np

    seconds = max(
        3, min(20, int(seconds))
    )

    frames = []
    start = time.monotonic()

    while (
        time.monotonic() - start < seconds
        and len(frames) < 160
    ):
        try:
            jpg = camera_photo()

            frame = cv2.imdecode(
                np.frombuffer(
                    jpg, np.uint8
                ),
                cv2.IMREAD_COLOR
            )

            if frame is not None:
                frames.append(frame)

        except Exception as exc:
            log.warning(
                "Recording frame skipped: %s",
                str(exc)[:100]
            )

        time.sleep(0.12)

    elapsed = (
        time.monotonic() - start
    )

    if len(frames) < 2:
        raise RuntimeError(
            "Not enough frames. "
            "Test /capture first."
        )

    height, width = (
        frames[0].shape[:2]
    )

    fps = max(
        0.5,
        min(
            15.0,
            len(frames) / max(elapsed, 0.5)
        )
    )

    fd, path = tempfile.mkstemp(
        prefix="safeguard_",
        suffix=".mp4"
    )

    os.close(fd)

    writer = cv2.VideoWriter(
        path,
        cv2.VideoWriter_fourcc(
            *"mp4v"
        ),
        fps,
        (width, height)
    )

    if not writer.isOpened():
        os.unlink(path)

        raise RuntimeError(
            "OpenCV MP4 codec unavailable"
        )

    try:
        for frame in frames:
            if frame.shape[:2] != (
                height, width
            ):
                frame = cv2.resize(
                    frame,
                    (width, height)
                )

            writer.write(frame)

    finally:
        writer.release()

    return path, len(frames), fps


def recording_job(chats, seconds):
    if not RECORD_LOCK.acquire(
        blocking=False
    ):
        for chat in chats:
            send_text(
                chat,
                "⏳ Recording already running."
            )

        return

    path = None

    try:
        for chat in chats:
            send_text(
                chat,
                f"🎥 Recording approximately "
                f"{seconds}s..."
            )

        path, count, fps = (
            record_clip(seconds)
        )

        for chat in chats:
            send_clip(
                chat,
                path,
                f"SafeGuard AI • {count} frames "
                f"• ~{fps:.1f} fps • no audio"
            )

    except Exception as exc:
        log.exception(
            "Video recording failed"
        )

        for chat in chats:
            try:
                send_text(
                    chat,
                    "❌ Video failed: "
                    + str(exc)[:200]
                )
            except Exception:
                pass

    finally:
        if path:
            Path(path).unlink(
                missing_ok=True
            )

        RECORD_LOCK.release()


# ===========================================
# AUTOMATIC DANGER ALERT
# ===========================================

def auto_danger(sensor):
    if not ALERT_LOCK.acquire(
        blocking=False
    ):
        return

    try:
        msg = (
            "🚨 SAFEGUARD AI CRITICAL PROXIMITY\n"
            f"Distance: {sensor.get('distance_cm')} cm\n"
            f"Alert level: {sensor.get('alert_level')}\n"
            f"At: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            "Demo notification. Inspect locally."
        )

        for chat in ALLOWED:
            try:
                send_text(chat, msg)
            except Exception as exc:
                log.warning(
                    "Telegram alert failed: %s",
                    exc
                )

        try:
            jpg = camera_photo()

        except Exception as exc:
            log.warning(
                "Alert photo failed: %s",
                exc
            )
            return

        for chat in ALLOWED:
            try:
                send_photo(
                    chat,
                    jpg,
                    "Critical proximity snapshot"
                )

            except Exception as exc:
                log.warning(
                    "Alert snapshot failed: %s",
                    exc
                )

        if valid_secret(API_KEY):
            try:
                ai = analyze(
                    jpg, sensor
                )

                for chat in ALLOWED:
                    try:
                        send_text(
                            chat,
                            "🧠 Gemini: " + ai
                        )
                    except Exception:
                        pass

            except Exception as exc:
                log.warning(
                    "Gemini skipped; alert sent: %s",
                    exc
                )

        if AUTO_RECORD_SECONDS:
            threading.Thread(
                target=recording_job,
                args=(
                    list(ALLOWED),
                    AUTO_RECORD_SECONDS
                ),
                daemon=True
            ).start()

    finally:
        ALERT_LOCK.release()


# ===========================================
# INDEPENDENT CAMERA SAFETY OBSERVER
# ===========================================

def vision_alert(event, jpeg):
    """Send supervisor CHECK request, never an unverified injury declaration."""
    if not ALERTS.is_set() or not ALLOWED or not TELEGRAM_URL:
        return
    header = ("🔎 SAFEGUARD AI · VISUAL CHECK\n"
              f"{event['title']}\n"
              f"{event['detail']}\n"
              "⚠️ Automated suspicion only — inspect the worker; actual injury UNKNOWN.")
    for chat in ALLOWED:
        try:
            send_text(chat, header)
            if jpeg:
                send_photo(chat, jpeg, "Image at possible visual safety event")
        except Exception as exc:
            log.warning("Vision Telegram delivery: %s", str(exc)[:150])
    if VISION_GEMINI_ON_ALERT and jpeg and valid_secret(API_KEY):
        try:
            context = {'source': 'camera_cv', 'event': event, 'sensor': STATUS.get('sensor') or {}}
            ai = analyze(jpeg, context)
            for chat in ALLOWED:
                try: send_text(chat, "🧠 AI interpretation (not injury proof):\n" + ai)
                except Exception: pass
        except Exception as exc:
            log.warning("Vision Gemini optional check: %s", str(exc)[:140])


def send_vision_to_devkit(code):
    """Advisory display/buzzer, only local Wi-Fi; does NOT stop a machine."""
    try:
        r = requests.post(ESP32 + '/vision', data={'state': code}, timeout=1.2)
        if not r.ok and r.status_code != 404:
            log.warning('DevKit vision feedback: HTTP %s', r.status_code)
    except requests.RequestException:
        pass  # Local ultrasonic alarm remains completely independent.


def vision_worker():
    """Runs regardless of ultrasonic readings, but doesn't monopolize ESP32-CAM."""
    consecutive_errors = 0
    last_push_code = None
    last_push_at = 0.0
    while not STOP.is_set():
        if not VISION_MONITOR.cfg.enabled:
            if last_push_code != 'PAUSED':
                send_vision_to_devkit('PAUSED')
                last_push_code = 'PAUSED'
            STOP.wait(1.2)
            continue
        try:
            # Reuse a frame within a one-second TTL if streaming; otherwise fresh.
            frame = camera_photo(fresh=True)
            report = VISION_MONITOR.process(frame)
            consecutive_errors = 0
            code = {
                'POSSIBLE FALL': 'FALL',
                'CHECK HAZARD ZONE': 'HAND',
                'LOW MOTION': 'STILL',
                'MOTION SEEN': 'MOTION'
            }.get(report['state'], 'NORMAL')
            now = time.monotonic()
            if code != last_push_code or now - last_push_at > 7:
                send_vision_to_devkit(code)
                last_push_code = code
                last_push_at = now
            while True:
                pending = VISION_MONITOR.pop_event()
                if not pending:
                    break
                event, jpg = pending
                log.warning("Possible visual event: %s", event['title'])
                threading.Thread(target=vision_alert, args=(event, jpg), daemon=True).start()
            if report['frames'] % 15 == 0:
                log.info("Vision: %s motion=%.1f%% pose=%s fps=%.1f",
                         report['state'], report['motion_percent'], report['pose_backend'], report['fps'])
        except Exception as exc:
            consecutive_errors += 1
            VISION_MONITOR.mark_error(str(exc))
            if consecutive_errors >= 3 and last_push_code != 'CAM_OFFLINE':
                send_vision_to_devkit('CAM_OFFLINE')
                last_push_code = 'CAM_OFFLINE'
            if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                log.warning("Camera monitoring unavailable: %s", str(exc)[:150])
        STOP.wait(VISION_INTERVAL + min(4, consecutive_errors * 0.4))


# ===========================================
# ESP32 MONITORING LOOP
# ===========================================

def monitor():
    global LAST_ALERT
    global PREVIOUS_DANGER

    last_camera = 0.0
    last_log = None

    while not STOP.is_set():
        try:
            s = sensor_status()

            with STATE_LOCK:
                STATUS["sensor"] = s
                STATUS["sensor_error"] = ""

            key = (
                s.get("state"),
                s.get("sensor_ok")
            )

            if key != last_log:
                log.info(
                    "ESP32 distance=%s "
                    "level=%s state=%s",
                    s.get("distance_cm"),
                    s.get("alert_level"),
                    s.get("state")
                )

                last_log = key

            danger = bool(
                s.get("sensor_ok")
                and s.get("danger")
            )

            now = time.monotonic()

            should_alert = (
                danger
                and not ALERT_LOCK.locked()
                and ALERTS.is_set()
                and bool(ALLOWED)
                and bool(TELEGRAM_URL)
                and (
                    not PREVIOUS_DANGER
                    or now - LAST_ALERT >= COOLDOWN
                )
            )

            if should_alert:
                LAST_ALERT = now

                threading.Thread(
                    target=auto_danger,
                    args=(s,),
                    daemon=True
                ).start()

            PREVIOUS_DANGER = danger

        except Exception as exc:
            with STATE_LOCK:
                STATUS["sensor_error"] = (
                    str(exc)[:200]
                )

            log.warning(
                "ESP32: %s", exc
            )

        if (
            time.monotonic() - last_camera
            > 10
        ):
            last_camera = (
                time.monotonic()
            )

            try:
                c = camera_status()

                with STATE_LOCK:
                    STATUS["camera"] = c
                    STATUS["camera_error"] = ""

            except Exception as exc:
                with STATE_LOCK:
                    STATUS["camera_error"] = (
                        str(exc)[:200]
                    )

                log.warning(
                    "ESP32-CAM: %s", exc
                )

        STOP.wait(MONITOR_INTERVAL)


# ===========================================
# TELEGRAM SUPERVISOR COMMANDS
# ===========================================

def command(chat, name, arg=""):

    if name in (
        "start", "help", "menu"
    ):
        send_text(
            chat,
            HELP,
            menu=True
        )

    elif name == "ping":
        send_text(
            chat,
            "✅ Bot is online."
        )

    elif name in (
        "id", "chatid"
    ):
        send_text(
            chat,
            f"Telegram user ID: {chat}"
        )

    elif name == "status":
        try:
            s = sensor_status()

            msg = (
                "📊 ESP32\n"
                f"Distance: {s.get('distance_cm')} cm\n"
                f"Level: {s.get('alert_level')}\n"
                f"State: {s.get('state')}\n"
                f"Sensor OK: {s.get('sensor_ok')}"
            )

        except Exception as exc:
            msg = (
                "❌ Sensor unreachable: "
                + str(exc)
            )

        try:
            c = camera_status()

            msg += (
                "\n📷 Camera: "
                f"{c.get('camera_ready')}"
                "\nFlash: "
                f"{c.get('flash_mode')}"
            )

        except Exception as exc:
            msg += (
                "\n❌ Camera unreachable: "
                + str(exc)
            )

        v = VISION_MONITOR.report()
        msg += ("\n👁 Vision: " + v['state'] +
                f" ({v['motion_percent']}% motion)")
        send_text(chat, msg)

    elif name in (
        "camstatus", "camera"
    ):
        send_text(
            chat,
            json.dumps(
                camera_status(),
                indent=2
            )
        )

    elif name in (
        "photo", "snapshot"
    ):
        send_photo(
            chat,
            camera_photo()
        )

    elif name == "flash":
        j = flash_control(arg)

        send_text(
            chat,
            f"💡 Flash: "
            f"{j.get('flash_mode')}"
        )

    elif name == "brightness":
        j = flash_control(
            brightness=int(arg)
        )

        send_text(
            chat,
            f"💡 Brightness: "
            f"{j.get('flash_brightness')}"
        )

    elif name in (
        "record", "clip"
    ):
        n = int(arg or "8")

        if not 3 <= n <= 20:
            raise ValueError(
                "Use /record 8 (3-20 seconds)"
            )

        threading.Thread(
            target=recording_job,
            args=([chat], n),
            daemon=True
        ).start()

    elif name == "analyze":
        send_text(
            chat,
            "🧠 Analyzing new camera image..."
        )

        threading.Thread(
            target=analysis_job,
            args=(chat,),
            daemon=True
        ).start()

    elif name == "models":
        models = (
            ", ".join(MODELS)
            if MODELS
            else "AUTO (models.list)"
        )

        send_text(
            chat,
            f"Configured Gemini models: {models}\n"
            f"Last working model: "
            f"{LAST_MODEL or 'none'}"
        )

    elif name == "alerts":
        if arg == "on":
            ALERTS.set()

        elif arg == "off":
            ALERTS.clear()

        else:
            raise ValueError(
                "Use /alerts on or /alerts off"
            )

        send_text(
            chat,
            "Remote alerts: "
            + (
                "ON" if ALERTS.is_set()
                else "OFF"
            )
            + ". Local buzzer unaffected."
        )

    elif name == "vision":
        if arg in ("on", "off"):
            VISION_MONITOR.configure({"enabled": arg == "on"})
        elif arg not in ("", "status"):
            raise ValueError("Use /vision status|on|off")
        d = VISION_MONITOR.report()
        send_text(chat, (
            f"👁 Vision: {d['state']}\n"
            f"Reason: {d['reason']}\n"
            f"Tracking backend: {d['pose_backend']}\n"
            f"Motion: {d['motion_percent']}%\n"
            f"Still for: {d['still_for']} s\n"
            f"Worker expected: {d['worker_expected']}\n"
            f"Monitoring: {d['enabled']}\n"
            "Possible hazards only; injury cannot be confirmed."
        ))
    elif name == "worker":
        if arg not in ("on", "off"):
            raise ValueError("Use /worker on or /worker off")
        VISION_MONITOR.configure({"worker_expected": arg == "on"})
        send_text(chat, "Worker expected in view: " + arg.upper() +
                  ". Low-motion alerts are now " + ("eligible." if arg == "on" else "pose-evidence only."))
    elif name == "still":
        cfg = VISION_MONITOR.configure({"still_seconds": float(arg)})
        send_text(chat, f"Low-motion observation delay: {cfg['still_seconds']} seconds")
    elif name == "zone":
        vals = [float(v) for v in arg.replace(",", " ").split()]
        cfg = VISION_MONITOR.configure({"hazard_zone": vals})
        send_text(chat, "Marked image zone: " + str(cfg['hazard_zone']) +
                  "\nThis is a visual ROI, not calibrated physical distance.")

    elif name == "live":
        send_text(
            chat,
            "Camera (same Wi-Fi): "
            f"{CAMERA}/\n"
            "Stream: "
            f"http://{urlparse(CAMERA).hostname}"
            ":81/stream\n"
            "Outside this Wi-Fi, use "
            "/photo or /record 8."
        )

    elif name in (
        "test", "diag"
    ):
        results = [
            "🧪 SAFEGUARD TEST",
            "✅ Telegram received command"
        ]

        for label, action in [
            ("ESP32", sensor_status),
            ("Camera", camera_status)
        ]:
            try:
                data = action()

                results.append(
                    f"✅ {label}: "
                    f"{data.get('device')}"
                )

            except Exception as exc:
                results.append(
                    f"❌ {label}: "
                    f"{str(exc)[:120]}"
                )

        results.append(
            "✅ Gemini key present"
            if valid_secret(API_KEY)
            else "⚠️ Gemini key missing"
        )

        model_info = (
            ", ".join(MODELS)
            if MODELS else "AUTO"
        )

        results.append(
            f"Models: {model_info}"
        )

        send_text(
            chat,
            "\n".join(results),
            menu=True
        )

    elif name == "alerttest":
        send_text(
            chat,
            "🔔 Test notification. "
            "No danger was detected."
        )

    else:
        send_text(
            chat,
            "Unknown command. Use /help.",
            menu=True
        )


# ===========================================
# TELEGRAM UPDATE HANDLING
# ===========================================

def handle_update(update):
    cb = update.get("callback_query")

    msg = (
        cb.get("message")
        if cb
        else update.get("message")
    )

    if not msg:
        return

    user = (
        cb or msg
    ).get("from", {})

    sender = user.get("id")
    chat_info = msg.get("chat", {})
    chat = chat_info.get("id")

    if cb:
        try:
            tg(
                "answerCallbackQuery",
                {
                    "callback_query_id":
                    cb["id"]
                }
            )
        except Exception:
            pass

    if (
        not isinstance(chat, int)
        or chat_info.get("type") != "private"
    ):
        return

    if sender not in ALLOWED:
        send_text(
            chat,
            "⛔ Unauthorized. "
            f"Your Telegram ID: {sender}. "
            "Add it to TELEGRAM_USER_IDS."
        )
        return

    if cb:
        name, _, arg = str(
            cb.get("data", "")
        ).partition(":")

    else:
        text = (
            msg.get("text") or ""
        ).strip()

        if not text.startswith("/"):
            return

        parts = text.split(
            maxsplit=1
        )

        name = (
            parts[0]
            .split("@")[0]
            .lstrip("/")
            .lower()
        )

        arg = (
            parts[1].strip().lower()
            if len(parts) > 1
            else ""
        )

    try:
        command(
            chat, name, arg
        )

    except Exception as exc:
        log.warning(
            "Bot command %s failed: %s",
            name, exc
        )

        send_text(
            chat,
            f"❌ /{name}: "
            + str(exc)[:260]
        )


def telegram_worker():
    """Reliable long-polling worker with visible diagnostics and retries.

    The old implementation returned permanently if the first getMe/webhook
    check failed. This worker retries so transient network failures do not
    leave the dashboard stuck on Telegram offline until a manual restart.
    """
    global BOT_USERNAME

    offset = None
    backoff = 5

    while not STOP.is_set():
        BOT_READY.clear()

        if not TELEGRAM_URL:
            error = "Missing or placeholder TELEGRAM_BOT_TOKEN in .env"
            update_telegram_status("configuration_error", error)
            log.error("Telegram: %s", error)
            STOP.wait(20)
            continue

        if not ALLOWED:
            error = "TELEGRAM_USER_IDS is empty or contains no numeric IDs"
            update_telegram_status("configuration_error", error)
            log.error("Telegram: %s", error)
            STOP.wait(20)
            continue

        try:
            # Check the token first and report the Telegram API result.
            me = tg("getMe", timeout=12).get("result", {})
            if not me.get("is_bot") or not me.get("username"):
                raise RuntimeError("getMe returned an unexpected bot response")

            BOT_USERNAME = me["username"]

            info = tg("getWebhookInfo", timeout=12).get("result", {})
            webhook_url = str(info.get("url") or "").strip()

            if webhook_url:
                if envflag("TELEGRAM_REMOVE_WEBHOOK", True):
                    tg(
                        "deleteWebhook",
                        {"drop_pending_updates": "false"},
                        timeout=12
                    )
                    log.info("Removed Telegram webhook; switched to long polling")
                else:
                    raise RuntimeError(
                        "Webhook is configured. Set TELEGRAM_REMOVE_WEBHOOK=true "
                        "or remove the webhook before polling."
                    )

            BOT_READY.set()
            update_telegram_status("connected", "", polling=True)
            log.info(
                "Telegram connected: @%s; authorized private user IDs loaded: %d",
                BOT_USERNAME, len(ALLOWED)
            )
            backoff = 5

            # Inner loop: poll messages until there is a network/API error.
            while not STOP.is_set():
                try:
                    params = {
                        "timeout": 20,
                        "allowed_updates": json.dumps(["message", "callback_query"])
                    }
                    if offset is not None:
                        params["offset"] = offset

                    r = requests.get(
                        TELEGRAM_URL + "/getUpdates",
                        params=params,
                        timeout=30
                    )

                    if r.status_code == 409:
                        raise RuntimeError(
                            "HTTP 409 Conflict: another process is polling this bot. "
                            "Close other m.py/Telegram polling processes."
                        )

                    r.raise_for_status()
                    data = r.json()
                    if not data.get("ok"):
                        raise RuntimeError(
                            "Telegram getUpdates failed: "
                            + str(data.get("description", "unknown error"))
                        )

                    update_telegram_status("connected", "", polling=True)

                    for item in data.get("result", []):
                        offset = int(item.get("update_id", 0)) + 1
                        # Handle long-running operations off the polling thread.
                        threading.Thread(
                            target=handle_update,
                            args=(item,),
                            daemon=True,
                            name="telegram-command"
                        ).start()

                except Exception as exc:
                    error = safe_telegram_error(exc)
                    BOT_READY.clear()
                    update_telegram_status("reconnecting", error, polling=False)
                    log.warning("Telegram polling disconnected: %s", error)
                    break

            if STOP.is_set():
                break

        except Exception as exc:
            error = safe_telegram_error(exc)
            BOT_READY.clear()
            update_telegram_status("error", error, polling=False)
            log.error("Telegram startup failed: %s", error)

        # Retry after initial failure or interrupted polling.
        retry_wait = backoff
        if "409" in str(STATUS.get("telegram_error", "")):
            retry_wait = max(retry_wait, 20)
        log.info("Telegram reconnect retry in %s seconds", retry_wait)
        STOP.wait(retry_wait)
        backoff = min(max(backoff * 2, 5), 60)

    BOT_READY.clear()
    update_telegram_status("stopped", "Backend is shutting down", polling=False)


# ===========================================
# FLASK BACKEND
# ===========================================

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = (
    19 * 1024 * 1024
)

CAMERA_ORIGIN = (
    f"{urlparse(CAMERA).scheme}://"
    f"{urlparse(CAMERA).netloc}"
)


@app.after_request
def add_headers(response):
    if (
        request.headers.get("Origin")
        == CAMERA_ORIGIN
    ):
        response.headers[
            "Access-Control-Allow-Origin"
        ] = CAMERA_ORIGIN

        response.headers[
            "Access-Control-Allow-Headers"
        ] = "Content-Type"

        response.headers[
            "Access-Control-Allow-Methods"
        ] = "POST,GET,OPTIONS"

        response.headers[
            "Access-Control-Allow-Private-Network"
        ] = "true"

    response.headers[
        "Cache-Control"
    ] = "no-store"

    return response


@app.get("/")
def home():
    page = (
        ROOT
        / "assets"
        / "backend_dashboard.html"
    )

    if page.exists():
        return page.read_text(
            encoding="utf-8"
        )

    return (
        "SafeGuard AI backend online; "
        "visit /api/health",
        200
    )


@app.get("/api/vision/status")
def vision_status_route():
    return jsonify(VISION_MONITOR.report())


@app.get("/api/vision/frame")
def vision_frame_route():
    frame = VISION_MONITOR.annotated_frame()
    if not frame:
        return jsonify({"error": "No processed camera frame yet"}), 503
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/vision/config")
def vision_config_route():
    if not local_only() or (request.headers.get('Origin') not in
                            (None, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}')):
        return jsonify({"error": "Configure using the laptop dashboard only"}), 403
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Send a JSON object"}), 400
        allowed = {"enabled", "worker_expected", "still_seconds", "min_motion_percent", "hazard_zone"}
        if any(k not in allowed for k in body):
            return jsonify({"error": "Unknown vision setting"}), 400
        return jsonify(VISION_MONITOR.configure(body))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/health")
def health():
    with STATE_LOCK:
        data = dict(STATUS)

    return jsonify({
        "ok": True,
        **data,
        "telegram_configured":
            bool(TELEGRAM_URL and ALLOWED),
        "telegram_connected":
            BOT_READY.is_set(),
        "telegram_username":
            BOT_USERNAME,
        "telegram_state": data.get("telegram_state", "starting"),
        "telegram_error": data.get("telegram_error", ""),
        "telegram_last_success": data.get("telegram_last_success", ""),
        "telegram_polling": data.get("telegram_polling", False),
        "telegram_config_error": (
            "TELEGRAM_BOT_TOKEN missing/placeholder"
            if not TELEGRAM_URL else
            "TELEGRAM_USER_IDS needs at least one numeric ID"
            if not ALLOWED else ""
        ),
        "alerts_enabled":
            ALERTS.is_set(),
        "gemini_configured":
            valid_secret(API_KEY),
        "gemini_model":
            LAST_MODEL or (
                MODELS[0]
                if MODELS
                else "AUTO"
            ),
        "esp32_url": ESP32,
        "camera_url": CAMERA,
        "vision": VISION_MONITOR.report()
    })


@app.get("/api/status")
def status_route():
    try:
        return jsonify(
            sensor_status()
        )

    except Exception as exc:
        return jsonify({
            "error": str(exc)[:180]
        }), 502


@app.get("/api/camera/status")
def camera_status_route():
    try:
        return jsonify(
            camera_status()
        )

    except Exception as exc:
        return jsonify({
            "error": str(exc)[:180]
        }), 502


@app.get("/api/photo")
def photo_route():
    try:
        return Response(
            camera_photo(),
            mimetype="image/jpeg"
        )

    except Exception as exc:
        return jsonify({
            "error": str(exc)[:180]
        }), 502


def local_only():
    return (
        request.remote_addr
        in ("127.0.0.1", "::1")
    )


@app.post("/api/control/flash")
def flash_route():
    if not local_only():
        return jsonify({
            "error": "Local laptop only"
        }), 403

    try:
        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        return jsonify(
            flash_control(
                data.get("mode"),
                data.get("brightness")
            )
        )

    except Exception as exc:
        return jsonify({
            "error": str(exc)[:180]
        }), 502


@app.post("/api/control/alerts")
def alert_route():
    if not local_only():
        return jsonify({
            "error": "Local laptop only"
        }), 403

    value = (
        request.get_json(silent=True)
        or {}
    ).get("enabled")

    if not isinstance(value, bool):
        return jsonify({
            "error":
                "enabled must be boolean"
        }), 400

    if value:
        ALERTS.set()
    else:
        ALERTS.clear()

    return jsonify({
        "ok": True,
        "alerts_enabled":
            ALERTS.is_set()
    })


@app.post("/api/control/record")
def record_route():
    if not local_only():
        return jsonify({
            "error": "Local laptop only"
        }), 403

    if (
        not BOT_READY.is_set()
        or not ALLOWED
    ):
        return jsonify({
            "error": "Telegram offline"
        }), 503

    try:
        n = int(
            (
                request.get_json(
                    silent=True
                )
                or {}
            ).get("seconds", 8)
        )

        if not 3 <= n <= 20:
            raise ValueError(
                "Duration must be 3-20 seconds"
            )

    except (ValueError, TypeError) as exc:
        return jsonify({
            "error": str(exc)
        }), 400

    if RECORD_LOCK.locked():
        return jsonify({
            "error": "Recording busy"
        }), 409

    threading.Thread(
        target=recording_job,
        args=(list(ALLOWED), n),
        daemon=True
    ).start()

    return jsonify({
        "ok": True,
        "message":
            "Recording requested; "
            "sending clip to supervisors"
    })


# Browser-recorded WebM upload
@app.route(
    "/api/telegram/video",
    methods=["POST", "OPTIONS"]
)
def browser_recording():

    if request.method == "OPTIONS":
        return "", 204

    if (
        not local_only()
        or request.headers.get(
            "Origin"
        ) not in (CAMERA_ORIGIN, None)
    ):
        return jsonify({
            "error":
                "Use the local laptop only"
        }), 403

    if (
        not BOT_READY.is_set()
        or not ALLOWED
    ):
        return jsonify({
            "error": "Telegram offline"
        }), 503

    if "video" not in request.files:
        return jsonify({
            "error": "Missing video upload"
        }), 400

    payload = request.files[
        "video"
    ].read(
        18 * 1024 * 1024 + 1
    )

    if (
        len(payload) > 18 * 1024 * 1024
        or not payload.startswith(
            b"\x1a\x45\xdf\xa3"
        )
    ):
        return jsonify({
            "error":
                "Invalid or oversized WebM"
        }), 400

    errors = []

    for chat in ALLOWED:
        try:
            tg(
                "sendDocument",
                {
                    "chat_id": chat,
                    "caption":
                        "SafeGuard WebM recording"
                },
                {
                    "document": (
                        "safeguard.webm",
                        io.BytesIO(payload),
                        "video/webm"
                    )
                },
                timeout=140
            )

        except Exception as exc:
            errors.append(
                str(exc)[:130]
            )

    return (
        jsonify({
            "ok": not errors,
            "errors": errors
        }),
        502 if errors else 200
    )


# ===========================================
# SAFE TELEGRAM STARTUP DIAGNOSTIC
# ===========================================

def check_telegram_cli():
    """Check the bot without exposing credentials or changing webhook state.

    Usage: py m.py --check-telegram
    """
    env_path = ROOT / ".env"
    print("SafeGuard AI - Telegram connection check")
    print("Configuration file:", env_path)
    if not env_path.is_file():
        print("FAIL: .env file not found beside m.py (rename .env.example to .env).")
        return 2

    if not TELEGRAM_URL:
        print("FAIL: TELEGRAM_BOT_TOKEN is missing or still a placeholder.")
        return 2
    if not ALLOWED:
        print("FAIL: TELEGRAM_USER_IDS needs numeric Telegram user IDs.")
        return 2

    print("Bot token present: yes (value hidden)")
    print("Authorized supervisor IDs:", len(ALLOWED))
    for label, url in (("ESP32_URL", ESP32), ("CAMERA_URL", CAMERA)):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print("WARNING:", label, "must be a plain URL like http://10.221.145.74")

    try:
        me = tg("getMe", timeout=15).get("result", {})
        if not me.get("is_bot"):
            print("FAIL: Telegram did not identify this account as a bot.")
            return 4
        print("PASS: Telegram API accepted the token. Bot: @" + str(me.get("username", "unknown")))
    except Exception as exc:
        print("FAIL: Telegram getMe:", safe_telegram_error(exc))
        print("Check revoked token, internet access, proxy or firewall.")
        return 3

    try:
        info = tg("getWebhookInfo", timeout=15).get("result", {})
        if info.get("url"):
            print("WARNING: A webhook is configured. The backend removes it when TELEGRAM_REMOVE_WEBHOOK=true.")
        else:
            print("PASS: No webhook is configured.")
    except Exception as exc:
        print("WARNING: Could not check webhook:", safe_telegram_error(exc))

    print("Next: stop all other m.py instances, run py m.py, then send /start in a private chat.")
    print("For diagnostics see http://127.0.0.1:%s/api/health" % PORT)
    return 0


# ===========================================
# MAIN ENTRY POINT
# ===========================================

if __name__ == "__main__":

    if "--check-telegram" in sys.argv:
        raise SystemExit(check_telegram_cli())

    if not (ROOT / ".env").is_file():
        log.error("Missing .env file beside m.py: %s", ROOT / ".env")
        log.error("Copy .env.example to .env and enter fresh credentials.")

    log.info("Telegram configuration: token=%s, authorized users=%d",
             "present" if TELEGRAM_URL else "missing/placeholder", len(ALLOWED))


    log.info(
        "Running backend: %s",
        Path(__file__).resolve()
    )

    log.info(
        "Gemini parsed models: %s",
        MODELS if MODELS
        else "AUTO (listModels)"
    )

    log.info(
        "ESP32=%s Camera=%s",
        ESP32,
        CAMERA
    )

    log.info(
        "Dashboard: "
        "http://127.0.0.1:%s",
        PORT
    )

    if HOST != "127.0.0.1":
        log.warning(
            "Do not expose this unauthenticated "
            "backend to the public internet"
        )

    threading.Thread(
        target=monitor,
        daemon=True
    ).start()

    threading.Thread(
        target=telegram_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=vision_worker,
        daemon=True,
        name="independent-camera-observer"
    ).start()

    try:
        app.run(
            host=HOST,
            port=PORT,
            threaded=True,
            debug=False,
            use_reloader=False
        )

    finally:
        STOP.set()
