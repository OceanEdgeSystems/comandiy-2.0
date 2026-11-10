// The knob poses as an Ooono NO2 co-driver over Bluetooth LE.
//
// The Ooono box is a light with a speaker: the Ooono app on the phone decides
// what to warn about and writes one command byte to the box. Here the knob
// serves the same GATT layout as the real box (decoded 2026-10-28), the app
// connects to the knob instead, and every write goes to the host as
// {"e":"ooono","w":"<hex>"}. The host decides what it means.
//
// What the app writes to the command slot (…0240): 04 03, then one byte --
//   10 speed camera (blue)      11 danger (red)
//   12 report received (green)  13 speed limit exceeded
//   14 phone connected          02 on connect
// On connecting to a box it has not seen before, the app plays 10-13 once.
// The button (…0210) notifies 01 00000000 on press, 02 <ms held> on release.
//
// It has to be the knob's radio, not the Pi's: the phone is paired with the
// Pi for audio, so it shows the Pi under its remembered name and the app never
// recognises it as an Ooono. The knob's address is one the phone has never
// seen.
//
// Callbacks run on the NimBLE host task, and Serial belongs to loop(), so
// events go through a queue that ooonoPoll() drains.
//
// Host -> knob:  {"ooono":{"btn":300}}   the box's button, held 300 ms
//                {"ooono":{"adv":false}} stop advertising (true resumes)
//                {"alert":{"text":"Speed camera","rgb":[40,110,255],"ms":10000}}
//                    with "limit":50 the middle is the speed-limit sign instead
//                    a warning on the knob; {"alert":null} takes it down
// While a warning is up, the knob's own press is the Ooono button: it goes to
// the app as the box's press would, and the app counts the presses -- one is
// a camera report, two are a danger report -- exactly as with the real box.
// Knob -> host:  {"e":"ooono","conn":true|false}
//                {"e":"ooono","w":"0403"}  what the app wrote to the command slot

#include <NimBLEDevice.h>

#define OOONO_UUID(x) "72e8" x "-d54b-48ea-a00c-5ea3973cebc7"

struct OooEvent { uint8_t kind; uint8_t len; uint8_t data[20]; };   // kind: 0 write, 1 conn, 2 disc
static QueueHandle_t oooQueue;
static NimBLECharacteristic *oooButton, *oooCommand, *oooBeat;
static bool oooAdvertising = true;
static uint32_t oooUpAt = 0, oooHeld = 0, oooBeatAt = 0;
static bool oooConnected = false, oooPressing = false;

// The warning on the knob's screen. Drawn by drawAlert() in knob-ui.ino.
struct Alert { char text[24]; uint16_t colour; uint32_t until; int limit; } alert = {"", 0, 0, 0};
bool alertActive() { return alert.until && (int32_t)(millis() - alert.until) < 0; }

static void oooPush(uint8_t kind, const uint8_t *d = nullptr, size_t n = 0) {
  OooEvent ev = {kind, (uint8_t)min(n, sizeof ev.data), {0}};
  if (d) memcpy(ev.data, d, ev.len);
  xQueueSend(oooQueue, &ev, 0);
}

class OooServer : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *, NimBLEConnInfo &) override { oooPush(1); }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &, int) override {
    oooPush(2);
    if (oooAdvertising) NimBLEDevice::startAdvertising();
  }
};

class OooCommand : public NimBLECharacteristicCallbacks {
  uint8_t setting = 0x03;
  void onWrite(NimBLECharacteristic *c, NimBLEConnInfo &) override {
    NimBLEAttValue v = c->getValue();
    oooPush(0, v.data(), v.size());
    // Reads back as [last command][setting], like the real box.
    if (v.size() == 2) setting = v[1];
    if (v.size() == 1) { uint8_t back[2] = {v[0], setting}; c->setValue(back, 2); }
  }
};

static NimBLECharacteristic *oooChar(NimBLEService *s, const char *uuid, uint32_t props, const char *hex) {
  NimBLECharacteristic *c = s->createCharacteristic(uuid, props);
  uint8_t buf[20]; size_t n = 0;
  for (; hex[2 * n] && n < sizeof buf; n++) sscanf(hex + 2 * n, "%2hhx", &buf[n]);
  c->setValue(buf, n);
  return c;
}

void ooonoBegin() {
  oooQueue = xQueueCreate(16, sizeof(OooEvent));
  NimBLEDevice::init("OOONO2");
  NimBLEServer *srv = NimBLEDevice::createServer();
  srv->setCallbacks(new OooServer());

  using P = NIMBLE_PROPERTY;
  const uint32_t R = P::READ, RN = P::READ | P::NOTIFY, RWN = P::READ | P::WRITE | P::NOTIFY;

  // The real box's values, because the app reads them on connect.
  NimBLEService *s1 = srv->createService(OOONO_UUID("0100"));
  oooChar(s1, OOONO_UUID("0110"), R, "01010004");
  oooChar(s1, OOONO_UUID("0120"), R, "f2cf4ef5cd34");
  oooChar(s1, OOONO_UUID("0130"), RWN, "0fe459010101");
  oooBeat = oooChar(s1, OOONO_UUID("0140"), RN, "00000018");

  NimBLEService *s2 = srv->createService(OOONO_UUID("0200"));
  oooButton = oooChar(s2, OOONO_UUID("0210"), RN, "02000000c8");
  oooChar(s2, OOONO_UUID("0211"), RN, "02000000c8");
  oooChar(s2, OOONO_UUID("0220"), RWN, "0000000000000000000000000000000000000000");
  oooChar(s2, OOONO_UUID("0230"), RWN, "0000000000000000000000000000000000000000");
  oooCommand = oooChar(s2, OOONO_UUID("0240"), RWN, "0203");
  oooCommand->setCallbacks(new OooCommand());

  NimBLEService *s3 = srv->createService(OOONO_UUID("0300"));
  oooChar(s3, OOONO_UUID("0310"), RN, "00000040");

  NimBLEService *s4 = srv->createService(OOONO_UUID("0400"));
  oooChar(s4, OOONO_UUID("0410"), RWN, "00000000");
  oooChar(s4, OOONO_UUID("0411"), RWN, "00000000");
  oooChar(s4, OOONO_UUID("0412"), RWN, "00000000");
  oooChar(s4, OOONO_UUID("0413"), RWN, "00000000");
  oooChar(s4, OOONO_UUID("0414"), RWN, "00000000");

  // Firmware update: a stub, because the app subscribes to it. Writes do nothing.
  NimBLEService *dfu = srv->createService("fe59");
  dfu->createCharacteristic("8ec90003-f315-4f60-9fb8-838830daea50", P::WRITE | P::INDICATE);

  for (NimBLEService *s : {s1, s2, s3, s4, dfu}) s->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  // Spelled out: the phone only finds a box in the background by this service
  // ID, and with the shorthand calls a scan saw the name and no ID, so the app
  // connected by hand but never by itself.
  NimBLEAdvertisementData data;
  data.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);
  data.addServiceUUID(NimBLEUUID(OOONO_UUID("1523")));
  data.setName("OOONO2");
  adv->setAdvertisementData(data);
  NimBLEAdvertisementData resp;
  resp.setName("OOONO2");
  adv->setScanResponseData(resp);
  adv->start();
}

// The button: 01 00000000 on press, 02 <ms held, big-endian> on release.
static void oooButtonState(uint8_t state, uint32_t ms) {
  uint8_t v[5] = {state, (uint8_t)(ms >> 24), (uint8_t)(ms >> 16), (uint8_t)(ms >> 8), (uint8_t)ms};
  oooButton->setValue(v, 5);
  oooButton->notify();
}

void alertCommand(JsonVariantConst a) {
  if (a.isNull()) { alert.until = 0; dirty = true; return; }
  strlcpy(alert.text, a["text"] | "", sizeof alert.text);
  JsonArrayConst c = a["rgb"];
  alert.colour = c.size() == 3 ? RGB565((int)c[0], (int)c[1], (int)c[2]) : C_WARN;
  alert.limit = a["limit"] | 0;               // the speed limit, drawn as the sign
  alert.until = millis() + constrain((int)(a["ms"] | 8000), 500, 60000);
  if (!alert.until) alert.until = 1;
  dirty = true;
}

// The knob's switch while a warning is up: the box's button, press and release.
void ooonoButtonDown() { oooPressing = true; oooButtonState(1, 0); }
void ooonoButtonUp(uint32_t held) {
  oooPressing = false;
  held = constrain(held, 1, 4000);
  oooButtonState(2, held);
  JsonDocument j; j["e"] = "ooono"; j["btn"] = held; emit(j);
}

void ooonoCommand(JsonObjectConst o) {
  if (o["btn"].is<int>()) {
    oooHeld = constrain((int)o["btn"], 50, 4000);
    oooButtonState(1, 0);
    oooUpAt = millis() + oooHeld;
  }
  if (o["adv"].is<bool>()) {
    oooAdvertising = o["adv"];
    if (oooAdvertising) NimBLEDevice::startAdvertising();
    else NimBLEDevice::stopAdvertising();
  }
}

void ooonoPoll() {
  OooEvent ev;
  while (xQueueReceive(oooQueue, &ev, 0) == pdTRUE) {
    JsonDocument j; j["e"] = "ooono";
    if (ev.kind == 0) {
      char hex[41];
      for (int i = 0; i < ev.len; i++) sprintf(hex + 2 * i, "%02x", ev.data[i]);
      hex[2 * ev.len] = 0;
      j["w"] = hex;
    } else {
      j["conn"] = oooConnected = ev.kind == 1;
    }
    emit(j);
  }
  if (oooUpAt && (int32_t)(millis() - oooUpAt) >= 0) {
    oooUpAt = 0;
    oooButtonState(2, oooHeld);
  }
  // The real box's heartbeat, every 30 s.
  if (millis() - oooBeatAt > 30000) {
    oooBeatAt = millis();
    oooBeat->notify();
  }
}
