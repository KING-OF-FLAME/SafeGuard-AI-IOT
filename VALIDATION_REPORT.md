# Validation — SafeGuard AI

Verified on 31 October 2026 using Python 3.12.14 on Linux, before repository publication.

## Passed

- Base dependencies installed from `requirements.txt`; `python -m pip check` reported no conflicts.
- `python -m unittest discover -s tests -v`: **12 tests passed** using real Flask, NumPy and OpenCV. Covers motion, empty-scene suppression, supervised stillness, simulated fall/hand landmarks, settings validation, model parsing and firmware/API contracts.
- Fixed the supplied health-route test to enter a Flask application context; the original test failed with real Flask installed. Application detection logic was preserved.
- Flask test-client smoke checks: dashboard HTML, health JSON, vision status, missing-frame `503`, valid operator configuration, invalid-config `400`, and cross-origin config denial `403`.
- Python compilation: `m.py`, `vision_monitor.py`, `verify_devices.py`.
- JavaScript syntax: inline scripts in both supplied HTML dashboards checked with `node --check`.
- README local links/images and banner SVG structure checked.
- Known Telegram/Gemini credentials and Wi-Fi password from the source archive are absent from publishable files; report text checked too. `.env` and Python caches were excluded. Configuration and firmware now contain placeholders.

## Not verified

- Actual Arduino-ESP32 compilation, flashing and electrical operation; no physical boards were attached.
- HC-SR04 calibration, buzzer behavior, camera capture/FPS or real LAN connectivity.
- Actual MediaPipe landmarks; it is optional and was not installed. Offline tests simulate landmarks.
- Telegram message delivery or Gemini responses; no supplied credentials were used.
- Real-worker accuracy, alert latency, uptime or safety certification.

The existing screenshots/photos are evidence supplied in the project report, not screenshots from this validation run. No hardware, Telegram or Gemini success is inferred from offline checks.

## Reproduce

```bash
python -m pip install -r requirements.txt
python -m pip check
python -m unittest discover -s tests -v
```

For physical commissioning, flash both boards, configure a private `.env`, run `python verify_devices.py`, then `python m.py`. Follow the staged tests in the report and technical guide. Use harmless targets and staged postures; do not test with hazardous machinery or real falls.
