// SafeGuard AI Camera - compile-safe final revision for Arduino-ESP32 core 3.x
// Non-JPEG sensor workaround: RGB565 -> software JPEG. Lightweight 160x120 stream.
#include "esp_camera.h"
#include "img_converters.h"
#include <WiFi.h>
#include <WebServer.h>
#include <esp_heap_caps.h>
#include "camera_dashboard.h"

const char* WIFI_NAME = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
#define USE_STATIC_IP false
IPAddress localIP(10,221,145,74), gateway(10,221,145,1), subnet(255,255,255,0);

#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22
#define FLASH_PIN 4 // AI Thinker onboard white LED. No SD card in use.

WebServer server(80);
WiFiServer streamServer(81);
SemaphoreHandle_t cameraMutex;
uint8_t* lastJpeg=nullptr;
size_t lastJpegLength=0;
uint32_t lastJpegAt=0;
bool cameraReady = false;
uint16_t sensorPID = 0;
String flashMode = "off";
uint8_t flashBrightness = 70;
bool autoLight = false;
volatile uint16_t streamFPS = 0;
volatile bool streamConnected = false;
unsigned long autoLastChange = 0;
const int STREAM_DELAY_MS = 200;  // real FPS depends on conversion/network speed

void flashWrite(uint8_t brightness) {
  // LEDC core 3.x: GPIO4 was attached in setup.
  ledcWrite(FLASH_PIN, brightness);
}

void applyFlash() {
  flashWrite(flashMode == "on" || (flashMode == "auto" && autoLight)
             ? flashBrightness : 0);
}

// Call with camera mutex held; estimates light from raw RGB565 frame.
void measureAmbient(camera_fb_t* fb) {
  if (flashMode != "auto" || fb->format != PIXFORMAT_RGB565) return;
  unsigned long now = millis();
  if (now - autoLastChange < 3500) return;  // hysteresis for flash oscillation
  unsigned long sum = 0, count = 0;
  for (size_t i = 0; i + 1 < fb->len; i += 96) {
    uint16_t px = ((uint16_t)fb->buf[i] << 8) | fb->buf[i+1];
    uint8_t r = (px >> 11) & 31, g = (px >> 5) & 63, b = px & 31;
    sum += (r * 255 / 31 + g * 255 / 63 + b * 255 / 31) / 3;
    count++;
  }
  int brightness = count ? sum / count : 128;
  // LED changes will affect measured brightness; this is an approximation, not a lux sensor.
  bool proposed = autoLight ? brightness < 105 : brightness < 52;
  if (proposed != autoLight) {
    autoLight = proposed;
    autoLastChange = now;
    applyFlash();
  }
}

bool captureJpeg(uint8_t** result, size_t* size, bool forceFresh=false) {
  *result = nullptr; *size = 0;
  if (!cameraReady || xSemaphoreTake(cameraMutex, pdMS_TO_TICKS(2200)) != pdTRUE) return false;
  // Allow photo/Telegram clients to reuse the last MJPEG frame. This reduces
  // RGB565 software-JPEG conversion contention during continuous streaming.
  if (!forceFresh && lastJpeg && lastJpegLength && millis()-lastJpegAt < 1100) {
    uint8_t* copy=(uint8_t*)malloc(lastJpegLength);
    if(copy) {
      memcpy(copy,lastJpeg,lastJpegLength);
      *result=copy; *size=lastJpegLength;
      xSemaphoreGive(cameraMutex);
      return true;
    }
  }
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { xSemaphoreGive(cameraMutex); return false; }
  measureAmbient(fb);
  bool ok = (fb->format == PIXFORMAT_JPEG)
            ? fmt2jpg(fb->buf, fb->len, fb->width, fb->height, fb->format, 78, result, size)
            : frame2jpg(fb, 78, result, size);
  esp_camera_fb_return(fb);
  if(ok && *result && *size) {
    uint8_t* memory=(uint8_t*)(psramFound()?ps_malloc(*size):malloc(*size));
    if(memory) {
      memcpy(memory,*result,*size);
      if(lastJpeg)free(lastJpeg);
      lastJpeg=memory;lastJpegLength=*size;lastJpegAt=millis();
    }
  }
  xSemaphoreGive(cameraMutex);
  return ok && *result && *size;
}

bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  // This user's sensor rejected PIXFORMAT_JPEG (error 0x106).
  c.pixel_format = PIXFORMAT_RGB565;
  c.frame_size = FRAMESIZE_QQVGA; // 160x120, fits RAM and smooth-ish SW conversion
  c.jpeg_quality = 12; c.fb_count = 1;
  c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) { Serial.printf("Camera init error: 0x%x\n", err); return false; }
  sensor_t* s = esp_camera_sensor_get();
  if (s) { sensorPID = s->id.PID; Serial.printf("Sensor PID: 0x%04X\n", sensorPID); }
  return true;
}

void handleCapture() {
  uint8_t* data = nullptr; size_t n = 0;
  if (!captureJpeg(&data, &n, server.hasArg("fresh") && server.arg("fresh")=="1")) { server.send(503,"text/plain","Capture failed/busy"); return; }
  server.sendHeader("Cache-Control", "no-store");
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.setContentLength(n);
  server.send(200, "image/jpeg", "");
  WiFiClient client = server.client();
  size_t sent=0;
  while(sent<n && client.connected()) {
    size_t wr=client.write(data+sent, min((size_t)1024,n-sent));
    if(!wr)break;
    sent+=wr;
  }
  free(data);
}

void handleStatus() {
  String s="{\"device\":\"SAFEGUARD_CAM_01\",\"camera_ready\":";
  s += cameraReady ? "true" : "false";
  s += ",\"sensor_pid\":\"0x" + String(sensorPID, HEX) + "\"";
  s += ",\"wifi_connected\":";
  s += WiFi.status()==WL_CONNECTED ? "true" : "false";
  s += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  s += ",\"stream_port\":81,\"capture_format\":\"RGB565 to JPEG\"";
  s += ",\"stream_connected\":" + String(streamConnected?"true":"false");
  s += ",\"stream_fps\":" + String(streamFPS);
  s += ",\"flash_mode\":\"" + flashMode + "\"";
  s += ",\"flash_brightness\":" + String(flashBrightness);
  s += ",\"free_heap\":" + String(ESP.getFreeHeap()) + "}";
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200,"application/json",s);
}

void handleFlash() {
  if (server.hasArg("mode")) {
    String m = server.arg("mode");
    if (m!="on" && m!="off" && m!="auto") { server.send(400,"text/plain","Use mode=on|off|auto"); return; }
    flashMode=m;
    if (m == "auto") { autoLight=false; autoLastChange=0; }
  }
  if (server.hasArg("brightness")) flashBrightness = constrain(server.arg("brightness").toInt(),0,255);
  applyFlash();
  lastJpegAt=0; // Force a fresh image after flash changes.
  handleStatus();
}

// Run MJPEG endpoint on dedicated port/task; does not block port 80 API.
void streamTask(void* arg) {
  for(;;) {
    WiFiClient client=streamServer.available();
    if(!client) { vTaskDelay(pdMS_TO_TICKS(25)); continue; }
    client.setTimeout(1500);
    // Minimal HTTP request parsing; accept only GET /stream.
    String first=client.readStringUntil('\n'); first.trim();
    unsigned long start=millis();
    while(client.connected() && millis()-start<900) {
      String line=client.readStringUntil('\n'); line.trim();
      if(line.length()==0) break;
    }
    if (!(first.startsWith("GET /stream ") || first.startsWith("GET /stream?"))) {
      client.print("HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
      client.stop(); continue;
    }
    client.print("HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\nAccess-Control-Allow-Origin: *\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n");
    streamConnected=true; uint32_t fpsStart=millis(); uint16_t frames=0;
    while(client.connected()) {
      uint8_t* jpg=nullptr; size_t length=0;
      if(captureJpeg(&jpg,&length,true)) {
        client.printf("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",(unsigned)length);
        size_t pos=0;
        while(pos<length && client.connected()) {
          size_t wrote=client.write(jpg+pos,min((size_t)1024,length-pos));
          if(!wrote)break;
          pos+=wrote;
        }
        client.print("\r\n");
        free(jpg);
        if(pos!=length)break;
        frames++;
        if(millis()-fpsStart>=3000){streamFPS=(frames*1000UL)/(millis()-fpsStart);frames=0;fpsStart=millis();}
      }
      vTaskDelay(pdMS_TO_TICKS(STREAM_DELAY_MS));
    }
    streamConnected=false; streamFPS=0; client.stop();
  }
}

// Browser HTML/JS lives in a normal C++ header, not in the .ino file.
// This avoids Arduino sketch preprocessor misreading JS as C++ code.


void setup() {
  Serial.begin(115200); delay(700);
  cameraMutex=xSemaphoreCreateMutex();
  cameraReady=initCamera();
  // Must attach flash after camera clock setup. Core 3.x LEDC pin API.
  if(!ledcAttachChannel(FLASH_PIN,5000,8,7)) Serial.println("Flash PWM attach failed");
  flashWrite(0);
  WiFi.mode(WIFI_STA);
  if(USE_STATIC_IP) WiFi.config(localIP,gateway,subnet);
  WiFi.begin(WIFI_NAME,WIFI_PASSWORD);
  unsigned long since=millis();
  while(WiFi.status()!=WL_CONNECTED && millis()-since<20000){delay(350);Serial.print('.');}
  Serial.println();Serial.print("Camera IP: ");Serial.println(WiFi.localIP());
  server.on("/",HTTP_GET, [](){server.send_P(200,"text/html",DASHBOARD);});
  server.on("/capture",HTTP_GET,handleCapture);
  server.on("/status",HTTP_GET,handleStatus);
  server.on("/flash",HTTP_GET,handleFlash);
  server.begin(); streamServer.begin();
  xTaskCreatePinnedToCore(streamTask,"mjpeg",8192,nullptr,1,nullptr,1);
  Serial.println("Dashboard on :80, /stream on :81, /capture, /status, /flash");
}
void loop() {
  server.handleClient();
  static unsigned long lastReconnect=0;
  if(WiFi.status()!=WL_CONNECTED && millis()-lastReconnect>10000){lastReconnect=millis();WiFi.reconnect();}
  delay(2);
}
