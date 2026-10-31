> Archived planning document supplied with the project. It describes an earlier design; use the root README for current behavior and thresholds.

# SafeGuard AI — Intelligent Industrial Worker Safety Monitoring System

Final Year B.Tech Artificial Intelligence & Data Science Project

Project Title: SafeGuard AI: An IoT-Based Industrial Hazard Monitoring System Using ESP32, Computer Vision, Gemini AI, and Telegram Alerts.

## 1. Problem Statement

Industrial workers operating machines in factories and manufacturing units are exposed to risks such as accidental contact with moving machinery, unsafe proximity, and delayed emergency response.

Traditional safety monitoring systems may rely on manual supervision, fixed sensors, or CCTV cameras. These systems often lack contextual understanding, automated incident reporting, and immediate communication with responsible personnel.

The problem is to develop an intelligent, affordable, and real-time monitoring system that detects potentially unsafe proximity to industrial machinery, analyzes the surrounding environment, and alerts supervisors before a hazardous situation escalates.

## 2. Proposed Solution

SafeGuard AI is an IoT and AI-powered industrial safety monitoring prototype that integrates an ESP32 microcontroller, ESP32-CAM, ultrasonic sensor, OLED display, buzzer, Gemini AI, and Telegram bot.

The system continuously monitors the distance between an object or worker and a designated machine danger zone.

When unsafe proximity is detected, it activates a local buzzer, displays a warning on the OLED, captures an image using ESP32-CAM, and sends the image with sensor information to Gemini AI for contextual analysis. The system then delivers an incident alert and AI-generated observations to authorized supervisors through Telegram.

The objective is to support early hazard detection, faster communication, and informed human intervention.

## 3. Five Key Benefits

1\. Early Hazard Detection

Detects objects entering a predefined danger zone and immediately triggers a local warning, potentially helping prevent accidents.

2\. Faster Emergency Communication

Automatically sends alerts and camera snapshots to authorized personnel through Telegram, reducing dependence on someone continuously watching CCTV.

3\. AI-Assisted Situation Understanding

Combines sensor measurements with camera images to provide more context than a basic distance alarm.

4\. Low-Cost and Scalable Prototype

Uses affordable ESP32-based components and can be extended to monitor multiple workstations through a centralized backend.

5\. Incident Documentation and Analysis

Captured images, sensor readings, and AI observations can be stored in an incident database for future analysis, reporting, and safety improvements.

## 4. Unique Features

| Feature                          | Description                                                                                                        |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Hybrid Edge–Cloud Architecture   | ESP32 handles local distance monitoring while Gemini performs cloud-based visual analysis.                         |
| Sensor-Triggered AI Vision       | ESP32-CAM captures images when a proximity event occurs rather than requiring continuous AI analysis.              |
| Multimodal Risk Assessment       | Combines distance readings, camera images, and monitoring context.                                                 |
| Telegram-Based Remote Monitoring | Authorized supervisors receive incident alerts and can request live status or camera snapshots.                    |
| Multi-Level Warning System       | OLED and buzzer provide safe, warning, and critical indications.                                                   |
| AI-Assisted Incident Explanation | Gemini generates an explanation of visible hazards and uncertainty instead of only reporting a distance threshold. |
| Expandable Multi-Machine Design  | The architecture can be extended to multiple ESP32 devices using unique device identifiers.                        |

Innovation: The project's key contribution is integrating low-cost proximity sensing, event-triggered visual analysis, AI-generated incident interpretation, and remote notifications into one monitoring workflow. These technologies already exist individually; the proposed innovation is their application and integration.

## 5. Methodology

System workflow

Step 1 — Sensor Monitoring

ESP32 + HC-SR04 measure distance continuously

Step 2 — Risk-Zone Detection

Compare distance with predefined thresholds

Local Response

OLED warning + buzzer alert

Image Capture

ESP32-CAM captures the scene

Step 3 — Backend Processing

Python retrieves sensor data and camera image

Step 4 — Gemini AI Analysis

Analyzes visible scene and potential hazards

Step 5 — Telegram Notification

Sends incident snapshot and AI observations

Step 6 — Supervisor Review

Supervisor evaluates the alert and takes appropriate action

### Example demonstration

| Distance     | System response                                                             |
| ------------ | --------------------------------------------------------------------------- |
| Above 100 cm | SAFE — normal monitoring                                                    |
| 40–100 cm    | WARNING — OLED warning and intermittent buzzer                              |
| Below 40 cm  | CRITICAL — local alarm, camera capture, Gemini analysis, and Telegram alert |

These are demonstration thresholds and must be calibrated for the setup.

## 6. Technologies Used

| Component        | Purpose                                   |
| ---------------- | ----------------------------------------- |
| ESP32 DevKit V1  | Main sensor monitoring controller         |
| HC-SR04          | Distance measurement                      |
| ESP32-CAM        | Visual monitoring and image capture       |
| SH1106 OLED      | Local safety status display               |
| 3-pin buzzer     | Audible warning                           |
| Python           | Backend monitoring and automation         |
| Gemini API       | AI-based visual scene interpretation      |
| Telegram Bot API | Remote alerts and monitoring              |
| Wi-Fi            | Communication between devices and backend |

## 7. Expected Outcome and Evaluation

The expected outcome is a working prototype that detects proximity events, generates local warnings, captures incident images, and delivers AI-assisted notifications to supervisors.

For a strong final-year AI&DS submission, evaluate distance measurement error, event detection accuracy, false-alarm rate, end-to-end alert latency, Gemini classification performance against manually labeled images, and system reliability under different lighting and background conditions.

Important project boundary: The current implementation is a monitoring and alerting prototype. It does not physically stop a machine, verify that an injury occurred, or provide certified industrial protection. A real industrial deployment would require independent safety-rated guards, interlocks, and emergency-stop mechanisms.

Final project statement for presentation:

> SafeGuard AI proposes a low-cost, intelligent industrial hazard monitoring framework that integrates IoT-based proximity sensing, event-triggered computer vision, multimodal AI analysis, and real-time remote alerts to improve hazard awareness and support faster safety intervention in industrial environments.
