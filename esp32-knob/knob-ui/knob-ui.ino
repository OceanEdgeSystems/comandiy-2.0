// Context-adaptive control knob for the custom COMAND head unit
// Elecrow CrowPanel 1.28"-HMI (ESP32-S3R8, GC9A01 240x240 round IPS, CST816D)
//
// The host pushes a mode; the knob renders the screen for that mode and sends
// raw input events back. Firmware stays dumb about meaning -- the only screen
// it drives on its own is the menu, because selection has to feel instant and
// a USB round trip per detent does not.
//
// Deliberately NOT a USB HID keyboard. The LIVI-era sketches emulated one so a
// third-party application would react to keycodes; we own the host now, so the
// knob speaks its own protocol over CDC and stops being "a keyboard into
// whatever has focus" the moment it is plugged into a laptop.
//
// Protocol: docs/knob-protocol.md.  Flash with esp32-knob/flash.sh.
// Develop against esp32-knob/knob-sim.py -- no Pi, no car, no dongle needed.

#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include "driver/pulse_cnt.h"

#include "FreeSans9pt7b.h"
#include "FreeSansBold12pt7b.h"
#include "FreeSansBold18pt7b.h"
#include "FreeSansBold24pt7b.h"
// Digits only (0-9), generated with Adafruit's fontconvert from GNU FreeSans:
//   fontconvert FreeSans.ttf 48 48 57 > FreeSans48pt7b.h
// Same family as the rest of the UI, and free to redistribute. Restricting it
// to digits keeps it at ~3.7 KB rather than the ~20 KB a full face would cost,
// which is the whole reason a 48 pt face is affordable at all.
#include "FreeSans48pt7b.h"

#define FW_VERSION "knob-ui 0.21"

#include "star.h"

// ---- CrowPanel 1.28" pin map (README "Board pin map") ----
#define PIN_ENC_A   45
#define PIN_ENC_B   42
#define PIN_ENC_SW  41
#define PIN_BL      46
#define PIN_PWR_1    1      // panel power rails: undocumented, factory source only
#define PIN_PWR_2    2      // both must be high or the panel gets no power
#define PIN_SCLK    10
#define PIN_MOSI    11
#define PIN_DC       3
#define PIN_CS       9
#define PIN_RST     14
#define PIN_TP_SDA   6
#define PIN_TP_SCL   7
#define PIN_TP_INT   5
#define PIN_TP_RST  13

// ---- geometry ----
static const int CX = 120, CY = 120;
static const int R_OUT = 118, R_IN = 109;   // the status ring
// fillArc: 0 deg is 3 o'clock, clockwise. Everything here is measured from 12.
static inline float fromTop(float deg) { return fmodf(270.0f + deg, 360.0f); }

// ---- palette ----
#define C_BG      RGB565(8, 10, 14)
#define C_TEXT    RGB565(236, 238, 242)
#define C_DIM     RGB565(120, 128, 140)
#define C_FAINT   RGB565(52, 58, 68)
#define C_ACCENT  RGB565(64, 158, 255)
#define C_GOOD    RGB565(58, 190, 120)
#define C_WARN    RGB565(240, 176, 64)
#define C_BAD     RGB565(232, 76, 76)

Arduino_DataBus *bus = new Arduino_ESP32SPI(PIN_DC, PIN_CS, PIN_SCLK, PIN_MOSI, GFX_NOT_DEFINED);
Arduino_GFX *panel = new Arduino_GC9A01(bus, PIN_RST, 0 /* rotation */, true /* IPS */);
Arduino_Canvas *gfx = new Arduino_Canvas(240, 240, panel);

// ---------------------------------------------------------------- state ----
// Host-pushed mode. Boot and menu are firmware states, not host modes.
enum Mode { M_IDLE, M_MEDIA, M_NAV, M_CALL, M_RADIO, M_SLEEP };

struct Media {
  char title[48] = "";
  char artist[48] = "";
  char source[20] = "";
  uint32_t pos = 0, dur = 0;
  bool playing = false;
};

// The radio face: a station's own logo, which is the whole point of putting a
// screen on a volume knob. The artwork is pushed by the host rather than held
// in flash, because the stations are a list the driver edits and firmware that
// has to be reflashed to add one is firmware nobody adds a station to.
//
// It arrives RLE-compressed: station logos are flat colour and a few marks,
// so runs are long and a 116x116 image that would be 27 kB raw arrives in one
// or two. Decoded straight to the screen rather than into a framebuffer -- a
// second 27 kB buffer is real money on this board, and drawing a run is a
// fillRect either way.
#define LOGO_MAX 24000

// The transport button on the radio face: where it is drawn and where a touch
// counts as pressing it. The zone is larger than the ring, because a thumb on
// a moving car is not accurate and there is nothing else down there to hit.
#define RADIO_BTN_Y  206
#define RADIO_BTN_R   20
#define RADIO_BTN_TOP 176

struct Radio {
  char name[128] = "";           // a video's title scrolls, so it is kept whole
  uint32_t shown = 0;            // when this name came, for its ticker
  bool playing = false;
  uint8_t *rle = nullptr;        // count, hi, lo -- repeated
  size_t   len = 0;              // bytes expected
  size_t   got = 0;              // bytes received so far
  int      w = 0, h = 0;
  bool ready() const { return rle && len && got >= len && w > 0; }
};

struct Nav {
  char street[48] = "";
  char maneuver[16] = "straight";
  uint32_t dist = 0;
  char unit[6] = "m";
};

// The call button: dial while a number is typed on the panel, hang up once
// there is a call. Same place and same generous zone as the radio's transport,
// for the same reason -- a thumb in a moving car.
#define CALL_BTN_Y   184
#define CALL_BTN_R    32
#define CALL_BTN_TOP 145

struct Call {
  char who[40] = "";
  char num[28] = "";             // shown under a name, when there is one
  char status[16] = "ringing";   // ready | ringing | dialing | active
  uint32_t since = 0;            // millis() when it went active
  uint32_t shown = 0;            // millis() when who/num last changed: the
                                 // scroll starts over from the beginning
};

struct Idle {
  char clock[8] = "--:--";
  float volts = 0;
  bool haveVolts = false;
};

struct Hint {                    // what rotate/press mean right now
  char rot[16] = "";
  char press[16] = "";
};

Mode mode = M_IDLE;

// The star, while the car is still waking up.
//
// Timed rather than frame-counted: this panel's redraw rate depends on what
// else the loop is doing, and an animation measured in frames runs at a
// different speed on a busy boot than on a quiet one.
static const uint32_t SPLASH_IN_MS   = 1400;
static const uint32_t SPLASH_HOLD_MS = 2200;
static const uint32_t SPLASH_OUT_MS  = 800;
static const uint32_t SPLASH_MS = SPLASH_IN_MS + SPLASH_HOLD_MS + SPLASH_OUT_MS;

// Zero means "from power-on", which is the normal case and needs no special
// handling: millis() - 0 is millis(). The host can set it to now to play the
// animation again, which is the only way to see it without power-cycling the
// board -- and the only way to tell "it did not run" from "nobody was looking
// at the moment it did".
uint32_t splashStart  = 0;
uint32_t splashFrames = 0;
// The hold is variable so the host can ask for a long one. Holding the star
// still, for as long as somebody needs to walk over and look at it, is the
// only way to answer "is it not drawing, or was nobody watching?" -- a four
// second animation on a device nobody is standing in front of answers
// neither.
uint32_t splashHoldMs = SPLASH_HOLD_MS;

uint32_t splashTotal() { return SPLASH_IN_MS + splashHoldMs + SPLASH_OUT_MS; }
bool splashActive() { return millis() - splashStart < splashTotal(); }

// Play it from the beginning, or from part way in: entering standby uses the
// fade-out alone, so the star dips out as the screen goes dark and the driver
// can see that the five second hold registered.
void splashPlay(uint32_t hold, uint32_t skip = 0) {
  splashHoldMs = hold;
  splashFrames = 0;
  splashStart  = millis() - skip;
}
Media md; Nav nv; Call cl; Idle id; Hint hint; Radio rd;



uint32_t lastHostMs = 0;         // last line from the host
bool     linked     = false;
uint8_t  backlight  = 255;
bool     haveTouch  = false;
int      tpInitMode = 0;         // which touch init strategy actually worked

bool dirty = true;

// --------------------------------------------------------------- volume ----
// Rotation means volume whenever the menu is closed, because that is the one
// thing done constantly, at speed, without looking. Nothing has to be entered
// or selected to reach it: it is what the knob does when it is doing nothing
// else, and every other state falls back to it.
//
// The level is applied optimistically here and corrected by the host's next
// {"vol":n}. A detent must move the number under your fingers immediately; a
// USB round trip before the display responds feels broken even when it is only
// a few milliseconds.
int      vol        = 30;        // 0-100
bool     muted      = false;
uint32_t volShownMs = 0;         // when the overlay last had something to say
static const uint32_t VOL_HOLD_MS = 1600;

bool volOverlay() { return volShownMs && millis() - volShownMs < VOL_HOLD_MS; }

// Mute pulse. A blink draws attention to the transition rather than to the
// state, and on a dark screen an icon appearing from nothing catches the eye
// the way a warning light does -- too urgent for "the audio is muted". A fade
// carries the same information as motion without ever being an event.
//
// It never reaches zero: the floor keeps the icon legible throughout, so the
// pulse says "still muted" rather than blinking a message you might miss
// between glances.
static const uint32_t MUTE_PULSE_MS  = 2200;
static const float    MUTE_PULSE_MIN = 0.28f;   // dimmest, as a fraction

// Scale an RGB565 colour toward the background. Channels are unpacked, scaled
// and repacked, because a 565 word cannot be multiplied as a whole.
uint16_t dimColour(uint16_t c, float f) {
  if (f >= 1.0f) return c;
  if (f < 0.0f)  f = 0;
  uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
  r = (uint8_t)(r * f); g = (uint8_t)(g * f); b = (uint8_t)(b * f);
  return (uint16_t)((r << 11) | (g << 5) | b);
}

// Raised cosine: no corners at either end, so the turn-around is not a moment
// you can point at. A triangle wave pulses too, but you can see it change
// direction, which is exactly the flicker being avoided.
float mutePulse() {
  float p = (millis() % MUTE_PULSE_MS) / (float)MUTE_PULSE_MS;
  float t = (1.0f - cosf(p * 2.0f * (float)PI)) * 0.5f;      // 0 -> 1 -> 0
  return MUTE_PULSE_MIN + (1.0f - MUTE_PULSE_MIN) * t;
}
void showVol()    { volShownMs = millis(); dirty = true; }

// ----------------------------------------------------------------- menu ----
// A scrolling text list with the focused item pinned to the centre of the
// screen. Rendered and navigated locally: the host is told what happened, not
// asked what to do, because a USB round trip per detent would feel awful.
//
// The list does NOT wrap. With neighbours visible, wrapping puts "Settings"
// directly above "Telephone", which reads as a rendering glitch rather than a
// feature. Ends stop, and the ring shows position instead.
static const int MENU_MAX = 12;
char menuItems[MENU_MAX][20] = {
  "Telephone", "Music", "Radio", "Carplay", "Navigation", "Car", "Settings"
};
int  menuCount = 7;
int  menuSel   = 0;
bool menuOpen  = false;

// The menu closes itself after a few idle seconds, and this is the most
// important line in the interaction model. Without it the knob can be left in
// menu mode: you open it, get distracted, and the next time you reach over to
// turn the volume down you scroll a list instead -- a mode error discovered
// after acting, with your eyes on the road. Relaxing back to volume means the
// knob is only ever wrong for a moment, and whatever you grab it for blind is
// what it does.
uint32_t menuTouchedMs = 0;
static const uint32_t MENU_IDLE_MS = 5000;
void menuKeepAlive() { menuTouchedMs = millis(); }

// Layout is tunable at runtime over the protocol -- {"ui":{"rowh":44}} -- and
// dumped with {"uiget":true}. Reflashing to move a row four pixels is a bad
// way to design a screen you can only judge by looking at it.
struct UiCfg {
  float rowh   = 40;    // pixels between rows
  int   band   = 0;     // focus-band hairlines: 0 off. Kept tunable so
                        // they can be tried again without a reflash.
  int   radius = 104;   // text is clipped to the circle's chord at this radius
  float ease   = 0.35f; // scroll easing per frame
  float near   = 0.5f;  // |offset| below this = focused, large + bright
  float mid    = 1.5f;  // below this = neighbour, medium + dim
  bool  wrap   = false; // ends stop, or the list is endless
  bool  ring   = true;  // position segments around the rim
} ui;

// Continuous, unbounded when wrapping: menuSel is derived from it, so easing
// across the wrap boundary moves one row rather than sweeping the whole list.
float scrollCur = 0, scrollTarget = 0;

static int menuWrapIdx(int i) {
  if (menuCount <= 0) return 0;
  return ((i % menuCount) + menuCount) % menuCount;
}

// ---------------------------------------------------------------- input ----
// The encoder is decoded by the S3's pulse counter (PCNT), full quadrature:
// both pins, both edges, 4 counts per detent. Until 0.19 loop() polled A's
// falling edge and read B there, between redraws: a detent's redraw made the
// next one be seen late, after B had moved too, so turns came out as
// up/down/up/down (2026-11-05: volume went 73 74 73 74 75 74 ...). In hardware
// no edge is missed, and contact bounce on one pin counts +1 -1 and cancels.
// (An interrupt version emitted nothing and used noInterrupts() in loop(),
// which on ESP32 starves the USB task; the counter needs neither.)
static const int COUNTS_PER_DETENT = 4;
pcnt_unit_handle_t encUnit = nullptr;
int      encLast    = 0;          // count already turned into detents
int      lastCLK    = HIGH;
bool     lastSw     = HIGH;
uint32_t swDownMs   = 0;
bool     longFired  = false;
bool     holdFired  = false;
static const uint32_t LONG_MS = 600;
// Standby. Deliberately far past the menu's threshold: this is the one
// gesture that takes the whole car's screen away, and it should not be
// reachable by holding the knob a moment too long.
static const uint32_t HOLD_MS = 5000;

void emit(const JsonDocument &doc) {
  serializeJson(doc, Serial);
  Serial.println();
}

void sendRotate(int8_t d) {
  JsonDocument j; j["e"] = "rot"; j["d"] = d; emit(j);
}
void sendPress(const char *kind) {
  JsonDocument j; j["e"] = "press"; j["t"] = kind; emit(j);
}
void sendMenu(const char *action, int sel) {
  JsonDocument j; j["e"] = "menu"; j["a"] = action;
  if (sel >= 0 && sel < menuCount) { j["sel"] = sel; j["label"] = menuItems[sel]; }
  emit(j);
}
void sendHello() {
  JsonDocument j; j["e"] = "hello"; j["fw"] = FW_VERSION;
  j["w"] = 240; j["h"] = 240; j["shape"] = "round";
  // Two of these boards can be on one bus, both 0x303A, and either may
  // enumerate first. The role is how the host tells them apart without
  // depending on device order or a serial number written down somewhere.
  j["role"] = "knob";
  j["touch"] = haveTouch;
  j["vol"] = vol;
  j["muted"] = muted;
  j["tpinit"] = tpInitMode;
  // How much of a logo is held, so the host can see from the outside whether a
  // transfer arrived rather than having to be told by someone looking at the
  // screen.
  j["logo"] = (uint32_t)rd.got;
  j["logolen"] = (uint32_t)rd.len;
  j["ooono"] = true;
  emit(j);
}

#include "ooono.h"

void menuMove(int delta) {
  if (menuCount <= 0) return;
  float want = scrollTarget + delta;
  if (!ui.wrap) want = constrain(want, 0.0f, (float)(menuCount - 1));
  if (fabsf(want - scrollTarget) < 0.001f) return;      // already at an end
  scrollTarget = want;
  menuSel = menuWrapIdx((int)lroundf(scrollTarget));
  dirty = true;
  sendMenu("move", menuSel);
}

void menuActivate() {
  sendMenu("activate", menuSel);
  menuOpen = false;
  dirty = true;
}

void openMenu() {
  menuOpen = true;
  menuKeepAlive();
  scrollTarget = scrollCur = menuSel;
  dirty = true;
  sendMenu("open", menuSel);
}

void onRotate(int8_t d) {
  if (mode == M_SLEEP) return;         // see onShortPress
  if (menuOpen) { menuKeepAlive(); menuMove(d); return; }

  // Default state: volume. Turning while muted un-mutes and sets the level,
  // which is what reaching for the knob obviously means.
  if (muted) { muted = false; sendPress("unmute"); }
  vol = constrain(vol + d, 0, 100);
  showVol();
  sendRotate(d);
}

void onShortPress() {
  // Asleep, a press is a press to wake and nothing else -- not a mute, which
  // is what it means every other time. Reaching for a dark car's knob means
  // "come back", and making that the five-second hold would be asking the
  // driver to remember which long press does what.
  if (mode == M_SLEEP) { sendPress("wake"); return; }
  if (menuOpen) { menuActivate(); return; }

  // Mute, as on this car's original COMAND and on essentially every car knob.
  // Nothing to learn, and it keeps the menu behind a deliberate long press so
  // the knob cannot be knocked into another mode while driving.
  muted = !muted;
  showVol();
  sendPress(muted ? "mute" : "unmute");
}

void onLongPress() {
  if (mode == M_SLEEP) return;         // the menu is not reachable in standby
  if (menuOpen) {
    menuKeepAlive();
    menuOpen = false;
    dirty = true;
    sendMenu("cancel", -1);
  } else if (menuCount) {
    openMenu();
  } else {
    sendPress("long");
  }
}

void onHold() {
  // Reached by holding through the long press, so the menu this gesture
  // opened on the way past is still up. It is taken down here rather than
  // left for the host's reply: the screen is about to go dark either way, and
  // a menu still open underneath would be what the knob came back to.
  if (menuOpen) {
    menuOpen = false;
    dirty = true;
    sendMenu("cancel", -1);
  }
  sendPress("hold");
}

void encoderBegin() {
  pcnt_unit_config_t uc = {};
  uc.low_limit = -1000; uc.high_limit = 1000;
  ESP_ERROR_CHECK(pcnt_new_unit(&uc, &encUnit));
  pcnt_glitch_filter_config_t fc = {};
  fc.max_glitch_ns = 10000;                    // ~the most the filter takes
  ESP_ERROR_CHECK(pcnt_unit_set_glitch_filter(encUnit, &fc));
  pcnt_chan_config_t ca = {}; ca.edge_gpio_num = PIN_ENC_A; ca.level_gpio_num = PIN_ENC_B;
  pcnt_chan_config_t cb = {}; cb.edge_gpio_num = PIN_ENC_B; cb.level_gpio_num = PIN_ENC_A;
  pcnt_channel_handle_t chA, chB;
  ESP_ERROR_CHECK(pcnt_new_channel(encUnit, &ca, &chA));
  ESP_ERROR_CHECK(pcnt_new_channel(encUnit, &cb, &chB));
  // Same sense as before: A falling while B is high is +1.
  pcnt_channel_set_edge_action(chA, PCNT_CHANNEL_EDGE_ACTION_DECREASE, PCNT_CHANNEL_EDGE_ACTION_INCREASE);
  pcnt_channel_set_level_action(chA, PCNT_CHANNEL_LEVEL_ACTION_KEEP, PCNT_CHANNEL_LEVEL_ACTION_INVERSE);
  pcnt_channel_set_edge_action(chB, PCNT_CHANNEL_EDGE_ACTION_INCREASE, PCNT_CHANNEL_EDGE_ACTION_DECREASE);
  pcnt_channel_set_level_action(chB, PCNT_CHANNEL_LEVEL_ACTION_KEEP, PCNT_CHANNEL_LEVEL_ACTION_INVERSE);
  ESP_ERROR_CHECK(pcnt_unit_enable(encUnit));
  ESP_ERROR_CHECK(pcnt_unit_clear_count(encUnit));
  ESP_ERROR_CHECK(pcnt_unit_start(encUnit));
}

void pollEncoder() {
  int c = 0;
  if (encUnit && pcnt_unit_get_count(encUnit, &c) == ESP_OK) {
    int steps = (c - encLast) / COUNTS_PER_DETENT;   // whole detents only
    if (steps) {
      encLast += steps * COUNTS_PER_DETENT;
      int8_t d = steps > 0 ? 1 : -1;
      for (int n = abs(steps); n > 0; n--) onRotate(d);
    }
    if (abs(encLast) > 500 && c == encLast) {  // stay clear of the limits, at a detent
      pcnt_unit_clear_count(encUnit);
      encLast = 0;
    }
  }

  bool sw = digitalRead(PIN_ENC_SW);
  uint32_t now = millis();
  if (lastSw == HIGH && sw == LOW) {           // pressed
    swDownMs = now; longFired = false; holdFired = false;
    // A warning is up: this press is the Ooono button, not a mute.
    if (alertActive() && mode != M_SLEEP) ooonoButtonDown();
  } else if (oooPressing) {
    if (lastSw == LOW && sw == HIGH) ooonoButtonUp(now - swDownMs);
  } else if (sw == LOW && !longFired && now - swDownMs >= LONG_MS) {
    longFired = true; onLongPress();
  } else if (sw == LOW && !holdFired && now - swDownMs >= HOLD_MS) {
    holdFired = true; onHold();
  } else if (lastSw == LOW && sw == HIGH) {    // released
    if (!longFired && now - swDownMs > 25) onShortPress();
  }
  lastSw = sw;
}

// ---------------------------------------------------------------- touch ----
// CST816D on I2C. No library for it is installed and it needs about fifteen
// lines, so this is a direct register read.
//
// The gesture register is not trusted: its codes differ between CST816S and
// CST816D firmware revisions and between vendor datasheets. Raw coordinates
// are unambiguous, so drag and tap are derived from them here.
#define TP_ADDR 0x15

bool tpDown = false;
int  tpY0 = 0, tpYLast = 0;
// Horizontal travel is tracked too: on the radio face a sideways swipe is how
// stations are changed, which is the one gesture a knob cannot express -- it
// turns for volume, and giving that up to walk a list would cost more than it
// bought.
int  tpX0 = 0, tpXLast = 0;
float tpScroll0 = 0;
uint32_t tpDownMs = 0;
int  tpMoved = 0;
bool tpDebug = false;              // {"dbg":true} from the host turns this on

static bool tpProbe(uint32_t forMs) {
  uint32_t t0 = millis();
  do {
    Wire.beginTransmission(TP_ADDR);
    if (Wire.endTransmission() == 0) return true;
    delay(10);
  } while (millis() - t0 < forMs);
  return false;
}

// DANGER, learned the hard way: driving PIN_TP_RST (13) and PIN_TP_INT (5)
// after the display is initialised blanks the panel and it never comes back.
// The board pin map in the README is not trustworthy -- it was already wrong
// about the upload port -- so one of those two pins is evidently not what it
// is labelled, and touching it takes the display with it.
//
// Nothing here drives any GPIO any more. This probes the I2C bus and nothing
// else, and it only runs when explicitly asked via {"tpinit":true}. Until the
// board's schematic settles what is actually wired where, that stays true.
bool touchBegin() {
  Wire.end();
  if (!Wire.begin(PIN_TP_SDA, PIN_TP_SCL, 100000)) { tpInitMode = 0; return false; }
  if (tpProbe(200)) { tpInitMode = 1; return true; }
  tpInitMode = 0;
  return false;
}

// Probe every address on a given pin pair and report what answers. The touch
// controller did not ACK where the README's pin map says it should, and
// guessing pin order or address one flash at a time is slow.
void i2cScan(int sda, int scl) {
  Wire.end();
  Wire.begin(sda, scl, 100000);
  delay(20);

  JsonDocument j;
  j["e"] = "i2c"; j["sda"] = sda; j["scl"] = scl;
  JsonArray found = j["addrs"].to<JsonArray>();
  for (uint8_t a = 0x08; a < 0x78; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) found.add(a);
  }
  emit(j);
}

// Returns true while a finger is on the glass, filling x/y.
bool touchRead(int &x, int &y) {
  Wire.beginTransmission(TP_ADDR);
  Wire.write(0x01);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)TP_ADDR, (uint8_t)6) != 6) return false;

  uint8_t r[6];
  for (int i = 0; i < 6; i++) r[i] = Wire.read();
  uint8_t fingers = r[1] & 0x0F;               // 0x02: finger count
  if (!fingers) return false;
  x = ((r[2] & 0x0F) << 8) | r[3];             // 0x03/0x04
  y = ((r[4] & 0x0F) << 8) | r[5];             // 0x05/0x06
  return true;
}

void pollTouch() {
  if (!haveTouch) return;
  // 60 Hz is plenty for a finger, and polling every loop iteration would put
  // a 6-byte I2C transaction on the bus thousands of times a second.
  static uint32_t lastPoll = 0;
  if (millis() - lastPoll < 16) return;
  lastPoll = millis();

  int x, y;
  bool down = touchRead(x, y);

  if (tpDebug && down != tpDown) {
    JsonDocument j; j["e"] = "touch"; j["t"] = down ? "down" : "up";
    j["x"] = x; j["y"] = down ? y : tpYLast; emit(j);
  }

  if (down && !tpDown) {                       // finger down
    tpDown = true; tpY0 = tpYLast = y; tpScroll0 = scrollCur;
    tpX0 = tpXLast = x;
    tpDownMs = millis(); tpMoved = 0;
  } else if (down && tpDown) {                 // dragging
    // Both axes: a sideways drag is movement too, and counting only the
    // vertical made a swipe look like a tap to everything downstream.
    tpMoved = max(tpMoved, max(abs(y - tpY0), abs(x - tpX0)));
    if (menuOpen) {
      // Drag follows the finger one-to-one: content moves with the touch.
      scrollCur = tpScroll0 - (y - tpY0) / ui.rowh;
      if (!ui.wrap) scrollCur = constrain(scrollCur, -0.4f, menuCount - 1 + 0.4f);
      scrollTarget = scrollCur;
      dirty = true;
    }
    tpYLast = y; tpXLast = x;
  } else if (!down && tpDown) {                // released
    tpDown = false;
    bool tap = tpMoved < 10 && (millis() - tpDownMs) < 400;

    if (!menuOpen) {
      // A sideways swipe on the radio face steps to the next station. Judged
      // against the vertical travel as well as a threshold, so a slightly
      // slanted press is still a press: a gesture that fires on any smudge is
      // worse than one that occasionally has to be repeated.
      int dx = tpXLast - tpX0, dy = tpYLast - tpY0;
      if (mode == M_RADIO && abs(dx) >= 45 && abs(dx) > abs(dy) * 2) {
        JsonDocument r;
        r["e"] = "radio";
        r["step"] = dx < 0 ? 1 : -1;      // swipe left goes forward, as a page does
        emit(r);
        return;
      }

      // On the radio face the bottom of the screen is a transport button, and
      // pressing a control that is drawn on the screen should do what it says
      // rather than open a menu. Everywhere else still opens the menu, so the
      // gesture that reaches everything is not lost.
      if (tap && mode == M_CALL && tpYLast >= CALL_BTN_TOP &&
          strcmp(cl.status, "ringing")) {
        // Dial is shown as taken at once -- the host's "dialing" arrives a
        // moment later and says the same thing. Hang up is not: the call is
        // over when the phone says so, and a screen that claims it first is
        // wrong whenever the phone disagrees.
        bool dial = !strcmp(cl.status, "ready");
        if (dial) { strlcpy(cl.status, "dialing", sizeof cl.status); dirty = true; }
        JsonDocument r; r["e"] = "call"; r["a"] = dial ? "dial" : "hangup"; emit(r);
      } else if (tap && mode == M_RADIO && tpYLast >= RADIO_BTN_TOP) {
        // Moved locally first and corrected by the host's reply: a button that
        // waits for a USB round trip before it changes feels broken even when
        // the wait is milliseconds.
        rd.playing = !rd.playing;
        dirty = true;
        JsonDocument r; r["e"] = "radio"; r["toggle"] = true; emit(r);
      } else if (tap) {
        openMenu();
      }
      return;
    }
    if (tap) {
      // Tap the centred item to activate it; tap a neighbour to bring it in.
      int row = (int)lroundf(scrollCur + (tpYLast - CY) / ui.rowh);
      if (!ui.wrap) row = constrain(row, 0, menuCount - 1);
      if (menuWrapIdx(row) == menuSel) menuActivate();
      else menuMove(row - (int)lroundf(scrollCur));
    } else {
      scrollTarget = lroundf(scrollCur);      // snap to the nearest row
      if (!ui.wrap) scrollTarget = constrain(scrollTarget, 0.0f, (float)(menuCount - 1));
      int want = menuWrapIdx((int)scrollTarget);
      if (want != menuSel) { menuSel = want; sendMenu("move", menuSel); }
      dirty = true;
    }
  }
}

// ------------------------------------------------------------- protocol ----
void applyMode(const char *m) {
  Mode was = mode;
  if      (!strcmp(m, "media")) mode = M_MEDIA;
  else if (!strcmp(m, "nav"))   mode = M_NAV;
  else if (!strcmp(m, "call"))  mode = M_CALL;
  else if (!strcmp(m, "idle"))  mode = M_IDLE;
  else if (!strcmp(m, "radio")) mode = M_RADIO;
  else if (!strcmp(m, "sleep")) mode = M_SLEEP;
  else if (!strcmp(m, "menu"))  { openMenu(); return; }
  if (mode != was) { menuOpen = false; dirty = true; }
  // Going dark is worth seeing. A five second hold has no feedback of its own
  // -- you are holding a knob, and nothing happens for five seconds -- so the
  // star fading out is the car saying it heard. The backlight stays on until
  // the fade finishes, handled in loop(), or there would be nothing to see.
  if (mode == M_SLEEP && was != M_SLEEP) {
    splashPlay(0, SPLASH_IN_MS);       // the fade-out alone
  }
  // And the whole animation on the way back, which is the startup the driver
  // actually sees day to day: the car is rarely watched while it boots.
  if (mode != M_SLEEP && was == M_SLEEP) {
    ledcWrite(PIN_BL, backlight);
    splashPlay(SPLASH_HOLD_MS);
  }
}

void copyStr(char *dst, size_t n, JsonVariantConst v, const char *fallback = "") {
  const char *s = v.is<const char *>() ? v.as<const char *>() : fallback;
  strlcpy(dst, s ? s : "", n);
}

// Base64 in, raw bytes out. The logo arrives as text because it shares a
// line-delimited JSON channel with everything else, and a binary side-channel
// would need its own framing and its own way of resynchronising after a
// half-written line.
static int b64val(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;                                   // '=' padding and anything else
}

static size_t b64decode(const char *in, uint8_t *out, size_t outMax) {
  uint32_t acc = 0; int bits = 0; size_t n = 0;
  for (const char *p = in; *p; p++) {
    int v = b64val(*p);
    if (v < 0) continue;
    acc = (acc << 6) | (uint32_t)v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (n < outMax) out[n++] = (uint8_t)((acc >> bits) & 0xFF);
    }
  }
  return n;
}

// A new logo is coming: forget the old one before the first chunk lands, so a
// transfer that is interrupted leaves no logo rather than half of two.
void logoBegin(int w, int h, size_t len) {
  free(rd.rle);
  rd.rle = nullptr;
  rd.len = rd.got = 0;
  rd.w = rd.h = 0;
  if (len == 0 || len > LOGO_MAX) return;      // refuse rather than truncate
  rd.rle = (uint8_t *)malloc(len);
  if (!rd.rle) return;
  rd.len = len; rd.w = w; rd.h = h;
  dirty = true;
}

void logoChunk(const char *b64) {
  if (!rd.rle || rd.got >= rd.len) return;
  rd.got += b64decode(b64, rd.rle + rd.got, rd.len - rd.got);
  if (rd.got >= rd.len) dirty = true;          // the last chunk completes it
  // Acknowledge every chunk. The host waits for this before sending the next,
  // which is the only flow control on a link that has none: without it the
  // host outruns a board that reads its port once a frame.
  JsonDocument r;
  r["e"] = "logoack";
  r["got"] = (uint32_t)rd.got;
  r["len"] = (uint32_t)rd.len;
  emit(r);
}

void handleLine(const char *line) {
  JsonDocument j;
  if (deserializeJson(j, line)) return;        // ignore malformed quietly
  lastHostMs = millis();
  if (!linked) { linked = true; dirty = true; }

  if (j["m"].is<const char *>()) applyMode(j["m"]);
  if (j["ooono"].is<JsonObjectConst>()) ooonoCommand(j["ooono"]);
  if (j["alert"].is<JsonObjectConst>() || (j.containsKey("alert") && j["alert"].isNull())) alertCommand(j["alert"]);

  // Volume from the host. This is the authority: the local value moved
  // optimistically on the detent and may have drifted, or the level may have
  // changed somewhere else entirely -- the steering wheel, the phone. Showing
  // the overlay on a host-driven change is deliberate: the number should
  // appear whoever turned it, so the panel never disagrees with the speakers.
  if (j["vol"].is<int>()) {
    int v = constrain((int)j["vol"], 0, 100);
    if (v != vol) { vol = v; showVol(); }
  }
  if (j["muted"].is<bool>()) {
    bool m = j["muted"];
    if (m != muted) { muted = m; showVol(); }
  }

  if (j["media"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["media"];
    copyStr(md.title,  sizeof md.title,  o["title"]);
    copyStr(md.artist, sizeof md.artist, o["artist"]);
    copyStr(md.source, sizeof md.source, o["source"]);
    md.pos = o["pos"] | md.pos;
    md.dur = o["dur"] | md.dur;
    md.playing = o["playing"] | md.playing;
    dirty = true;
  }
  if (j["radio"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["radio"];
    if (o["name"].is<const char *>()) {
      char old[sizeof rd.name];
      strlcpy(old, rd.name, sizeof old);
      copyStr(rd.name, sizeof rd.name, o["name"]);
      if (strcmp(old, rd.name)) rd.shown = millis();
    }
    if (o["playing"].is<bool>()) rd.playing = o["playing"];
    // A logo header and its chunks are separate messages: one line carrying
    // 24 kB of base64 would not fit the read buffer, and a board that has to
    // buffer a whole image before it can parse anything is a board that drops
    // the volume events arriving while it does.
    if (o["logo"].is<JsonObjectConst>()) {
      JsonObjectConst L = o["logo"];
      logoBegin(L["w"] | 0, L["h"] | 0, (size_t)(L["bytes"] | 0));
    }
    if (o["chunk"].is<const char *>()) logoChunk(o["chunk"]);
    dirty = true;
  }

  if (j["nav"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["nav"];
    copyStr(nv.street,   sizeof nv.street,   o["street"]);
    copyStr(nv.maneuver, sizeof nv.maneuver, o["maneuver"], "straight");
    copyStr(nv.unit,     sizeof nv.unit,     o["unit"], "m");
    nv.dist = o["dist"] | nv.dist;
    dirty = true;
  }
  if (j["call"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["call"];
    bool wasActive = !strcmp(cl.status, "active");
    char oldWho[sizeof cl.who], oldNum[sizeof cl.num];
    strlcpy(oldWho, cl.who, sizeof oldWho);
    strlcpy(oldNum, cl.num, sizeof oldNum);
    copyStr(cl.who,    sizeof cl.who,    o["who"]);
    copyStr(cl.num,    sizeof cl.num,    o["num"]);
    if (strcmp(oldWho, cl.who) || strcmp(oldNum, cl.num)) cl.shown = millis();
    copyStr(cl.status, sizeof cl.status, o["status"], "ringing");
    // The timer is counted here. Hands-free does not report a call's age, so
    // the host has nothing better than the moment it saw the call connect --
    // which is the moment this sees it too. secs, if sent, wins.
    if (o["secs"].is<int>()) cl.since = millis() - (uint32_t)o["secs"] * 1000UL;
    else if (!wasActive && !strcmp(cl.status, "active")) cl.since = millis();
    dirty = true;
  }
  if (j["idle"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["idle"];
    copyStr(id.clock, sizeof id.clock, o["clock"], "--:--");
    id.volts = o["volts"] | id.volts;
    if (o["volts"].is<float>() || o["volts"].is<int>()) id.haveVolts = true;
    dirty = true;
  }
  if (j["hint"].is<JsonObjectConst>()) {
    copyStr(hint.rot,   sizeof hint.rot,   j["hint"]["rot"]);
    copyStr(hint.press, sizeof hint.press, j["hint"]["press"]);
    dirty = true;
  }
  if (j["menu"].is<JsonArrayConst>()) {
    menuCount = 0;
    for (JsonVariantConst v : j["menu"].as<JsonArrayConst>()) {
      if (menuCount >= MENU_MAX) break;
      strlcpy(menuItems[menuCount++], v.as<const char *>(), 20);
    }
    if (menuSel >= menuCount) menuSel = max(0, menuCount - 1);
    scrollTarget = scrollCur = menuSel;
    dirty = true;
  }
  if (j["sel"].is<int>()) {                    // host moves the selection
    menuSel = constrain((int)j["sel"], 0, max(0, menuCount - 1));
    scrollTarget = menuSel;
    scrollCur = menuSel;
    dirty = true;
  }
  if (j["splash"].is<bool>() && j["splash"]) {
    splashPlay(SPLASH_HOLD_MS);
    dirty = true;
  } else if (j["splash"].is<int>()) {
    splashPlay((uint32_t)j["splash"] * 1000UL);
    dirty = true;
  }
  if (j["bl"].is<int>()) {
    backlight = constrain((int)j["bl"], 8, 255);
    ledcWrite(PIN_BL, backlight);
  }
  if (j["ui"].is<JsonObjectConst>()) {
    JsonObjectConst o = j["ui"];
    ui.rowh   = o["rowh"]   | ui.rowh;
    ui.band   = o["band"]   | ui.band;
    ui.radius = o["radius"] | ui.radius;
    ui.ease   = o["ease"]   | ui.ease;
    ui.near   = o["near"]   | ui.near;
    ui.mid    = o["mid"]    | ui.mid;
    ui.wrap   = o["wrap"]   | ui.wrap;
    ui.ring   = o["ring"]   | ui.ring;
    ui.rowh   = constrain(ui.rowh, 16.0f, 80.0f);
    ui.ease   = constrain(ui.ease, 0.05f, 1.0f);
    if (!ui.wrap) scrollTarget = constrain(scrollTarget, 0.0f, (float)(menuCount - 1));
    dirty = true;
  }
  if (j["uiget"].is<bool>()) {
    JsonDocument r; r["e"] = "ui";
    r["rowh"] = ui.rowh; r["band"] = ui.band; r["radius"] = ui.radius;
    r["ease"] = ui.ease; r["near"] = ui.near; r["mid"] = ui.mid;
    r["wrap"] = ui.wrap; r["ring"] = ui.ring;
    emit(r);
  }
  if (j["dbg"].is<bool>()) tpDebug = j["dbg"];
  if (j["i2csweep"].is<bool>()) {
    // Nothing answered on the pins the README documents, so sweep every pin
    // that is safe to bit-bang. Excluded deliberately: 19/20 (USB, and losing
    // them loses this very link), 26-37 (octal flash and PSRAM), the SPI pins
    // the display is on, and the strapping pins.
    // 5 and 13 are excluded: driving them after display init blanks the panel
    // permanently, so whatever they really are, the sweep must not touch them.
    static const int CAND[] = {1, 2, 4, 6, 7, 8, 12, 15, 16, 17, 18,
                               21, 38, 39, 40, 43, 44, 47};
    static const int NCAND = sizeof(CAND) / sizeof(CAND[0]);
    static const uint8_t PROBE[] = {0x15, 0x14, 0x2E, 0x38, 0x5D};

    JsonDocument st; st["e"] = "sweep"; st["t"] = "start"; st["pins"] = NCAND; emit(st);
    int hits = 0;
    for (int a = 0; a < NCAND; a++) {
      for (int b = 0; b < NCAND; b++) {
        if (a == b) continue;
        Wire.end();
        if (!Wire.begin(CAND[a], CAND[b], 100000)) continue;
        delay(2);
        for (unsigned k = 0; k < sizeof(PROBE); k++) {
          Wire.beginTransmission(PROBE[k]);
          if (Wire.endTransmission() == 0) {
            JsonDocument h; h["e"] = "sweephit";
            h["sda"] = CAND[a]; h["scl"] = CAND[b]; h["addr"] = PROBE[k];
            emit(h); hits++;
          }
        }
      }
    }
    Wire.end();
    haveTouch = touchBegin();
    JsonDocument d; d["e"] = "sweepdone"; d["hits"] = hits; emit(d);
  }
  if (j["tpinit"].is<bool>()) {
    haveTouch = touchBegin();
    JsonDocument r; r["e"] = "tpinit"; r["touch"] = haveTouch;
    r["mode"] = tpInitMode; emit(r);
  }
  if (j["i2cscan"].is<bool>()) {
    // Try the documented pins, then the same pair swapped.
    i2cScan(PIN_TP_SDA, PIN_TP_SCL);
    i2cScan(PIN_TP_SCL, PIN_TP_SDA);
    haveTouch = touchBegin();                  // restore the working config
    JsonDocument r; r["e"] = "i2cdone"; r["touch"] = haveTouch; emit(r);
  }
  if (j["ping"].is<bool>()) sendHello();
}

void pollSerial() {
  // Long enough for a logo chunk: 400 base64 characters plus its JSON wrapper.
  // An overlong line resyncs rather than truncating, so a buffer that is too
  // small does not corrupt an image, it loses the whole transfer -- which is
  // worse to diagnose and just as broken.
  static char buf[768];
  static size_t n = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (n) { buf[n] = 0; handleLine(buf); n = 0; }
    } else if (n < sizeof(buf) - 1) {
      buf[n++] = c;
    } else {
      n = 0;                                   // overlong line, resync
    }
  }
}

// ----------------------------------------------------------- draw utils ----
// Truncating measure: fills `out` with as much of `s` as fits in maxW.
void fitText(const char *s, const GFXfont *f, int maxW, char *out, size_t outN,
             uint16_t *wOut, int16_t *x1Out, int16_t *y1Out, uint16_t *hOut) {
  gfx->setFont(f);
  strlcpy(out, s, outN);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(out, 0, 0, &x1, &y1, &w, &h);

  // Too wide: drop one character at a time, re-cutting from the original each
  // pass so the trailing ".." never eats itself.
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

// Centred on a row: vertically about cy, not on a baseline.
void textRow(const char *s, int cy, const GFXfont *f, uint16_t color, int maxW) {
  if (!s || !*s) return;
  char tmp[64]; uint16_t w, h; int16_t x1, y1;
  fitText(s, f, maxW, tmp, sizeof tmp, &w, &x1, &y1, &h);
  gfx->setTextColor(color);
  gfx->setCursor(CX - w / 2 - x1, cy - y1 - h / 2);
  gfx->print(tmp);
}

void ring(float fromDeg, float sweepDeg, uint16_t color, int rOut = R_OUT, int rIn = R_IN) {
  if (sweepDeg <= 0.05f) return;
  if (sweepDeg >= 359.9f) sweepDeg = 359.9f;
  gfx->fillArc(CX, CY, rOut, rIn, fromTop(fromDeg), fromTop(fromDeg + sweepDeg), color);
}

// Maneuver arrow, drawn from a local-space polygon rotated to face `deg`.
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
  return 0;
}

void topLabel(const char *s, uint16_t color = C_DIM) {
  textCentered(s, 46, &FreeSans9pt7b, color, 150);
}

void hintRow() {
  if (!*hint.rot && !*hint.press) return;
  char line[40];
  if (*hint.rot && *hint.press) snprintf(line, sizeof line, "%s / %s", hint.rot, hint.press);
  else strlcpy(line, *hint.rot ? hint.rot : hint.press, sizeof line);
  textCentered(line, 208, &FreeSans9pt7b, C_FAINT, 170);
}

// -------------------------------------------------------------- screens ----
void drawMenu() {
  if (!menuCount) return;

  // Focus-band hairlines, off by default. They were meant to read as a wheel
  // passing under a fixed window, but the focused row is already the largest
  // and brightest thing on screen, so the lines only added furniture. Left
  // behind the tunable rather than deleted, so `band 20` brings them back
  // without a reflash.
  if (ui.band > 0) {
    gfx->drawFastHLine(CX - 92, CY - ui.band, 184, C_FAINT);
    gfx->drawFastHLine(CX - 92, CY + ui.band, 184, C_FAINT);
  }

  // Walk a window of slots around the current position. With wrap on, slot
  // indices run past the ends and fold back; with it off they are skipped.
  int span = (int)(120.0f / ui.rowh) + 1;
  int base = (int)floorf(scrollCur);
  for (int k = base - span; k <= base + span + 1; k++) {
    if (!ui.wrap && (k < 0 || k >= menuCount)) continue;
    int idx = menuWrapIdx(k);

    float off = k - scrollCur;
    int   y   = CY + (int)lroundf(off * ui.rowh);
    int   dy  = y - CY;
    if (abs(dy) > 100) continue;               // off the glass

    // Usable width is the circle's chord at this height, not the full 240.
    float halfChord = sqrtf(fmaxf(0.0f, (float)ui.radius * ui.radius - (float)dy * dy));
    int maxW = (int)(halfChord * 2) - 10;
    if (maxW < 30) continue;

    float d = fabsf(off);
    const GFXfont *f = &FreeSans9pt7b;
    uint16_t col = C_FAINT;
    if (d < ui.near)     { f = &FreeSansBold18pt7b; col = C_TEXT; }
    else if (d < ui.mid) { f = &FreeSansBold12pt7b; col = C_DIM;  }

    textRow(menuItems[idx], y, f, col, maxW);
  }

  // Position on the rim: one segment per item, the selected one accented.
  if (ui.ring) {
    float seg = 360.0f / menuCount;
    ring(0, 360, C_FAINT, R_OUT, R_OUT - 3);
    ring(seg * menuSel + 1.5f, seg - 3.0f, C_ACCENT, R_OUT, R_OUT - 5);
  }

  if (!linked) {                               // developing without a host
    gfx->fillCircle(CX, 226, 3, C_WARN);
  }
}

// Play and pause, drawn rather than typed: this board has no glyph for either
// in the fonts it carries.
void transportGlyph(int cx, int cy, bool playing, uint16_t color) {
  if (playing) {
    gfx->fillRect(cx - 7, cy - 8, 5, 16, color);
    gfx->fillRect(cx + 2, cy - 8, 5, 16, color);
  } else {
    gfx->fillTriangle(cx - 6, cy - 9, cx - 6, cy + 9, cx + 8, cy, color);
  }
}

// Decode straight to the panel. Runs are horizontal and wrap at the image
// width, so a run crossing the right edge is drawn as two.
//
// bright scales every colour on the way out, which is what makes a fade
// possible without a second copy of the image or any blending the panel does
// not have: the same bytes, drawn darker.
void drawRLE(const uint8_t *rle, size_t len, int w, int h,
             int ox, int oy, float bright = 1.0f) {
  int x = 0, y = 0;
  for (size_t p = 0; p + 2 < len; p += 3) {
    int n = rle[p];
    uint16_t c = ((uint16_t)rle[p + 1] << 8) | rle[p + 2];
    if (bright < 1.0f) c = dimColour(c, bright);
    while (n > 0 && y < h) {
      int run = w - x;
      if (run > n) run = n;
      gfx->fillRect(ox + x, oy + y, run, 1, c);
      x += run; n -= run;
      if (x >= w) { x = 0; y++; }
    }
  }
}


void drawSplash() {
  splashFrames++;
  uint32_t t = millis() - splashStart;
  float bright;
  if (t < SPLASH_IN_MS) {
    // Squared, so it comes up out of the black slowly and arrives quickly --
    // linear reads as a lamp being switched on through a dimmer.
    float f = t / (float)SPLASH_IN_MS;
    bright = f * f;
  } else if (t < SPLASH_IN_MS + splashHoldMs) {
    bright = 1.0f;
  } else {
    bright = 1.0f - (t - SPLASH_IN_MS - splashHoldMs) / (float)SPLASH_OUT_MS;
  }
  gfx->fillScreen(0x0000);
  drawRLE(STAR_RLE, STAR_LEN, STAR_W, STAR_H,
          CX - STAR_W / 2, CY - STAR_H / 2, bright);
}

extern bool marqueeOn;
void marquee(const char *s, int baselineY, const GFXfont *f, uint16_t color,
             int maxW, uint32_t since);

void drawRadio() {
  marqueeOn = false;
  if (rd.ready()) {
    drawRLE(rd.rle, rd.len, rd.w, rd.h, CX - rd.w / 2, 94 - rd.h / 2);
  } else {
    // No artwork yet, or a transfer that never finished: the name still says
    // what is playing, which is the thing that matters.
    textRow(*rd.name ? rd.name : "Radio", 94, &FreeSansBold18pt7b, C_TEXT, 170);
  }

  marquee(rd.name, 173, &FreeSans9pt7b, C_DIM, 186, rd.shown);
  ring(0, 360, C_FAINT);             // after the marquee, whose masks would bite it

  // The transport is a control, not a readout, so it is drawn as one: a ring
  // says where to press. Without it the glyph is just a symbol, and nothing on
  // the screen suggests the panel does anything when touched.
  gfx->drawCircle(CX, RADIO_BTN_Y, RADIO_BTN_R, C_FAINT);
  transportGlyph(CX, RADIO_BTN_Y, rd.playing, rd.playing ? C_GOOD : C_DIM);
}

void drawMedia() {
  float p = md.dur ? (float)md.pos / (float)md.dur : 0.0f;
  ring(0, 360, C_FAINT);
  ring(0, 360.0f * constrain(p, 0.0f, 1.0f), C_ACCENT);

  topLabel(*md.source ? md.source : "Media");
  textCentered(md.title,  118, &FreeSansBold18pt7b, C_TEXT, 186);
  textCentered(md.artist, 148, &FreeSans9pt7b,      C_DIM,  186);

  transportGlyph(CX, 176, md.playing, C_DIM);
  if (md.dur) {
    char t[24];
    snprintf(t, sizeof t, "%lu:%02lu / %lu:%02lu",
             (unsigned long)md.pos / 60, (unsigned long)md.pos % 60,
             (unsigned long)md.dur / 60, (unsigned long)md.dur % 60);
    textCentered(t, 206, &FreeSans9pt7b, C_FAINT, 170);
  }
}

void drawNav() {
  ring(0, 360, C_FAINT);
  ring(0, 360, C_GOOD, R_OUT, R_OUT - 4);
  arrow(CX, 96, 74, maneuverAngle(nv.maneuver), C_GOOD);

  char d[24];
  if (!strcmp(nv.unit, "km")) snprintf(d, sizeof d, "%.1f km", nv.dist / 10.0f);
  else                        snprintf(d, sizeof d, "%lu %s", (unsigned long)nv.dist, nv.unit);
  textCentered(d, 172, &FreeSansBold18pt7b, C_TEXT, 186);
  textCentered(nv.street, 200, &FreeSans9pt7b, C_DIM, 180);
}

// A line too long for its row scrolls instead of being cut short. A name or a
// number is the one thing on the call screen that has to be read whole --
// "+45 12 34 .." is not a number anybody can check.
//
// A ticker: it moves left at a steady pace and never stops, with the next pass
// following the last one after a gap, so nothing ever jumps back to the start.
// Drawn unclipped and then masked:
// the canvas has no clip rectangle, so whatever runs past the window is
// painted over with the background. Callers draw the ring afterwards, or the
// mask would take a bite out of it.
static const int      MARQUEE_PX_S     = 40;
static const int      MARQUEE_GAP      = 48;
bool marqueeOn = false;            // set while anything on screen is scrolling

void marquee(const char *s, int baselineY, const GFXfont *f, uint16_t color,
             int maxW, uint32_t since) {
  if (!s || !*s) return;
  gfx->setFont(f);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  if (w <= maxW) { textCentered(s, baselineY, f, color, maxW); return; }

  marqueeOn = true;
  int span = w + MARQUEE_GAP;
  int off = (int)((uint64_t)(millis() - since) * MARQUEE_PX_S / 1000 % span);

  int left = CX - maxW / 2;
  gfx->setTextColor(color);
  for (int k = 0; k < 2; k++) {              // this pass, and the next coming in
    gfx->setCursor(left - off + k * span - x1, baselineY);
    gfx->print(s);
  }
  int top = baselineY + y1 - 1, rows = h + 2;
  gfx->fillRect(0, top, left, rows, C_BG);
  gfx->fillRect(left + maxW, top, 240 - (left + maxW), rows, C_BG);
}

// A handset, from primitives: a thick arc with a round cap at each end, the
// ear and mouth pieces. deg turns it; 0 lies flat with the arc on top, which
// is the hang-up mark everybody knows.
void handset(int cx, int cy, int s, float deg, uint16_t color) {
  const float rad = deg * DEG_TO_RAD;
  int ox = cx - (int)(s * 0.45f * sinf(rad));
  int oy = cy + (int)(s * 0.45f * cosf(rad));
  float a0 = 205 + deg, a1 = 335 + deg;
  gfx->fillArc(ox, oy, s, s * 0.6f, fmodf(a0 + 360, 360), fmodf(a1 + 360, 360), color);
  float rm = s * 0.8f;
  for (float a : {a0, a1}) {
    float r = a * DEG_TO_RAD;
    gfx->fillCircle(ox + (int)(rm * cosf(r)), oy + (int)(rm * sinf(r)),
                    (int)(s * 0.3f), color);
  }
}

void callButton(bool dial) {
  gfx->fillCircle(CX, CALL_BTN_Y, CALL_BTN_R, dial ? C_GOOD : C_BAD);
  handset(CX, CALL_BTN_Y - (dial ? 0 : 3), 17, dial ? -40 : 0, C_TEXT);
}

void drawCall() {
  bool ringing = !strcmp(cl.status, "ringing");
  bool ready   = !strcmp(cl.status, "ready");
  bool active  = !strcmp(cl.status, "active");
  marqueeOn = false;
  const char *who = *cl.who ? cl.who : "Unknown";

  if (ringing) {
    marquee(who, 122, &FreeSansBold18pt7b, C_TEXT, 186, cl.shown);
    topLabel("Incoming call");
    gfx->fillCircle(CX - 44, 174, 17, C_BAD);
    gfx->fillCircle(CX + 44, 174, 17, C_GOOD);
    textCentered("long = reject   press = accept", 212, &FreeSans9pt7b, C_FAINT, 210);
  } else {
    marquee(who, 96, &FreeSansBold18pt7b, C_TEXT, 186, cl.shown);
    // The number under a name, so what is about to be dialled can be checked.
    // Not under a bare number: the same digits twice reads as a glitch.
    if (*cl.num && strcmp(cl.num, cl.who)) {
      marquee(cl.num, 120, &FreeSans9pt7b, C_DIM, 170, cl.shown);
    }
    topLabel(ready ? "Call" : (active ? "In call" : "Calling"));
    if (active) {
      uint32_t secs = (millis() - cl.since) / 1000;
      char t[16];
      if (secs >= 3600) {
        snprintf(t, sizeof t, "%lu:%02lu:%02lu", (unsigned long)secs / 3600,
                 (unsigned long)(secs / 60) % 60, (unsigned long)secs % 60);
      } else {
        snprintf(t, sizeof t, "%02lu:%02lu", (unsigned long)secs / 60, (unsigned long)secs % 60);
      }
      textCentered(t, 143, &FreeSansBold12pt7b, C_GOOD, 140);
    } else if (!ready) {
      textCentered("calling...", 143, &FreeSans9pt7b, C_DIM, 140);
    }
    callButton(ready);
  }

  // Last, over the marquee's masks.
  ring(0, 360, C_FAINT);
  if (ringing) {
    float ph = (millis() % 1200) / 1200.0f;
    ring(360.0f * ph, 70, C_GOOD);
  } else if (active) {
    ring(0, 360, C_GOOD, R_OUT, R_OUT - 4);
  }
}

void smallStat(int x, int y, const char *value, const char *label, uint16_t color) {
  gfx->setFont(&FreeSansBold12pt7b);
  gfx->setTextColor(color);
  int16_t x1, y1; uint16_t w, h;
  gfx->getTextBounds(value, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor(x - w / 2 - x1, y);
  gfx->print(value);

  gfx->setFont(&FreeSans9pt7b);
  gfx->setTextColor(C_FAINT);
  gfx->getTextBounds(label, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor(x - w / 2 - x1, y + 17);
  gfx->print(label);
}

void drawIdle() {
  ring(0, 360, C_FAINT);
  // textRow, not textCentered: the latter takes a baseline, so the digits
  // hung above it and sat high of the middle of a round screen where being
  // off-centre is the first thing you notice. textRow centres the ink itself
  // on the given line, and CY is the middle of the panel.
  textRow(id.clock, CY, &FreeSansBold24pt7b, C_TEXT, 190);

  // Just the battery voltage, centred under the clock. The coolant and speed
  // that used to flank it belong to a car that is being driven; the resting
  // face is what the knob shows when it is not, and the one number worth a
  // glance then is whether the battery is holding up.
  if (id.haveVolts) {
    char b[12];
    snprintf(b, sizeof b, "%.1f", id.volts);
    uint16_t voltCol = (id.volts < 12.0f || id.volts > 15.0f) ? C_WARN : C_DIM;
    smallStat(CX, 172, b, "volts", voltCol);
  } else {
    hintRow();
  }
}

// Volume: a number big enough to read at a glance and an arc that carries the
// level without being read at all. It deliberately looks nothing like the
// menu, so which mode the knob is in needs no reading -- only a glance.
// A small crossed-out speaker. Drawn from primitives rather than a bitmap
// because at this size it is a dozen shapes, and a bitmap would be another
// asset to keep in step with the palette.
void drawMuteIcon(int cx, int cy, uint16_t color) {
  gfx->fillRect(cx - 9, cy - 3, 4, 7, color);            // driver
  gfx->fillTriangle(cx - 6, cy - 3,                      // cone
                    cx - 6, cy + 4,
                    cx - 1, cy + 8, color);
  gfx->fillTriangle(cx - 6, cy - 3,
                    cx - 1, cy - 8,
                    cx - 1, cy + 8, color);
  for (int i = 0; i < 2; i++) {                          // a 2 px cross
    gfx->drawLine(cx + 3, cy - 5 + i, cx + 10, cy + 2 + i, color);
    gfx->drawLine(cx + 3, cy + 3 - i, cx + 10, cy - 4 - i, color);
  }
}

void drawVolume() {
  ring(0, 360, C_FAINT);

  if (muted) {
    // textRow centres about a row; textCentered takes a BASELINE, and using it
    // here is what left MUTED sitting high on the screen.
    textRow("MUTED", CY, &FreeSansBold18pt7b, C_WARN, 200);
    return;
  }

  ring(0, (int)(vol * 3.6f), C_ACCENT);

  // Drawn at its native size, never scaled. Doubling a 1-bit font is what put
  // stair-steps on every edge; the fix is a face that is actually this big
  // rather than a small one stretched, so the curves are rendered once at the
  // size they are shown.
  char b[8];
  snprintf(b, sizeof b, "%d", vol);
  textRow(b, CY - 6, &FreeSans48pt7b, C_TEXT, 216);

  textCentered("volume", 186, &FreeSans9pt7b, C_DIM, 200);
}

// A co-driver warning: the ring flashes left and right in the warning's
// colour, the way the Ooono box itself does, around the words.
void drawAlert() {
  const bool left = (millis() / 350) % 2;
  const int rIn = R_OUT - 18;
  ring(0, 180, left ? dimColour(alert.colour, 0.25f) : alert.colour, R_OUT, rIn);
  ring(180, 180, left ? alert.colour : dimColour(alert.colour, 0.25f), R_OUT, rIn);
  // A speed warning with a known limit: the road sign itself -- white disc,
  // red rim, black number -- which says more than "too fast" does.
  if (alert.limit > 0) {
    gfx->fillCircle(CX, CY, 76, RGB565(255, 255, 255));
    ring(0, 360, RGB565(216, 35, 42), 76, 62);
    char num[4]; snprintf(num, sizeof num, "%d", alert.limit);
    textRow(num, CY, alert.limit >= 100 ? &FreeSansBold24pt7b : &FreeSans48pt7b,
            RGB565(0, 0, 0), 110);
    return;
  }
  // Two words that do not fit side by side go on two lines, rather than
  // being cut short: "Speed camera" is wider than the ring allows.
  int16_t x1, y1; uint16_t w, h;
  gfx->setFont(&FreeSansBold18pt7b);
  gfx->getTextBounds(alert.text, 0, 0, &x1, &y1, &w, &h);
  const char *sp = strchr(alert.text, ' ');
  if (w > 170 && sp) {
    char first[24];
    strlcpy(first, alert.text, min((size_t)(sp - alert.text + 1), sizeof first));
    textRow(first, CY - 20, &FreeSansBold18pt7b, C_TEXT, 170);
    textRow(sp + 1, CY + 20, &FreeSansBold18pt7b, C_TEXT, 170);
  } else {
    textRow(alert.text, CY, &FreeSansBold18pt7b, C_TEXT, 170);
  }
}

void render() {
  gfx->fillScreen(C_BG);
  if (splashActive()) {
    // Not gated on the host being silent, which is what it used to be. The
    // car and this board power up together, so on a real start the host says
    // nothing for half a minute and the star played -- but plug the knob into
    // a running car, or restart the bridge, and the host greets it within
    // milliseconds and the animation was over before it began. What is on the
    // screen for the first few seconds is this board's business.
    drawSplash();
  } else if (mode == M_SLEEP) {
    gfx->fillScreen(0x0000);          // true black, not the faint panel grey
  } else if (alertActive()) {
    drawAlert();                       // over the menu and the volume: it is the road
  } else if (menuOpen) {
    drawMenu();
  } else if (volOverlay()) {
    drawVolume();                      // transient; falls back on its own
  } else {
    switch (mode) {
      case M_RADIO: drawRadio(); break;
      case M_MEDIA: drawMedia(); break;
      case M_NAV:   drawNav();   break;
      case M_CALL:  drawCall();  break;
      default:      drawIdle();  break;
    }
    // Muting is a state you can forget you are in, and once the overlay has
    // faded nothing else on these screens would say so. Small, low, out of the
    // way of the content above it, and pulsing rather than steady: an icon
    // that never changes in the corner of a screen you are not looking at is
    // one you stop seeing.
    // Above the artwork on the radio face: its usual place at the bottom is
    // the transport button there, and an icon inside a control is an icon
    // that looks like part of it.
    if (muted) drawMuteIcon(CX, (mode == M_RADIO || mode == M_CALL) ? 30 : 206,
                            dimColour(C_WARN, mutePulse()));
  }
  gfx->flush();
}

// Ease the list toward its target. Returns true while it is still moving.
bool stepScroll() {
  if (!menuOpen || tpDown) return false;
  float d = scrollTarget - scrollCur;
  if (fabsf(d) < 0.004f) {
    if (scrollCur != scrollTarget) { scrollCur = scrollTarget; return true; }
    return false;
  }
  scrollCur += d * ui.ease;
  return true;
}

bool animating() {
  // The splash is the one thing here that animates on its own clock.
  if (splashActive()) return true;
  if (alertActive()) return true;                             // the flashing ring
  if (menuOpen) return false;                                 // handled above
  if (muted) return true;                                     // the mute blink
  if (mode == M_RADIO) return marqueeOn;
  if (mode != M_CALL) return false;
  // The ticker runs whether or not the host has spoken lately. The bridge
  // only writes when something changes, so "linked" lapses three seconds after
  // the last update, and gating on it froze the name mid-scroll.
  if (marqueeOn) return true;
  return linked && !strcmp(cl.status, "ringing");
}

// ----------------------------------------------------------------- main ----
void setup() {
  // The panel's two power rails. Absent from the Elecrow wiki and the user
  // manual; found only in factory_soucecode/RotaryScreen_1_28, which drives
  // both high as the first thing it does. Without them the backlight pin does
  // nothing at any duty and the screen stays dark, which is what cost us the
  // "dead board" of 69de7b4.
  pinMode(PIN_PWR_1, OUTPUT); digitalWrite(PIN_PWR_1, HIGH);
  pinMode(PIN_PWR_2, OUTPUT); digitalWrite(PIN_PWR_2, HIGH);
  delay(200);

  pinMode(PIN_ENC_A, INPUT);          // external pull-ups on this board
  pinMode(PIN_ENC_B, INPUT);
  pinMode(PIN_ENC_SW, INPUT_PULLUP);
  lastCLK = digitalRead(PIN_ENC_A);
  encoderBegin();

  ledcAttach(PIN_BL, 5000, 8);
  ledcWrite(PIN_BL, backlight);

  gfx->begin();
  // Off: a line wider than the panel is either cut short by fitText or
  // scrolled by marquee, and never wrapped onto the row below it.
  gfx->setTextWrap(false);
  gfx->fillScreen(C_BG);
  gfx->flush();

  // Touch is brought up here now. It used to stay off until the host asked,
  // because an earlier version of touchBegin() drove PIN_TP_RST and
  // PIN_TP_INT and that blanked the panel for good. It no longer drives any
  // GPIO -- it opens I2C and probes an address -- so the hazard that made the
  // gate necessary is gone, while the cost of the gate was a touchscreen that
  // did nothing unless somebody remembered to send {"tpinit":true}.
  haveTouch = touchBegin();
  // A logo arrives as a few hundred lines back to back. The default 256-byte
  // CDC receive ring holds less than two of them, and the loop only drains it
  // once a frame, so the default silently loses most of an image -- which
  // looks exactly like a logo that was never sent.
  Serial.setRxBufferSize(4096);
  Serial.begin(115200);
  ooonoBegin();

  // With no host, the menu is the useful thing to show, so open it after the
  // splash rather than sitting on an empty idle screen.
  menuOpen = true;
  scrollCur = scrollTarget = menuSel;
  render();
}

void loop() {
  pollEncoder();
  pollTouch();
  pollSerial();
  ooonoPoll();

  // Menu relaxes back to volume. See menuTouchedMs.
  if (menuOpen && menuTouchedMs && millis() - menuTouchedMs > MENU_IDLE_MS) {
    menuOpen = false;
    dirty = true;
    sendMenu("timeout", menuSel);
  }

  // Repaint once when the volume overlay expires, or it would linger until
  // something else happens to mark the screen dirty.
  static bool volWas = false;
  bool volNow = volOverlay();
  if (volWas && !volNow) dirty = true;
  volWas = volNow;

  // Repaint once when a warning runs out, as for the volume overlay.
  static bool alertWas = false;
  bool alertNow = alertActive();
  if (alertWas && !alertNow) dirty = true;
  alertWas = alertNow;

  bool nowLinked = lastHostMs && (millis() - lastHostMs < 3000);
  if (nowLinked != linked) { linked = nowLinked; dirty = true; }

  // The hello sent from setup() is always lost -- the host has not opened the
  // CDC port that early. Re-announce until something arrives.
  static uint32_t lastHello = 0;
  if (!lastHostMs && millis() - lastHello > 1000) {
    lastHello = millis();
    sendHello();
  }

  // Report the animation when it finishes: how many frames it managed, over
  // how long. Without this there is no way to tell from the car whether the
  // star played at all, and no way to know whether the fade is smooth or a
  // slideshow -- nobody on this end can see the panel.
  static bool splashWas = true;
  bool splashNow = splashActive();
  if (splashWas && !splashNow) {
    dirty = true;
    // Now the fade has finished, and only now, the panel can go dark.
    if (mode == M_SLEEP) ledcWrite(PIN_BL, 0);
    JsonDocument j;
    j["e"] = "splash";
    j["frames"] = splashFrames;
    j["ms"] = splashTotal();
    emit(j);
  }
  splashWas = splashNow;

  // A running call timer repaints once a second, on the second it changes.
  if (mode == M_CALL && !strcmp(cl.status, "active")) {
    static uint32_t lastSec = 0;
    uint32_t sec = (millis() - cl.since) / 1000;
    if (sec != lastSec) { lastSec = sec; dirty = true; }
  }

  bool moving = stepScroll();
  static uint32_t lastPaint = 0;
  uint32_t now = millis();
  if (dirty || moving || (animating() && now - lastPaint > 33)) {
    if (now - lastPaint >= 16) {              // cap at ~60 fps
      render();
      dirty = false;
      lastPaint = now;
    }
  }
}
