"""Run after firmware uploads: `python verify_devices.py`.
Uses .env ESP32_URL/CAMERA_URL, no cloud secrets needed.
"""
from pathlib import Path
from urllib.parse import urlsplit
import os
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
ESP32 = os.getenv('ESP32_URL', 'http://10.221.145.179').rstrip('/')
CAMERA = os.getenv('CAMERA_URL', 'http://10.221.145.74').rstrip('/')
errors = []

def check(name, test):
    try:
        detail = test()
        print(f'[PASS] {name}: {detail}')
    except Exception as e:
        print(f'[FAIL] {name}: {e}')
        errors.append(name)

def sensor():
    data = requests.get(f'{ESP32}/status', timeout=6).json()
    assert data['device'] == 'SAFEGUARD_01'
    assert all(key in data for key in ['danger', 'sensor_ok', 'distance_cm', 'alert_level', 'state'])
    return f'level={data["alert_level"]} distance={data["distance_cm"]} cm'

def camera():
    data = requests.get(f'{CAMERA}/status', timeout=6).json()
    assert data['camera_ready'] is True
    return f'camera_ready=true sensor_pid={data.get("sensor_pid")}'

def image():
    res = requests.get(f'{CAMERA}/capture', timeout=20)
    res.raise_for_status()
    assert res.headers.get('content-type','').startswith('image/jpeg')
    assert res.content.startswith(b'\xff\xd8') and res.content.endswith(b'\xff\xd9')
    return f'JPEG bytes={len(res.content)}'

def stream():
    with requests.get(f'{urlsplit(CAMERA).scheme}://{urlsplit(CAMERA).hostname}:81/stream', timeout=(5,14), stream=True) as res:
        res.raise_for_status()
        assert 'multipart/x-mixed-replace' in res.headers.get('content-type', '')
        frame = next(res.iter_content(chunk_size=1024))
        assert frame
        return 'MJPEG headers and incoming data OK'

check('ESP32 DevKit /status', sensor)
check('ESP32-CAM /status', camera)
check('ESP32-CAM /capture', image)
check('ESP32-CAM port 81 /stream', stream)
print('\n' + ('All device checks passed.' if not errors else f'{len(errors)} test(s) failed: {", ".join(errors)}'))
raise SystemExit(1 if errors else 0)
