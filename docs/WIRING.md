# SafeGuard AI — complete physical connections

This connection list matches `esp32/esp32.ino` and `esp32_cam/esp32_cam.ino`. It covers the physical SafeGuard prototype. The Arduino Uno traffic-light simulation is a separate project.

## 1. ESP32 DevKit + distance sensor + display + buzzer

Disconnect power while wiring. Power the DevKit from USB. Use its board-labelled USB/VIN/5V rail for the HC-SR04 only after verifying that pin supplies 5 V on your board. Some ESP32 variants label power pins differently. Do not connect two independent 5 V sources to the same rail.

| From / component pin | Connect to | Details |
|---|---|---|
| DevKit USB | Laptop / suitable USB supply | Controller power |
| HC-SR04 VCC | Verified DevKit 5 V rail | Do not use the 3V3 rail for a standard HC-SR04 |
| HC-SR04 GND | DevKit GND | Common ground |
| HC-SR04 TRIG | DevKit GPIO5 | Trigger output |
| HC-SR04 ECHO | One end of 1 kΩ resistor R1 | Never connect ECHO directly to an ESP32 GPIO |
| Other end of R1 | Junction J | J is the divided signal, not a supply rail |
| Junction J | DevKit GPIO18 | Echo input |
| Junction J | One end of 2 kΩ resistor R2 | Lower divider resistor |
| Other end of R2 | DevKit GND | Completes the voltage divider |
| SH1106 OLED VCC | DevKit 3V3 | Use a 3.3 V-compatible OLED; I2C pull-ups must be to 3.3 V |
| SH1106 OLED GND | DevKit GND | Common ground |
| SH1106 OLED SDA | DevKit GPIO21 | I2C data |
| SH1106 OLED SCL | DevKit GPIO22 | I2C clock; configured address `0x3C` |
| Compatible 3-pin buzzer module S / I/O | DevKit GPIO25 | Logic input only; module must accept 3.3 V control |
| Compatible 3-pin buzzer module VCC | DevKit 3V3 **only if rated for 3.3 V** | Otherwise use its rated supply and an appropriate driver / level interface |
| Buzzer module GND | DevKit GND | Common ground |

**Common ground:** sensor GND, OLED GND, buzzer GND and R2 must all return to DevKit GND. Breadboard power rails may be split in the middle; bridge matching rail sections when needed, and verify continuity.

### ECHO divider: exact connection

```text
HC-SR04 ECHO (nominal 5 V)
             |
          R1 1 kΩ
             |
             +----------- GPIO18 (ESP32 input)
             |  Junction J
          R2 2 kΩ
             |
         DevKit GND
```

The nominal output is `5 × 2 / (1 + 2) ≈ 3.33 V`. ESP32 GPIO is **not 5 V tolerant**. Verify the actual divider output, supply and resistor tolerances against your board's input limits; use a suitable 5 V-to-3.3 V level shifter if needed. The divider belongs on **ECHO**, not TRIG.

### Buzzer module vs bare buzzer

The table assumes a three-pin module with its own suitable input circuitry, not a bare buzzer load. Check the module's printed pin labels rather than assuming a physical pin order. Do not drive a high-current buzzer directly from GPIO25; use a correctly rated transistor/MOSFET driver. Add flyback protection for an inductive load as specified by its manufacturer. A module requiring 5 V power must not pull its ESP32 signal pin up to 5 V.

- Supplied firmware: `PIN_BUZZ=25`, `PASSIVE_BUZZER=true`, `ACTIVE_HIGH=true`.
- A passive module uses the PWM tone output. Set `PASSIVE_BUZZER=false` for an active module.
- Match `ACTIVE_HIGH` to the actual module's trigger polarity.

## 2. Separate AI Thinker ESP32-CAM: normal operation

| Camera connection | Connect to | Details |
|---|---|---|
| 5V | Stable regulated 5 V supply | Use an adequate supply, typically 5 V / 1 A or better, with short leads |
| GND | That supply's GND | Power return |
| Camera ribbon | Onboard camera socket | Seat correctly with power off |
| GPIO0 | **No programming jumper to GND during normal operation** | Remove the flashing jumper, then reset |
| Flash LED | Already onboard, GPIO4 | No external LED wiring required |
| Wi-Fi | Same 2.4 GHz LAN as DevKit and laptop | Image/status data travels over Wi-Fi |

**Do not power the camera from the DevKit's 3V3 pin.** No GPIO, UART or I2C cable is needed between the DevKit and camera. Their independent power supplies need their own proper ground returns; no extra inter-board ground wire is required for a Wi-Fi-only link. If you intentionally use one shared supply, connect each board to that supply's matching rails and ground.

### USB-to-UART connections for camera flashing only

| USB-to-UART adapter | ESP32-CAM |
|---|---|
| TXD, **3.3 V logic** | U0R / GPIO3 (RX) |
| RXD, **3.3 V logic** | U0T / GPIO1 (TX) |
| GND | GND; common with camera power-supply GND |
| Suitable 5 V power output, if adequate | 5V; otherwise use one separate regulated camera supply |
| Temporary jumper | Camera GPIO0 → GND before reset/upload |

A 5 V power output does not mean 5 V UART logic is acceptable: **UART signals must be 3.3 V**. Do not join the adapter's power output to a separate supply's output. After upload, disconnect GPIO0 from GND and reset/power-cycle the camera. TX/RX wires are not needed in normal Wi-Fi operation.

### Onboard camera signals already assigned by the firmware

These are internal camera connections, **not extra breadboard wires**:

| Camera signal | ESP32-CAM GPIO |
|---|---|
| PWDN / RESET | 32 / unused (`-1`) |
| XCLK | 0 |
| SIOD / SIOC | 26 / 27 |
| Y2 / Y3 / Y4 / Y5 | 5 / 18 / 19 / 21 |
| Y6 / Y7 / Y8 / Y9 | 36 / 39 / 34 / 35 |
| VSYNC / HREF / PCLK | 25 / 23 / 22 |
| White flash LED | 4 |

## 3. Laptop and network

1. Connect both boards and the laptop to the same 2.4 GHz Wi-Fi network; disable client isolation if it prevents device-to-device access.
2. Read the boards' actual addresses in Serial Monitor at **115200 baud**. Review `STATIC_IP` (DevKit) and `USE_STATIC_IP` (camera) for your LAN.
3. Put those addresses in `.env` as `ESP32_URL` and `CAMERA_URL`.
4. Check DevKit `/status`, camera `/status`, camera `/capture`, and camera port `81` `/stream` with `python verify_devices.py`.
5. Start `python m.py`; open the dashboard at `http://127.0.0.1:8765/` on the laptop.

The distance alarm works locally on the DevKit. Python is required for the vision observer/dashboard; internet and credentials are needed for Telegram/Gemini.

## 4. Before power-on

- Verify power polarity, supply voltages, common ground and divider connections.
- Confirm GPIO18 receives the divided ECHO signal, not raw 5 V ECHO.
- Confirm OLED supply/pull-ups and buzzer input are compatible with 3.3 V GPIO.
- Keep the camera's flashing jumper removed for normal boot.
- Verify the actual assembly electrically; this document is a wiring reference, not evidence that physical wiring has been tested.
