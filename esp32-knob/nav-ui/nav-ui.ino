// Turn-by-turn on a second round screen.
//
// Built for the Waveshare ESP32-S3-Touch-LCD-1.28, and buildable for the
// Elecrow CrowPanel the knob uses. Both are ESP32-S3 with a 240x240 GC9A01
// round panel, and their pin maps are close enough to be dangerous: SCLK, MOSI
// and CS agree, while DC, RST and the backlight do not, and the CrowPanel has
// two power-enable rails the Waveshare board does not. Getting any of that
// wrong produces a black screen with a module that runs perfectly -- the
// failure this project has already lost a session to. Hence one board select
// and no silent defaults.
//
// WHY A SEPARATE SKETCH RATHER THAN A MODE OF knob-ui
// This screen has no encoder to read, no menu to own and no selection to keep
// instant, so nearly all of knob-ui is dead weight here. What it does have is a
// single job it should do well from a cold start: show the next turn. Keeping
// them apart also means a change to the knob's interaction model cannot break
// the navigation display, which is the one that matters while driving.
//
// The two announce themselves with a `role` in `hello`, so the host can tell
// them apart on a bus where both are 0x303A and either might enumerate first.
//
// Protocol subset, one JSON object per line:
//   {"m":"nav","nav":{"maneuver":"left","dist":250,"unit":"m","street":"..."}}
//   {"m":"idle"}          no route; show the resting screen
//   {"bl":8..255}         backlight
//   {"ping":true}         re-send hello

#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>

#include "FreeSans9pt7b.h"
#include "FreeSansBold12pt7b.h"
#include "FreeSansBold18pt7b.h"
#include "FreeSans48pt7b.h"      // digits only; see knob-ui for how it is made

#define FW_VERSION "nav-ui 0.2"
#define FW_ROLE    "nav"

// ---- board select ----
// BOARD_WAVESHARE_128  Waveshare ESP32-S3-Touch-LCD-1.28  (this screen)
// BOARD_CROWPANEL_128  Elecrow CrowPanel 1.28"-HMI        (the knob's board)
#define BOARD_WAVESHARE_128

#if defined(BOARD_WAVESHARE_128)
  #define PIN_SCLK   10
  #define PIN_MOSI   11
  #define PIN_CS      9
  #define PIN_DC      8      // CrowPanel uses 3
  #define PIN_RST    12      // CrowPanel uses 14
  #define PIN_BL     40      // CrowPanel uses 46
  // No power-enable rails on this board. GPIO 1 and 2 are not free here, so
  // driving them "just in case" would be actively wrong.
  #define HAS_PWR_RAILS 0
#elif defined(BOARD_CROWPANEL_128)
  #define PIN_SCLK   10
  #define PIN_MOSI   11
  #define PIN_CS      9
  #define PIN_DC      3
  #define PIN_RST    14
  #define PIN_BL     46
  // Undocumented, in the factory source only: both must be high or the panel
  // gets no power at all. See docs/knob-protocol.md.
  #define PIN_PWR_1   1
  #define PIN_PWR_2   2
  #define HAS_PWR_RAILS 1
#else
  #error "pick a board: BOARD_WAVESHARE_128 or BOARD_CROWPANEL_128"
#endif

static const int CX = 120, CY = 120;
static const int R_OUT = 118, R_IN = 109;
static inline float fromTop(float deg) { return fmodf(270.0f + deg, 360.0f); }

#define C_BG      RGB565(8, 10, 14)
#define C_TEXT    RGB565(236, 238, 242)
#define C_DIM     RGB565(120, 128, 140)
#define C_FAINT   RGB565(52, 58, 68)
#define C_ACCENT  RGB565(64, 158, 255)
#define C_GOOD    RGB565(58, 190, 120)

Arduino_DataBus *bus = new Arduino_ESP32SPI(PIN_DC, PIN_CS, PIN_SCLK, PIN_MOSI, GFX_NOT_DEFINED);
Arduino_GFX *panel = new Arduino_GC9A01(bus, PIN_RST, 0, true);
Arduino_Canvas *gfx = new Arduino_Canvas(240, 240, panel);

// ---------------------------------------------------------------- state ----
struct Nav {
  char street[48] = "";
  char maneuver[16] = "straight";
  uint32_t dist = 0;
  char unit[6] = "m";
} nv;

bool     routing   = false;      // is there a route at all
uint32_t lastHostMs = 0;
bool     linked    = false;
uint8_t  backlight = 255;
bool     dirty     = true;
// Whether the panel came up at all. A 240x240 canvas is 115 kB, allocated at
// runtime, and if that allocation fails gfx->begin() returns false and every
// draw call afterwards is a no-op: the board runs, answers on serial, reports
// its role and shows nothing. That is exactly the failure this screen has been
// showing, and nothing was reporting it -- so now hello carries the answer.
bool     gfxOk     = false;

// --------------------------------------------------------------- drawing ----
void fitText(const char *s, const GFXfont *f, int maxW, char *out, size_t outN,
             uint16_t *wOut, int16_t *x1Out, int16_t *y1Out, uint16_t *hOut) {
  gfx->setFont(f);
  strlcpy(out, s, outN);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(out, 0, 0, &x1, &y1, &w, &h);
  size_t keep = strlen(out);
  while (w > maxW && keep > 1) {
    keep--;
    strlcpy(out, s, outN);
    out[keep] = 0;
    if (keep > 2) { out[keep - 1] = '.'; out[keep - 2] = '.'; }
    gfx->getTextBounds(out, 0, 0, &x1, &y1, &w, &h);
  }
  *wOut = w; *x1Out = x1; *y1Out = y1; *hOut = h;
}

void textCentered(const char *s, int baselineY, const GFXfont *f,
                  uint16_t color, int maxW = 200) {
  if (!s || !*s) return;
  char tmp[64]; uint16_t w, h; int16_t x1, y1;
  fitText(s, f, maxW, tmp, sizeof tmp, &w, &x1, &y1, &h);
  gfx->setTextColor(color);
  gfx->setCursor(CX - w / 2 - x1, baselineY);
  gfx->print(tmp);
}

void ring(float fromDeg, float sweepDeg, uint16_t color,
          int rOut = R_OUT, int rIn = R_IN) {
  if (sweepDeg <= 0.05f) return;
  if (sweepDeg >= 359.9f) sweepDeg = 359.9f;
  gfx->fillArc(CX, CY, rOut, rIn, fromTop(fromDeg), fromTop(fromDeg + sweepDeg), color);
}

// Maneuver arrow, a local-space polygon rotated to face `deg`.
void arrow(int cx, int cy, int size, float deg, uint16_t color) {
  const float rad = deg * DEG_TO_RAD;
  const float c = cosf(rad), s = sinf(rad);
  auto P = [&](float x, float y, int16_t &ox, int16_t &oy) {
    ox = cx + (int16_t)(x * c - y * s);
    oy = cy + (int16_t)(x * s + y * c);
  };
  const float hw = size * 0.34f, hl = size * 0.46f, sw = size * 0.15f;

  int16_t ax, ay, bx, by, dx, dy;
  P(0, -size * 0.55f, ax, ay);
  P(-hw, -size * 0.55f + hl, bx, by);
  P( hw, -size * 0.55f + hl, dx, dy);
  gfx->fillTriangle(ax, ay, bx, by, dx, dy, color);

  int16_t p1x, p1y, p2x, p2y, p3x, p3y, p4x, p4y;
  P(-sw, -size * 0.55f + hl, p1x, p1y);
  P( sw, -size * 0.55f + hl, p2x, p2y);
  P( sw,  size * 0.55f,      p3x, p3y);
  P(-sw,  size * 0.55f,      p4x, p4y);
  gfx->fillTriangle(p1x, p1y, p2x, p2y, p3x, p3y, color);
  gfx->fillTriangle(p1x, p1y, p3x, p3y, p4x, p4y, color);
}

float maneuverAngle(const char *m) {
  if (!strcmp(m, "left"))         return -90;
  if (!strcmp(m, "right"))        return  90;
  if (!strcmp(m, "slight-left"))  return -40;
  if (!strcmp(m, "slight-right")) return  40;
  if (!strcmp(m, "sharp-left"))   return -135;
  if (!strcmp(m, "sharp-right"))  return  135;
  if (!strcmp(m, "uturn"))        return  180;
  return 0;                       // roundabout and anything unknown
}

// The distance is the one thing read at a glance, so it gets the big face.
// The 48 pt font is digits only, so the number and its unit are drawn as two
// pieces and centred as one -- which also lets the unit sit small beside a
// large number instead of competing with it.
void drawDistance(int baselineY) {
  char num[12], unit[6];
  if (!strcmp(nv.unit, "km")) {
    // Sent as tenths so the firmware never parses a float.
    snprintf(num, sizeof num, "%lu.%lu",
             (unsigned long)(nv.dist / 10), (unsigned long)(nv.dist % 10));
    strlcpy(unit, "km", sizeof unit);
  } else {
    snprintf(num, sizeof num, "%lu", (unsigned long)nv.dist);
    strlcpy(unit, nv.unit, sizeof unit);
  }

  // "1.4" needs a decimal point the digits-only face does not have, so a km
  // distance falls back to the bold face rather than dropping the point.
  const GFXfont *numFont = strchr(num, '.') ? &FreeSansBold18pt7b : &FreeSans48pt7b;

  char tmp[16]; uint16_t nw, nh, uw, uh; int16_t nx1, ny1, ux1, uy1;
  fitText(num, numFont, 190, tmp, sizeof tmp, &nw, &nx1, &ny1, &nh);
  fitText(unit, &FreeSansBold12pt7b, 60, tmp, sizeof tmp, &uw, &ux1, &uy1, &uh);

  const int gap = 8;
  const int total = nw + gap + uw;
  int x = CX - total / 2;

  gfx->setFont(numFont);
  gfx->setTextColor(C_TEXT);
  gfx->setCursor(x - nx1, baselineY);
  gfx->print(num);

  gfx->setFont(&FreeSansBold12pt7b);
  gfx->setTextColor(C_DIM);
  gfx->setCursor(x + nw + gap - ux1, baselineY);
  gfx->print(unit);
}

void drawNav() {
  ring(0, 360, C_FAINT);
  ring(0, 360, C_GOOD, R_OUT, R_OUT - 4);
  arrow(CX, 84, 78, maneuverAngle(nv.maneuver), C_GOOD);
  drawDistance(172);
  textCentered(nv.street, 206, &FreeSans9pt7b, C_DIM, 180);
}

void drawIdle() {
  ring(0, 360, C_FAINT);
  textCentered("no route", CY + 6, &FreeSansBold18pt7b,
               linked ? C_DIM : C_FAINT, 180);
  if (!linked) textCentered("no host", 200, &FreeSans9pt7b, C_FAINT, 160);
}

void drawBoot() {
  ring(0, 360, C_FAINT);
  textCentered("NAV", 118, &FreeSansBold18pt7b, C_ACCENT, 200);
  textCentered(FW_VERSION, 150, &FreeSans9pt7b, C_FAINT, 200);
}

void render() {
  gfx->fillScreen(C_BG);
  if (!lastHostMs && millis() < 2000) drawBoot();
  else if (routing)                   drawNav();
  else                                drawIdle();
  gfx->flush();
}

// -------------------------------------------------------------- protocol ----
void emit(JsonDocument &j) {
  serializeJson(j, Serial);
  Serial.print('\n');
  Serial.flush();
}

void sendHello() {
  JsonDocument j;
  j["e"] = "hello";
  j["fw"] = FW_VERSION;
  j["gfx"] = gfxOk;
  j["heap"] = (uint32_t)ESP.getFreeHeap();
  j["psram"] = (uint32_t)ESP.getPsramSize();
  // The host has two of these on one bus, both 0x303A, either of which may
  // enumerate first. The role is how it tells them apart without depending on
  // device order or on a serial number written down somewhere.
  j["role"] = FW_ROLE;
  j["w"] = 240; j["h"] = 240; j["shape"] = "round";
  emit(j);
}

void copyStr(char *dst, size_t n, JsonVariantConst v, const char *fallback = "") {
  const char *s = v.is<const char *>() ? v.as<const char *>() : fallback;
  strlcpy(dst, s ? s : "", n);
}

void handleLine(const char *line) {
  JsonDocument j;
  if (deserializeJson(j, line)) return;
  lastHostMs = millis();
  if (!linked) { linked = true; dirty = true; }

  if (j["m"].is<const char *>()) {
    routing = !strcmp(j["m"], "nav");
    dirty = true;
  }
  if (j["nav"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["nav"];
    copyStr(nv.street,   sizeof nv.street,   o["street"]);
    copyStr(nv.maneuver, sizeof nv.maneuver, o["maneuver"], "straight");
    copyStr(nv.unit,     sizeof nv.unit,     o["unit"], "m");
    nv.dist = o["dist"] | nv.dist;
    routing = true;                 // nav data implies a route
    dirty = true;
  }
  if (j["bl"].is<int>()) {
    backlight = constrain((int)j["bl"], 8, 255);
    ledcWrite(PIN_BL, backlight);
  }
  if (j["ping"] | false) sendHello();
}

void pollSerial() {
  static char buf[512];
  static size_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (n) { buf[n] = 0; handleLine(buf); n = 0; }
    } else if (n < sizeof buf - 1) {
      buf[n++] = c;
    } else {
      n = 0;                        // overlong line: drop it rather than wrap
    }
  }
}

// ------------------------------------------------------------------ main ----
void setup() {
#if HAS_PWR_RAILS
  // The rails first, before anything touches the panel. See knob-ui.
  pinMode(PIN_PWR_1, OUTPUT); digitalWrite(PIN_PWR_1, HIGH);
  pinMode(PIN_PWR_2, OUTPUT); digitalWrite(PIN_PWR_2, HIGH);
  delay(200);
#endif

  ledcAttach(PIN_BL, 5000, 8);
  ledcWrite(PIN_BL, backlight);

  gfxOk = gfx->begin();
  gfx->fillScreen(C_BG);
  gfx->flush();

  Serial.begin(115200);
  render();
}

void loop() {
  pollSerial();

  bool nowLinked = lastHostMs && (millis() - lastHostMs < 5000);
  if (nowLinked != linked) { linked = nowLinked; dirty = true; }

  // The hello sent from setup() is always lost: the host has not opened the
  // CDC port that early. Re-announce until something arrives.
  static uint32_t lastHello = 0;
  if (!lastHostMs && millis() - lastHello > 1000) {
    lastHello = millis();
    sendHello();
  }

  static bool splashDone = false;
  if (!splashDone && millis() >= 2000) { splashDone = true; dirty = true; }

  if (dirty) { render(); dirty = false; }
  delay(5);
}
