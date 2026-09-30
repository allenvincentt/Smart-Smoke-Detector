#include <Arduino.h>
#include <Preferences.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <esp_arduino_version.h>
#include <ctype.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>

constexpr uint8_t PIN_MQ2_AO    = 34;
constexpr uint8_t PIN_MQ2_DO    = 35;
constexpr uint8_t PIN_FIRE      = 33;
constexpr uint8_t PIN_RESET_BTN = 25;
constexpr uint8_t PIN_FAN_RELAY = 26;
constexpr uint8_t PIN_BUZZER    = 23;
constexpr uint8_t PIN_LED_R     = 21;
constexpr uint8_t PIN_LED_G     = 22;
constexpr uint8_t PIN_LED_B     = 19;

#define FIRE_SENSOR_NAME "IR"
constexpr bool     FIRE_ACTIVE_LOW  = true;
constexpr uint32_t FIRE_CONFIRM_MS  = 300;

#define USE_MQ2_DO 0
constexpr float    MQ2_DIVIDER_GAIN = (10.0f + 14.7f) / 14.7f;
constexpr float    MQ2_SUPPLY_V     = 5.0f;
constexpr float    MQ2_RL_KOHM      = 1.0f;
constexpr float    MQ2_CLEAN_AIR_RATIO = 9.83f;
constexpr float    MQ2_CURVE_M = -0.443f, MQ2_CURVE_B = 1.617f;
constexpr float    MQ2_DEFAULT_R0_KOHM = 10.0f;
constexpr float    MQ2_MAX_PPM      = 10000.0f;
constexpr float    MQ2_MIN_VOLTS    = 0.05f;
constexpr uint32_t MQ2_WARMUP_S     = 120;
constexpr uint32_t MQ2_FAULT_AFTER_MS = 3000;
constexpr uint8_t  MQ2_OVERSAMPLE   = 8;

constexpr float    DEFAULT_THR_PRE_PPM  = 250.0f;
constexpr float    DEFAULT_THR_CRIT_PPM = 500.0f;
constexpr float    HYSTERESIS       = 0.9f;
constexpr float    PPM_EMA_ALPHA    = 0.3f;
constexpr float    VOLT_AVG_ALPHA   = 0.05f;

constexpr uint32_t RESET_HOLD_MS    = 15000;
constexpr uint32_t BUTTON_DEBOUNCE_MS = 50;
constexpr uint32_t MUTE_MS          = 60000;
constexpr uint32_t FAN_RUN_ON_MS    = 30000;

constexpr bool     RELAY_ACTIVE_LOW = true;
constexpr bool     LED_COMMON_ANODE = true;
constexpr uint32_t LED_PWM_FREQ     = 5000;
constexpr uint8_t  LED_PWM_BITS     = 8;
constexpr uint32_t BEEP_PRE_ON_MS = 200, BEEP_PRE_OFF_MS = 800;
constexpr uint32_t CHIRP_ON_MS = 50,     CHIRP_OFF_MS = 2000;
constexpr uint32_t BLINK_ON_MS = 500,    BLINK_OFF_MS = 500;

constexpr uint32_t POLL_MS   = 100;
constexpr uint32_t NOTIFY_MS = 500;
#define DEVICE_NAME    "SmokeGuard-ESP32"
#define SERVICE_UUID   "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define COMMAND_UUID   "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define TELEMETRY_UUID "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
constexpr size_t   CMD_MAX_LEN = 128;

class DigitalFireSensor {
 public:
  DigitalFireSensor(uint8_t pin, bool activeLow, uint32_t confirmMs)
      : pin_(pin), activeLow_(activeLow), confirmMs_(confirmMs) {}

  void begin() { pinMode(pin_, activeLow_ ? INPUT_PULLUP : INPUT_PULLDOWN); }

  void update(uint32_t now) {
    bool raw = digitalRead(pin_) == (activeLow_ ? LOW : HIGH);
    if (!raw) {
      seen_ = false;
    } else if (!seen_) {
      seen_ = true;
      since_ = now;
    }
    detected_ = seen_ && now - since_ >= confirmMs_;
  }

  bool detected() const { return detected_; }

 private:
  uint8_t pin_;
  bool activeLow_;
  uint32_t confirmMs_;
  bool seen_ = false, detected_ = false;
  uint32_t since_ = 0;
};

class Mq2Sensor {
 public:
  void begin(uint32_t now) {
    analogReadResolution(12);
#if USE_MQ2_DO
    pinMode(PIN_MQ2_DO, INPUT);
#endif
    startMs_ = now;
  }

  void update(uint32_t now) {
    uint32_t mv = 0;
    for (uint8_t i = 0; i < MQ2_OVERSAMPLE; i++) mv += analogReadMilliVolts(PIN_MQ2_AO);
    volts_ = mv / (float)MQ2_OVERSAMPLE / 1000.0f * MQ2_DIVIDER_GAIN;

    bool bad = volts_ < MQ2_MIN_VOLTS;
    if (!bad) {
      badSeen_ = false;
    } else if (!badSeen_) {
      badSeen_ = true;
      badSince_ = now;
    }
    fault_ = badSeen_ && now - badSince_ >= MQ2_FAULT_AFTER_MS;

    if (!bad) {
      ppm_ += PPM_EMA_ALPHA * (ppmFromVolts(volts_) - ppm_);
      vAvg_ = vAvg_ < 0 ? volts_ : vAvg_ + VOLT_AVG_ALPHA * (volts_ - vAvg_);
    }
#if USE_MQ2_DO
    doTripped_ = digitalRead(PIN_MQ2_DO) == LOW;
#endif
  }

  bool calibrate(uint32_t now) {
    if (warmupLeftS(now) > 0 || fault_ || vAvg_ < 0) return false;
    r0Kohm_ = rsKohm(vAvg_) / MQ2_CLEAN_AIR_RATIO;
    ppm_ = 0;
    return true;
  }

  uint32_t warmupLeftS(uint32_t now) const {
    uint32_t elapsedS = (now - startMs_) / 1000;
    return elapsedS >= MQ2_WARMUP_S ? 0 : MQ2_WARMUP_S - elapsedS;
  }

  float ppm() const { return ppm_; }
  float volts() const { return volts_; }
  bool fault() const { return fault_; }
  bool doTripped() const { return doTripped_; }
  float r0Kohm() const { return r0Kohm_; }
  void setR0Kohm(float r0) { r0Kohm_ = r0; }

 private:
  static float rsKohm(float v) {
    v = constrain(v, 0.01f, MQ2_SUPPLY_V - 0.01f);
    return (MQ2_SUPPLY_V - v) / v * MQ2_RL_KOHM;
  }

  float ppmFromVolts(float v) const {
    float ratio = rsKohm(v) / r0Kohm_;
    return fminf(MQ2_MAX_PPM, powf(10.0f, (log10f(ratio) - MQ2_CURVE_B) / MQ2_CURVE_M));
  }

  uint32_t startMs_ = 0, badSince_ = 0;
  bool badSeen_ = false, fault_ = false, doTripped_ = false;
  float volts_ = 0, ppm_ = 0, vAvg_ = -1;
  float r0Kohm_ = MQ2_DEFAULT_R0_KOHM;
};

class Button {
 public:
  explicit Button(uint8_t pin) : pin_(pin) {}
  void begin() { pinMode(pin_, INPUT_PULLUP); }

  bool pressed(uint32_t now) {
    bool raw = digitalRead(pin_) == LOW;
    if (raw != lastRaw_) {
      lastRaw_ = raw;
      changedAt_ = now;
    }
    if (now - changedAt_ >= BUTTON_DEBOUNCE_MS && raw != stable_) {
      stable_ = raw;
      return stable_;
    }
    return false;
  }

 private:
  uint8_t pin_;
  bool lastRaw_ = false, stable_ = false;
  uint32_t changedAt_ = 0;
};

enum class Level : uint8_t { Clean = 0, Pre = 1, Crit = 2 };

struct LevelText {
  static const char* of(Level l) {
    switch (l) {
      case Level::Crit: return "CRIT";
      case Level::Pre:  return "PRE";
      default:          return "CLEAN";
    }
  }
};

struct AlarmInputs {
  float ppm;
  bool mq2DoTripped;
  bool fire;
  bool warming;
  bool mq2Fault;
};

class AlarmLogic {
 public:
  float thrPre = DEFAULT_THR_PRE_PPM;
  float thrCrit = DEFAULT_THR_CRIT_PPM;

  Level evaluate(const AlarmInputs& in) {
    uint8_t cur = (uint8_t)state_;
    uint8_t lvl;
    if (in.fire) {
      lvl = 2;
    } else if (in.mq2Fault) {
      lvl = cur > 1 ? cur : 1;
    } else if (in.warming) {
      lvl = 0;
    } else {
      lvl = in.mq2DoTripped ? 2 : levelFor(in.ppm, cur);
    }
    state_ = (Level)lvl;
    return state_;
  }

  Level state() const { return state_; }

  static bool validThresholds(float pre, float crit) {
    return pre > 0 && crit > pre && crit <= MQ2_MAX_PPM;
  }

 private:
  uint8_t levelFor(float ppm, uint8_t cur) const {
    if (ppm >= thrCrit * (cur >= 2 ? HYSTERESIS : 1.0f)) return 2;
    if (ppm >= thrPre * (cur >= 1 ? HYSTERESIS : 1.0f)) return 1;
    return 0;
  }

  Level state_ = Level::Clean;
};

class TimedWindow {
 public:
  explicit TimedWindow(uint32_t lengthMs) : lengthMs_(lengthMs) {}

  void trigger(uint32_t now) {
    active_ = true;
    start_ = now;
  }

  void cancel() { active_ = false; }

  bool update(uint32_t now) {
    if (active_ && now - start_ >= lengthMs_) {
      active_ = false;
      return true;
    }
    return false;
  }

  bool active() const { return active_; }
  uint32_t leftS(uint32_t now) const {
    return active_ ? (lengthMs_ - (now - start_) + 999) / 1000 : 0;
  }

 private:
  uint32_t lengthMs_;
  bool active_ = false;
  uint32_t start_ = 0;
};

class FanRunOn {
 public:
  explicit FanRunOn(uint32_t lengthMs) : window_(lengthMs) {}

  bool update(Level st, bool resetHold, uint32_t now) {
    if (resetHold) {
      cancel();
      return false;
    }
    if (st == Level::Crit) {
      latched_ = true;
      window_.cancel();
      return false;
    }
    if (!latched_) return false;
    if (st != Level::Clean) {
      window_.cancel();
      return false;
    }
    if (!window_.active()) {
      window_.trigger(now);
      return false;
    }
    if (window_.update(now)) {
      latched_ = false;
      return true;
    }
    return false;
  }

  void cancel() {
    latched_ = false;
    window_.cancel();
  }

  bool active() const { return latched_; }

 private:
  TimedWindow window_;
  bool latched_ = false;
};

struct Color {
  uint8_t r, g, b;
};
constexpr Color COLOR_OFF    = {0, 0, 0};
constexpr Color COLOR_GREEN  = {0, 255, 0};
constexpr Color COLOR_YELLOW = {255, 90, 0};
constexpr Color COLOR_RED    = {255, 0, 0};
constexpr Color COLOR_BLUE   = {0, 0, 255};

enum class BuzzPattern : uint8_t { Off, Continuous, Intermittent, Chirp };

struct OutputPlan {
  bool fan;
  BuzzPattern buzz;
  Color color;
  bool blink;
};

struct OutputPolicy {
  static OutputPlan plan(Level st, bool warming, bool mq2Fault, bool resetHold, bool muted,
                         bool fanForced, bool fanRunOn) {
    if (resetHold) return {false, BuzzPattern::Off, COLOR_OFF, false};
    OutputPlan p = alarm(st, warming, mq2Fault);
    if (muted) p.buzz = BuzzPattern::Off;
    if (fanForced || fanRunOn) p.fan = true;
    return p;
  }

 private:
  static OutputPlan alarm(Level st, bool warming, bool mq2Fault) {
    if (st == Level::Crit) return {true, BuzzPattern::Continuous, COLOR_RED, false};
    if (mq2Fault)          return {false, BuzzPattern::Chirp, COLOR_YELLOW, true};
    if (st == Level::Pre)  return {false, BuzzPattern::Intermittent, COLOR_YELLOW, false};
    if (warming)           return {false, BuzzPattern::Off, COLOR_BLUE, true};
    return {false, BuzzPattern::Off, COLOR_GREEN, false};
  }
};

class Relay {
 public:
  explicit Relay(uint8_t pin) : pin_(pin) {}
  void begin() {
    digitalWrite(pin_, level(false));
    pinMode(pin_, OUTPUT);
  }
  void set(bool on) {
    if (on == on_) return;
    on_ = on;
    digitalWrite(pin_, level(on));
  }
  bool on() const { return on_; }

 private:
  static uint8_t level(bool on) { return (on != RELAY_ACTIVE_LOW) ? HIGH : LOW; }
  uint8_t pin_;
  bool on_ = false;
};

class Buzzer {
 public:
  explicit Buzzer(uint8_t pin) : pin_(pin) {}
  void begin() {
    digitalWrite(pin_, LOW);
    pinMode(pin_, OUTPUT);
  }

  void set(BuzzPattern p, uint32_t now) {
    if (p == pattern_) return;
    pattern_ = p;
    phaseStart_ = now;
    update(now);
  }

  void update(uint32_t now) {
    bool on = false;
    switch (pattern_) {
      case BuzzPattern::Continuous:   on = true; break;
      case BuzzPattern::Intermittent: on = inOnPhase(now, BEEP_PRE_ON_MS, BEEP_PRE_OFF_MS); break;
      case BuzzPattern::Chirp:        on = inOnPhase(now, CHIRP_ON_MS, CHIRP_OFF_MS); break;
      default: break;
    }
    digitalWrite(pin_, on ? HIGH : LOW);
  }

 private:
  bool inOnPhase(uint32_t now, uint32_t onMs, uint32_t offMs) const {
    return (now - phaseStart_) % (onMs + offMs) < onMs;
  }
  uint8_t pin_;
  BuzzPattern pattern_ = BuzzPattern::Off;
  uint32_t phaseStart_ = 0;
};

static void pwmAttach(uint8_t pin, uint8_t channel) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)channel;
  ledcAttach(pin, LED_PWM_FREQ, LED_PWM_BITS);
#else
  ledcSetup(channel, LED_PWM_FREQ, LED_PWM_BITS);
  ledcAttachPin(pin, channel);
#endif
}

static void pwmWrite(uint8_t pin, uint8_t channel, uint8_t duty) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)channel;
  ledcWrite(pin, duty);
#else
  (void)pin;
  ledcWrite(channel, duty);
#endif
}

class RgbLed {
 public:
  void begin() {
    for (uint8_t i = 0; i < 3; i++) {
      digitalWrite(pins_[i], LED_COMMON_ANODE ? HIGH : LOW);
      pinMode(pins_[i], OUTPUT);
      pwmAttach(pins_[i], i);
    }
    write(COLOR_OFF);
  }

  void set(Color c, bool blink, uint32_t now) {
    if (c.r != color_.r || c.g != color_.g || c.b != color_.b || blink != blink_) {
      color_ = c;
      blink_ = blink;
      phaseStart_ = now;
    }
  }

  void update(uint32_t now) {
    bool lit = !blink_ || (now - phaseStart_) % (BLINK_ON_MS + BLINK_OFF_MS) < BLINK_ON_MS;
    write(lit ? color_ : COLOR_OFF);
  }

 private:
  void write(Color c) {
    const uint8_t v[3] = {c.r, c.g, c.b};
    for (uint8_t i = 0; i < 3; i++) {
      pwmWrite(pins_[i], i, LED_COMMON_ANODE ? 255 - v[i] : v[i]);
    }
  }
  const uint8_t pins_[3] = {PIN_LED_R, PIN_LED_G, PIN_LED_B};
  Color color_ = COLOR_OFF;
  bool blink_ = false;
  uint32_t phaseStart_ = 0;
};

struct CommandMsg {
  char text[CMD_MAX_LEN];
};

class BleLink {
 public:
  void begin() {
    queue_ = xQueueCreate(4, sizeof(CommandMsg));
    BLEDevice::init(DEVICE_NAME);
    BLEDevice::setMTU(247);

    BLEServer* server = BLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks(this));
    BLEService* service = server->createService(SERVICE_UUID);

    telemetry_ = service->createCharacteristic(
        TELEMETRY_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
    telemetry_->addDescriptor(new BLE2902());

    BLECharacteristic* command = service->createCharacteristic(
        COMMAND_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
    command->setCallbacks(new CommandCallbacks(this));

    service->start();
    BLEAdvertising* adv = BLEDevice::getAdvertising();
    adv->addServiceUUID(SERVICE_UUID);
    adv->setScanResponse(true);
    BLEDevice::startAdvertising();
    Serial.println("[BLE] advertising as " DEVICE_NAME);
  }

  bool popCommand(CommandMsg& out) { return xQueueReceive(queue_, &out, 0) == pdTRUE; }

  void publish(const char* json) {
    telemetry_->setValue((uint8_t*)json, strlen(json));
    if (connected_) telemetry_->notify();
  }

  bool connected() const { return connected_; }

 private:
  class ServerCallbacks : public BLEServerCallbacks {
   public:
    explicit ServerCallbacks(BleLink* link) : link_(link) {}
    void onConnect(BLEServer*) override { link_->connected_ = true; }
    void onDisconnect(BLEServer*) override {
      link_->connected_ = false;
      BLEDevice::startAdvertising();
    }
   private:
    BleLink* link_;
  };

  class CommandCallbacks : public BLECharacteristicCallbacks {
   public:
    explicit CommandCallbacks(BleLink* link) : link_(link) {}
    void onWrite(BLECharacteristic* c) override {
      auto value = c->getValue();
      CommandMsg msg = {};
      strncpy(msg.text, value.c_str(), CMD_MAX_LEN - 1);
      xQueueSend(link_->queue_, &msg, 0);
    }
   private:
    BleLink* link_;
  };

  BLECharacteristic* telemetry_ = nullptr;
  QueueHandle_t queue_ = nullptr;
  volatile bool connected_ = false;
};

static const char* jsonValueStart(const char* json, const char* key) {
  char pattern[24];
  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  const char* p = strstr(json, pattern);
  if (!p) return nullptr;
  p += strlen(pattern);
  while (*p == ' ' || *p == ':') p++;
  return p;
}

static bool jsonString(const char* json, const char* key, char* out, size_t outLen) {
  const char* p = jsonValueStart(json, key);
  if (!p || *p != '"') return false;
  const char* end = strchr(++p, '"');
  if (!end || (size_t)(end - p) >= outLen) return false;
  memcpy(out, p, end - p);
  out[end - p] = '\0';
  return true;
}

static bool jsonNumberPair(const char* json, const char* key, float& a, float& b) {
  const char* p = jsonValueStart(json, key);
  if (!p || *p != '[') return false;
  char* end;
  a = strtof(p + 1, &end);
  if (end == p + 1) return false;
  while (*end == ' ' || *end == ',') end++;
  const char* second = end;
  b = strtof(second, &end);
  return end != second;
}

DigitalFireSensor fireSensor(PIN_FIRE, FIRE_ACTIVE_LOW, FIRE_CONFIRM_MS);
Mq2Sensor mq2;
Button resetButton(PIN_RESET_BTN);
AlarmLogic alarmLogic;
TimedWindow resetHold(RESET_HOLD_MS);
TimedWindow mute(MUTE_MS);
bool fanForced = false;
FanRunOn fanRunOn(FAN_RUN_ON_MS);
Relay fan(PIN_FAN_RELAY);
Buzzer buzzer(PIN_BUZZER);
RgbLed statusLed;
BleLink ble;
Preferences prefs;
bool r0Saved = false;

struct Snapshot {
  Level state = Level::Clean;
  bool fire = false, warming = true, fault = false, doTripped = false;
};
Snapshot snap;

constexpr uint8_t EVENT_SLOTS = 6;
char events[EVENT_SLOTS][28];
uint8_t eventHead = 0, eventCount = 0;

void pushEvent(const char* ev) {
  Serial.printf("[EVT] %s\n", ev);
  if (eventCount == EVENT_SLOTS) {
    eventHead = (eventHead + 1) % EVENT_SLOTS;
    eventCount--;
  }
  uint8_t slot = (eventHead + eventCount) % EVENT_SLOTS;
  strncpy(events[slot], ev, sizeof(events[slot]) - 1);
  events[slot][sizeof(events[slot]) - 1] = '\0';
  eventCount++;
}

const char* popEvent() {
  if (eventCount == 0) return "";
  const char* ev = events[eventHead];
  eventHead = (eventHead + 1) % EVENT_SLOTS;
  eventCount--;
  return ev;
}

void loadCalibration() {
  prefs.begin("smokeguard", true);
  float r0 = prefs.getFloat("r0", NAN);
  float pre = prefs.getFloat("thrPre", NAN);
  float crit = prefs.getFloat("thrCrit", NAN);
  prefs.end();
  if (!isnan(r0) && r0 > 0) {
    mq2.setR0Kohm(r0);
    r0Saved = true;
  } else {
    Serial.println("[CAL] no saved R0: will auto re-zero when warm-up ends. Keep the air clean.");
  }
  if (AlarmLogic::validThresholds(pre, crit)) {
    alarmLogic.thrPre = pre;
    alarmLogic.thrCrit = crit;
  }
  Serial.printf("[CAL] R0=%.2f kOhm, thresholds=[%.0f, %.0f] ppm\n",
                mq2.r0Kohm(), alarmLogic.thrPre, alarmLogic.thrCrit);
}

void saveCalibration() {
  prefs.begin("smokeguard", false);
  prefs.putFloat("r0", mq2.r0Kohm());
  prefs.putFloat("thrPre", alarmLogic.thrPre);
  prefs.putFloat("thrCrit", alarmLogic.thrCrit);
  prefs.end();
  Serial.printf("[CAL] saved R0=%.2f kOhm, thresholds=[%.0f, %.0f] ppm\n",
                mq2.r0Kohm(), alarmLogic.thrPre, alarmLogic.thrCrit);
}

void applyOutputs(uint32_t now);

void startReset(uint32_t now, const char* source) {
  resetHold.trigger(now);
  mute.cancel();
  fanForced = false;
  fanRunOn.cancel();
  applyOutputs(now);
  pushEvent(source);
}

void handleCommand(const char* text, uint32_t now) {
  char cmd[16];
  if (!jsonString(text, "cmd", cmd, sizeof(cmd))) {
    Serial.printf("[CMD] malformed: %s\n", text);
    return;
  }
  Serial.printf("[CMD] %s\n", text);

  if (strcmp(cmd, "reset") == 0) {
    startReset(now, "APP_RESET");
  } else if (strcmp(cmd, "thr") == 0) {
    float pre, crit;
    if (jsonNumberPair(text, "mq", pre, crit) && AlarmLogic::validThresholds(pre, crit)) {
      alarmLogic.thrPre = pre;
      alarmLogic.thrCrit = crit;
      saveCalibration();
      pushEvent("APP_THR");
    } else {
      pushEvent("APP_THR_REJECTED");
    }
  } else if (strcmp(cmd, "cal") == 0) {
    if (mq2.calibrate(now)) {
      r0Saved = true;
      saveCalibration();
      pushEvent("APP_CAL");
    } else {
      pushEvent("APP_CAL_REFUSED");
    }
  } else if (strcmp(cmd, "fan") == 0) {
    char mode[8];
    if (jsonString(text, "mode", mode, sizeof(mode)) &&
        (strcmp(mode, "ON") == 0 || strcmp(mode, "AUTO") == 0)) {
      fanForced = strcmp(mode, "ON") == 0;
      applyOutputs(now);
      pushEvent(fanForced ? "APP_FAN_ON" : "APP_FAN_AUTO");
    } else {
      pushEvent("APP_FAN_REJECTED");
    }
  } else if (strcmp(cmd, "mute") == 0) {
    mute.trigger(now);
    applyOutputs(now);
    pushEvent("APP_MUTE");
  } else if (strcmp(cmd, "unmute") == 0) {
    mute.cancel();
    applyOutputs(now);
    pushEvent("APP_UNMUTE");
  } else {
    pushEvent("APP_UNSUPPORTED");
  }
}

void pollSensors(uint32_t now) {
  fireSensor.update(now);
  mq2.update(now);

  Snapshot prev = snap;
  snap.fire = fireSensor.detected();
  snap.warming = mq2.warmupLeftS(now) > 0;
  snap.fault = mq2.fault();
  snap.doTripped = mq2.doTripped() && !snap.warming;

  if (prev.warming && !snap.warming && !r0Saved && mq2.calibrate(now)) {
    r0Saved = true;
    saveCalibration();
    pushEvent("AUTO_CAL");
  }

  snap.state = alarmLogic.evaluate({mq2.ppm(), snap.doTripped, snap.fire, snap.warming, snap.fault});

  bool escalated = (uint8_t)snap.state > (uint8_t)prev.state || (snap.fire && !prev.fire);
  if (escalated && mute.active()) {
    mute.cancel();
    pushEvent("MUTE_CANCELLED");
  }

  if (snap.fire != prev.fire) pushEvent(snap.fire ? "FLAME_DETECTED" : "FLAME_CLEARED");
  if (snap.fault != prev.fault) pushEvent(snap.fault ? "SENSOR_FAULT" : "SENSOR_OK");
  if (prev.warming && !snap.warming) pushEvent("WARMUP_DONE");
  if (snap.state != prev.state) {
    char ev[28];
    snprintf(ev, sizeof(ev), "STATE_%s->%s", LevelText::of(prev.state), LevelText::of(snap.state));
    pushEvent(ev);
  }
}

void applyOutputs(uint32_t now) {
  OutputPlan plan = OutputPolicy::plan(snap.state, snap.warming, snap.fault, resetHold.active(),
                                       mute.active(), fanForced, fanRunOn.active());
  fan.set(plan.fan);
  buzzer.set(plan.buzz, now);
  statusLed.set(plan.color, plan.blink, now);
  buzzer.update(now);
  statusLed.update(now);
}

void publishTelemetry(uint32_t now) {
  char json[256];
  snprintf(json, sizeof(json),
           "{\"t\":%.1f,\"mq\":%d,\"v\":%.2f,\"st\":\"%s\",\"mode\":\"%s\",\"fan\":%d,"
           "\"sil\":%d,\"do\":%d,\"wu\":%lu,\"flt\":%d,\"fl\":%d,\"rh\":%lu,\"mu\":%lu,"
           "\"thr\":[%d,%d],\"src\":\"" FIRE_SENSOR_NAME "\",\"ev\":\"%s\"}",
           now / 1000.0f, (int)lroundf(mq2.ppm()), mq2.volts(), LevelText::of(snap.state),
           fanForced ? "ON" : "AUTO", fan.on(), resetHold.active(), snap.doTripped,
           (unsigned long)mq2.warmupLeftS(now), snap.fault, snap.fire,
           (unsigned long)resetHold.leftS(now), (unsigned long)mute.leftS(now),
           (int)alarmLogic.thrPre, (int)alarmLogic.thrCrit, popEvent());
  ble.publish(json);

  static uint8_t n = 0;
  if (++n % 4 == 0) {
    Serial.printf("[TEL] mq=%d ppm v=%.2f V r0=%.2f kOhm st=%s fl=%d flt=%d wu=%lu rh=%lu\n",
                  (int)lroundf(mq2.ppm()), mq2.volts(), mq2.r0Kohm(), LevelText::of(snap.state),
                  snap.fire, snap.fault, (unsigned long)mq2.warmupLeftS(now),
                  (unsigned long)resetHold.leftS(now));
  }
}

void pollSerialCommands(uint32_t now) {
  static char line[CMD_MAX_LEN];
  static size_t len = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c != '\n' && c != '\r') {
      if (len < sizeof(line) - 1) line[len++] = c;
      continue;
    }
    if (len == 0) continue;
    line[len] = '\0';
    len = 0;
    if (line[0] == '{') {
      handleCommand(line, now);
      continue;
    }
    char word[16];
    float a, b;
    char json[CMD_MAX_LEN];
    if (sscanf(line, "thr %f %f", &a, &b) == 2) {
      snprintf(json, sizeof(json), "{\"cmd\":\"thr\",\"mq\":[%.0f,%.0f]}", a, b);
    } else if (sscanf(line, "fan %15s", word) == 1) {
      for (char* c = word; *c; c++) *c = toupper((unsigned char)*c);
      snprintf(json, sizeof(json), "{\"cmd\":\"fan\",\"mode\":\"%s\"}", word);
    } else if (sscanf(line, "%15s", word) == 1) {
      snprintf(json, sizeof(json), "{\"cmd\":\"%s\"}", word);
    } else {
      continue;
    }
    handleCommand(json, now);
  }
}

void setup() {
  fan.begin();
  buzzer.begin();
  statusLed.begin();

  Serial.begin(115200);
  Serial.println("\n[BOOT] SmokeGuard ESP32 firmware, fire sensor: " FIRE_SENSOR_NAME);

  uint32_t now = millis();
  fireSensor.begin();
  mq2.begin(now);
  resetButton.begin();
  loadCalibration();
  ble.begin();
  pushEvent("BOOT");
}

void loop() {
  static uint32_t lastPoll = 0, lastNotify = 0;
  uint32_t now = millis();

  if (resetButton.pressed(now)) startReset(now, "BTN_RESET");

  CommandMsg msg;
  while (ble.popCommand(msg)) handleCommand(msg.text, now);
  pollSerialCommands(now);

  if (now - lastPoll >= POLL_MS) {
    lastPoll = now;
    pollSensors(now);
  }

  if (resetHold.update(now)) pushEvent("RESET_HOLD_END");
  if (mute.update(now)) pushEvent("MUTE_END");
  if (fanRunOn.update(snap.state, resetHold.active(), now)) Serial.println("[FAN] run-on ended");
  applyOutputs(now);

  if (now - lastNotify >= NOTIFY_MS) {
    lastNotify = now;
    publishTelemetry(now);
  }

  delay(5);
}
