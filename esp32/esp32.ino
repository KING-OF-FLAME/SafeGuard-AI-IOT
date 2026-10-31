// SafeGuard AI v3 - ESP32 DevKit V1 (Arduino-ESP32 core 3.x)
// Demo-only proximity monitor: OLED eye + local buzzer independent of Internet.
// IMPORTANT: HC-SR04 ECHO (5V) MUST have a divider/level shifter to 3.3V GPIO18.
#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SH110X.h>

const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
// Leave false unless you've verified your hotspot gateway and reserved the address.
const bool STATIC_IP = true;
IPAddress DEVICE_IP(10,221,145,179), GATEWAY(10,221,145,1), NETMASK(255,255,255,0);

constexpr uint8_t PIN_TRIG=5, PIN_ECHO=18, PIN_BUZZ=25, PIN_SDA=21, PIN_SCL=22;
// Change to false if using an ACTIVE buzzer (does not support variable Hz tones).
constexpr bool PASSIVE_BUZZER=true;
constexpr bool ACTIVE_HIGH=true;
constexpr float CAUTION_CM=100, WARNING_CM=60, CRITICAL_CM=40;
constexpr uint8_t FAIL_AFTER=5;           // tolerate short HC-SR04 timeouts
constexpr uint32_t SENSOR_PERIOD_MS=110; // HC-SR04 needs >=60ms between pings
constexpr uint32_t OLED_PERIOD_MS=150;
Adafruit_SH1106G oled(128,64,&Wire,-1);
WebServer web(80);

float distanceCm=-1, instantaneousCm=-1;
int alertLevel=4, criticalCount=0;
uint8_t invalidStreak=0, validCount=0, sampleIdx=0;
float history[3]={0,0,0};
bool oledOK=false, sensorOK=false, danger=false, buzzerReady=false;
uint32_t lastSensor=0, lastOled=0, lastValid=0, lastLog=0;
uint32_t lastToneHz=UINT32_MAX;
String machineState="STARTING";
// Optional Python visual-observer notices. These are advisory, not injury diagnoses.
String visionState="NO LINK";
uint32_t visionUpdated=0;
bool visionAlert=false;
uint32_t visionAlertAt=0;
const uint32_t VISION_TTL_MS=12000;


float pulseCm() {
  digitalWrite(PIN_TRIG,LOW); delayMicroseconds(3);
  digitalWrite(PIN_TRIG,HIGH); delayMicroseconds(10); digitalWrite(PIN_TRIG,LOW);
  uint32_t us=pulseIn(PIN_ECHO,HIGH,26000);
  if(!us) return -1;
  float cm=us*0.0343f/2.0f;
  return (cm>=2.0f && cm<=400.0f) ? cm : -1;
}
float recentMedian() {
  float a=history[0],b=history[1],c=history[2];
  if(validCount==1) return history[0];
  if(validCount==2) return (history[0]+history[1])*0.5f;
  if(a>b) {float t=a;a=b;b=t;}
  if(b>c) {float t=b;b=c;c=t;}
  if(a>b) {float t=a;a=b;b=t;}
  return b;
}
void updateSensor() {
  float cm=pulseCm(); instantaneousCm=cm;
  if(cm<0) {
    if(invalidStreak<255) invalidStreak++;
    if(invalidStreak>=FAIL_AFTER) {
      sensorOK=false; danger=false; distanceCm=-1; alertLevel=4;
      machineState="SENSOR ERROR"; validCount=0; sampleIdx=0; criticalCount=0;
    }
    return; // Retain last reading briefly instead of flickering ERROR / RUNNING
  }
  lastValid=millis(); invalidStreak=0; sensorOK=true;
  history[sampleIdx]=cm; sampleIdx=(sampleIdx+1)%3;
  if(validCount<3)validCount++;
  distanceCm=recentMedian();

  // Use raw close-range readings for quick critical recognition (2 consecutive).
  if(cm<CRITICAL_CM) criticalCount=min(criticalCount+1,3);
  else criticalCount=0;
  if(criticalCount>=2) {
    alertLevel=3; danger=true; machineState="CRITICAL";
  } else if(distanceCm<WARNING_CM) {
    alertLevel=2; danger=false; machineState="WARNING";
  } else if(distanceCm<CAUTION_CM) {
    alertLevel=1; danger=false; machineState="CAUTION";
  } else {
    alertLevel=0; danger=false; machineState="RUNNING";
  }
}

void setTone(uint32_t hz) {
  if(lastToneHz==hz) return;
  lastToneHz=hz;
  if(!buzzerReady) return;
  if(PASSIVE_BUZZER) ledcWriteTone(PIN_BUZZ,hz);
  else digitalWrite(PIN_BUZZ, ACTIVE_HIGH ? (hz?HIGH:LOW) : (hz?LOW:HIGH));
}
void updateAlarm() {
  uint32_t t=millis(),p; uint32_t hz=0;
  switch(alertLevel) {
    case 0: hz=0; break;
    case 1: p=t%2100; if(p<210 || (p>=370 && p<580))hz=1500; break;
    case 2: p=t%1160; if(p<170 || (p>=290&&p<460) || (p>=580&&p<750))hz=2300; break;
    case 3: p=t%480; hz=(p<190)?2700:(p<240)?0:(p<430)?3400:0; break;
    case 4: p=t%3000; if(p<350 || (p>=540&&p<890))hz=1100; break;
  }
  // Separate warning for a recent laptop-reported visual safety event.
  // Local CRITICAL and SENSOR ERROR alarms always have priority.
  if (alertLevel<3 && visionAlert && (uint32_t)(t-visionAlertAt)<8000) {
    uint32_t cvp=t%780;
    hz=(cvp<260)?2800:(cvp<320)?0:(cvp<580)?2300:0;
  }
  setTone(hz);
}

void drawEye(uint32_t now) {
  // White outline animated eye: glances + a blink every 5.6 seconds.
  const int x=102,y=28;
  bool blink=(now%5600)>5370;
  if(blink) {oled.drawLine(84,y,124,y,SH110X_WHITE); return;}
  oled.drawRoundRect(81,12,46,33,14,SH110X_WHITE);
  oled.drawRoundRect(83,14,42,29,12,SH110X_WHITE);
  int dx=((now/1200)%4==0)?-6:((now/1200)%4==2)?6:0;
  int pupil=alertLevel>=2?7:6;
  oled.fillCircle(x+dx,y,pupil,SH110X_WHITE);
  oled.fillCircle(x+dx+2,y-2,2,SH110X_BLACK);
  if(alertLevel>=2) { oled.drawLine(83,13,121,19,SH110X_WHITE); }
}
void updateOled() {
  if(!oledOK) return;
  uint32_t now=millis();
  oled.clearDisplay(); oled.setTextSize(1); oled.setTextColor(SH110X_WHITE);
  oled.setCursor(0,0); oled.println("SAFEGUARD AI");
  oled.drawFastHLine(0,10,128,SH110X_WHITE);
  drawEye(now);
  oled.setCursor(0,17); oled.print("DIST ");
  if(distanceCm>=0 && sensorOK) {oled.print(distanceCm,0);oled.print("cm");}
  else oled.print("---");
  oled.setCursor(0,29);oled.print("LEVEL ");oled.print(alertLevel);
  oled.setCursor(0,41);
  if(alertLevel==0) oled.print("SAFE");
  if(alertLevel==1) oled.print("CAUTION");
  if(alertLevel==2) oled.print("WARNING");
  if(alertLevel==3) oled.print("STOP DEMO");
  if(alertLevel==4) oled.print("SENSOR ERR");
  if(visionUpdated && (uint32_t)(now-visionUpdated)<VISION_TTL_MS && visionAlert) {
    oled.fillRect(0,40,79,12,SH110X_BLACK);
    oled.setCursor(0,41);
    if(visionState=="FALL")oled.print("CHECK FALL");
    else if(visionState=="HAND")oled.print("CHECK HAND");
    else if(visionState=="STILL")oled.print("CHECK STILL");
    else oled.print("CHECK CAM");
  }
  oled.drawFastHLine(0,52,128,SH110X_WHITE);
  oled.setCursor(0,55);oled.print(WiFi.status()==WL_CONNECTED?"NET OK":"NET OFF");
  oled.setCursor(64,55);
  if(visionUpdated && (uint32_t)(now-visionUpdated)<VISION_TTL_MS)
    oled.print(visionAlert?"CV ALERT":"CV ONLINE");
  else oled.print("CV OFFLINE");
  oled.display();
}
void sendJSON() {
  web.sendHeader("Cache-Control","no-store");
  String j="{\"device\":\"SAFEGUARD_01\",\"distance_cm\":"+String(distanceCm,1);
  j+=",\"raw_cm\":"+String(instantaneousCm,1);
  j+=",\"state\":\""+machineState+"\",\"alert_level\":"+String(alertLevel);
  j+=",\"danger\":"+String(danger?"true":"false");
  j+=",\"sensor_ok\":"+String(sensorOK?"true":"false");
  j+=",\"sensor_misses\":"+String(invalidStreak);
  j+=",\"age_ms\":"+String(lastValid?millis()-lastValid:0);
  j+=",\"vision_state\":\""+visionState+"\"";
  j+=",\"vision_alert\":"+String(visionAlert && (uint32_t)(millis()-visionAlertAt)<8000?"true":"false");
  j+=",\"wifi_connected\":"+String(WiFi.status()==WL_CONNECTED?"true":"false");
  j+=",\"ip\":\""+WiFi.localIP().toString()+"\",\"uptime_ms\":"+String(millis())+"}";
  web.send(200,"application/json",j);
}
void setup() {
  Serial.begin(115200);delay(250);
  pinMode(PIN_TRIG,OUTPUT);pinMode(PIN_ECHO,INPUT);digitalWrite(PIN_TRIG,LOW);
  if(PASSIVE_BUZZER) {buzzerReady=ledcAttach(PIN_BUZZ,2000,8); if(buzzerReady)ledcWriteTone(PIN_BUZZ,0);}
  else {pinMode(PIN_BUZZ,OUTPUT);digitalWrite(PIN_BUZZ,ACTIVE_HIGH?LOW:HIGH);buzzerReady=true;}
  Wire.begin(PIN_SDA,PIN_SCL);oledOK=oled.begin(0x3C,true);
  WiFi.mode(WIFI_STA);WiFi.setAutoReconnect(true);
  if(STATIC_IP)WiFi.config(DEVICE_IP,GATEWAY,NETMASK);
  WiFi.begin(WIFI_SSID,WIFI_PASSWORD);
  uint32_t start=millis();while(WiFi.status()!=WL_CONNECTED && millis()-start<12000)delay(150);
  web.on("/",HTTP_GET,[](){web.send(200,"text/plain","SafeGuard AI DevKit; GET /status");});
  web.on("/status",HTTP_GET,sendJSON);
  web.on("/vision",HTTP_POST,[](){
    if(!web.hasArg("state")) {web.send(400,"text/plain","Missing state");return;}
    String v=web.arg("state");v.toUpperCase();
    // Explicit allowlist; prevents unbounded text and JSON injection.
    if(v!="NORMAL" && v!="MOTION" && v!="STILL" && v!="FALL" &&
       v!="HAND" && v!="CAM_OFFLINE" && v!="PAUSED") {
      web.send(400,"text/plain","Invalid vision state");return;
    }
    bool newAlert=(v=="STILL" || v=="FALL" || v=="HAND");
    if(newAlert && (visionState!=v || !visionAlert)) visionAlertAt=millis();
    visionState=v;visionUpdated=millis();visionAlert=newAlert;
    web.send(200,"application/json","{\"ok\":true}");
  });
  web.begin();
  Serial.print("DevKit IP: ");Serial.println(WiFi.localIP());
  Serial.println("NOTE: MACHINE STOPPED IS DISPLAY-ONLY; NO ACTUATOR CONNECTED");
}
void loop() {
  web.handleClient();
  uint32_t now=millis();
  if(now-lastSensor>=SENSOR_PERIOD_MS) {lastSensor=now;updateSensor();}
  updateAlarm();
  if(now-lastOled>=OLED_PERIOD_MS) {lastOled=now;updateOled();}
  if(now-lastLog>1100) {lastLog=now;Serial.printf("cm=%.1f raw=%.1f level=%d misses=%u state=%s\n",distanceCm,instantaneousCm,alertLevel,invalidStreak,machineState.c_str());}
  delay(1);
}
