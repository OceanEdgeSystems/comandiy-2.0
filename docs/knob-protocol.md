# Knob protocol v0

Newline-delimited JSON in both directions over the CrowPanel's USB CDC serial
port. One object per line, no framing beyond the newline.

**The knob is no longer a USB HID keyboard.** The LIVI-era sketches emulated one
so a third-party application would react to keycodes; we own the host now, so
the knob speaks this protocol instead. That removes the hazard the README notes
about the old firmware -- that it types `F13`/`F14`/`Enter` into whatever has
focus the moment it is plugged into a laptop.

JSON was chosen over a packed binary format because the whole point of this link
is development ergonomics: `screen /dev/cu.usbmodem… 115200` shows you exactly
what is happening. The traffic is a few hundred bytes per second on a dedicated
cable, so compactness buys nothing.

## Two boards, one bus

There are two ESP32 screens: the knob, and a nav display that shows nothing but
the next turn. Both are `0x303A` and either may enumerate first, so they are
**not** told apart by device order or by a serial number written down
somewhere. Each announces a `role` in its `hello`:

| Role | Firmware | Board | What it is |
| --- | --- | --- | --- |
| `knob` | `esp32-knob/knob-ui` | Elecrow CrowPanel 1.28" | volume, mute, menu |
| `nav` | `esp32-knob/nav-ui` | Waveshare ESP32-S3-Touch-LCD-1.28 | turn-by-turn only |

A board that sends no role is treated as the knob, which is what older firmware
is. The bridge opens every ESP32 it finds, asks each one to identify itself with
`{"ping":true}` -- the `hello` sent from `setup()` is always lost, and a board
that has been running for hours has no reason to send another -- and keeps
scanning, so replugging either board recovers on its own.

**Turn-by-turn goes to the nav display, not the knob.** The knob is a control
that happens to have a screen; the nav display is a screen with one job.

### The two boards do not share a pin map

Close enough to be dangerous. `SCLK`, `MOSI` and `CS` agree; `DC`, `RST` and
the backlight do not, and the CrowPanel has two power-enable rails the
Waveshare board does not have:

| | CrowPanel 1.28" | Waveshare S3-Touch-LCD-1.28 |
| --- | --- | --- |
| DC | 3 | **8** |
| RST | 14 | **12** |
| Backlight | 46 | **40** |
| Power rails | **1 and 2, both required** | none |

Getting any of that wrong gives a black screen on a module that runs perfectly,
which is the failure this project has already lost a session to. `nav-ui.ino`
therefore has an explicit board select and no default.

## Design rule

**The firmware is dumb about meaning.** It renders what the host describes and
reports what the user did; it does not decide what a rotation means. The single
exception is the **menu overlay**, which the knob draws and navigates locally,
because selection has to feel instant and a USB round trip per detent does not.
The host is told when the menu opens, moves and commits, so it can preview or
ignore as it likes.

## Host -> knob

Every field is optional. A message carries only what changed; the knob keeps
the rest. Any received line also counts as a heartbeat.

| Key | Type | Meaning |
| --- | --- | --- |
| `m` | `"media"` \| `"nav"` \| `"call"` \| `"idle"` \| `"menu"` | switch screen |
| `media` | object | `title`, `artist`, `source`, `pos`, `dur` (seconds), `playing` |
| `nav` | object | `maneuver`, `dist`, `unit` (`"m"` \| `"km"`), `street` |
| `call` | object | `who`, `status` (`"ringing"` \| `"active"` \| `"dialing"`), `secs` |
| `idle` | object | `clock`, `coolant`, `volts`, `kph` |
| `hint` | object | `rot`, `press` -- what the controls currently do, shown in small type |
| `menu` | array of strings | replaces the menu contents (max 12, 19 chars each) |
| `sel` | int | move the selection from the host side |
| `bl` | int 8-255 | backlight |
| `ping` | bool | ask the knob to re-send `hello` |
| `ui` | object | layout tunables, see below |
| `uiget` | bool | dump the current layout as an `ui` event |
| `dbg` | bool | emit `touch` events for every finger down/up |
| `tpinit` | bool | re-run touch initialisation, report which strategy worked |
| `i2cscan` | bool | scan the documented touch pins, then the same pair swapped |
| `i2csweep` | bool | probe every safe GPIO pair for a touch controller |

`maneuver` is one of `straight`, `left`, `right`, `slight-left`, `slight-right`,
`sharp-left`, `sharp-right`, `uturn`. With `unit` `"km"`, `dist` is in tenths of
a kilometre, so `dist: 124` renders as `12.4 km`.

```json
{"m":"media","media":{"title":"Get Lucky","artist":"Daft Punk","source":"CarPlay","pos":73,"dur":248,"playing":true},"hint":{"rot":"volume","press":"play/pause"}}
{"m":"nav","nav":{"maneuver":"left","dist":300,"unit":"m","street":"Hauptstrasse"}}
{"menu":["Now Playing","CarPlay","Radio","Vehicle","Settings"]}
{"bl":96}
```

## Knob -> host

| Event | Fields | When |
| --- | --- | --- |
| `hello` | `fw`, `w`, `h`, `shape`, `touch`, `tpinit` | at boot, and in reply to `ping` |
| `rot` | `d` = `+1` / `-1` | one detent, menu closed |
| `press` | `t` = `"short"` / `"long"` | menu closed. Long press with a menu loaded opens it instead |
| `menu` | `a` = `open`/`move`/`activate`/`cancel`, `sel`, `label` | menu overlay |

```json
{"e":"hello","fw":"knob-ui 0.1","w":240,"h":240,"shape":"round"}
{"e":"rot","d":1}
{"e":"press","t":"short"}
{"e":"menu","a":"activate","sel":2,"label":"Radio"}
```

## Link state

The knob considers itself linked while it has seen any line within **3 s**. On
timeout it shows a waiting screen rather than a frozen stale one -- a screen
that keeps displaying the last track after the host died is worse than one that
admits it. Send something at least once a second; the simulator sends at 4 Hz.

## Interaction model

| Input | Default state | Menu open |
| --- | --- | --- |
| rotate | **volume**, and `rot` to the host | move selection locally, ends stop |
| short press | **mute / unmute**, `press:mute`\|`unmute` | `menu:activate`, close |
| long press (>600 ms) | open the menu | `menu:cancel`, close |
| 5 s idle | -- | `menu:timeout`, close |

### Why it is arranged this way

**Rotation is volume because volume is the thing done constantly, at speed,
without looking.** It needs no mode to be entered and nothing to be selected:
it is what the knob does when it is not doing anything else, and every other
state falls back to it.

**The menu closes itself after five idle seconds, and that is the most
important line here.** Without it the knob can be left in menu mode: you open
it, get distracted, and the next time you reach over to turn the volume down
you scroll a list instead -- a mode error discovered *after* acting, with your
eyes on the road. Relaxing back to volume means the knob is only ever wrong for
a moment, and whatever you reach for blind is what it does.

**The menu is behind a long press, not a short one.** A short press is easier
while driving, which is exactly the problem: the knob gets knocked. Entering a
mode should cost deliberate intent, and 600 ms of hold is that intent.

**Short press mutes**, as on this car's original COMAND and on essentially
every car knob, so there is nothing to learn. Turning while muted un-mutes and
sets the level, because that is plainly what reaching for the knob means.

**The two screens look nothing alike** -- a large number with an arc, against a
list -- so which mode the knob is in takes a glance rather than a read.

## Screens

All in `esp32-knob/knob-ui/knob-ui.ino`. Boot is a firmware state, not a host
mode. The menu is both: the host can request it, and a long press opens it.

| Screen | Shows |
| --- | --- |
| boot | splash, first 2 s until the host speaks |
| menu | scrolling text list, focus pinned to the centre |
| media | title, artist, source, play state, elapsed; ring is track progress |
| nav | maneuver arrow, distance, street |
| call | caller, ringing pulse or call timer, accept/reject dots |
| idle | clock, and coolant / volts / km/h once vehicle data exists |

### The menu

The default list is baked into the firmware -- Telephone, Music, Radio,
Carplay, Navigation, Car, Settings -- so the knob is useful with no host at
all, which is also how it is developed. The host can replace the list at any
time.

The focused item sits at the centre of the screen and the list moves under it,
with neighbours rendered smaller and dimmer by distance. Text is clipped to the
circle's chord at each row's height rather than to the full 240 px, or the
outer rows would run off the glass.

Two hairlines used to mark the focus band. They are off now: the focused row is
already the largest and brightest thing on screen, so the lines only added
furniture to a 240 px circle that has none to spare. `band 20` brings them back
without a reflash if that judgement turns out to be wrong.

**The list does not wrap.** With neighbours visible, wrapping puts "Settings"
directly above "Telephone", which reads as a rendering glitch rather than a
feature. The ends stop and the ring segment shows position instead.

Scrolling is eased rather than snapped -- the list moves 35% of the remaining
distance per frame, capped at 60 fps -- because a rotary control that teleports
feels broken.

A small amber dot at the bottom means no host is connected. That is the normal
state while developing.

### Tuning the layout

The menu's geometry is changeable at runtime, because reflashing to move a row
four pixels is a bad way to design a screen you can only judge by looking at
it -- and on this workstation a flash cycle is about two minutes.

| Key | Default | Meaning |
| --- | --- | --- |
| `rowh` | 40 | pixels between rows (16-80) |
| `band` | 0 | focus-band hairlines; 0 off, or a half-height in px |
| `radius` | 104 | text is clipped to the circle's chord at this radius |
| `ease` | 0.35 | fraction of remaining scroll distance per frame |
| `near` | 0.5 | \|offset\| below this is the focused row: large and bright |
| `mid` | 1.5 | ...and below this is a neighbour: medium and dim |
| `wrap` | false | endless list, or ends that stop |
| `ring` | true | position segments around the rim |

```sh
.venv/bin/python esp32-knob/knob-tune.py
> rowh 46
> wrap on
> get
```

Nothing is persisted. Settle on values, then bake them into `UiCfg` as the
firmware defaults.

## Touch -- working

The CST816D works at `0x15` on SDA 6 / SCL 7, RST 13, INT 5 -- exactly the
configuration this firmware always used. It never acknowledged because the
panel power rails on GPIO 1 and 2 were low, so the controller was unpowered.
With the rails up, `{"tpinit":true}` returns `{"e":"tpinit","touch":true}` on
the first attempt.

Everything previously recorded as evidence against the touch controller was the
same symptom in a different costume: no ACK at 400 kHz or 100 kHz, three init
strategies failing, retry cycles never succeeding. An unpowered chip fails all
of them identically. The sweep hit at `6/7 @ 0x15` -- dismissed at the time as
a floating line ACKing, by the same reasoning that dismissed the real one --
was in fact the touch controller at its documented address.

The question the old note posed, "whether this board variant has a touch
controller populated at all", was the wrong question, and it was reached by
treating the exhaustion of one hypothesis as evidence for another.

Touch still starts disabled and is enabled with `{"tpinit":true}`; see the
comment above `touchBegin()`.

## Developing against it

`esp32-knob/knob-sim.py` plays the host. It needs pyserial:

```sh
python3 -m venv .venv && .venv/bin/pip install pyserial
.venv/bin/python esp32-knob/knob-sim.py --demo     # cycle every screen
.venv/bin/python esp32-knob/knob-sim.py --watch    # print knob events only
```

## The panel has two power rails, on GPIO 1 and 2

2026-09-08. **`pinMode(1, OUTPUT); digitalWrite(1, HIGH);` and the same for
GPIO 2, before anything else.** Without both, the panel has no power: the
backlight pin does nothing at any duty and the screen stays black.

The pins are in neither the Elecrow wiki nor the user manual. They exist only
in Elecrow's factory source, `factory_soucecode/RotaryScreen_1_28`, commented
merely as "Power pin 1, output current" -- the first two statements of its
`setup()`. Every sketch in this repo left them low.

The failure is convincing because everything else works. The module enumerates,
flashes and runs; GPIO 40 blinks the power LED on command; the encoder reports;
`panel->begin()` returns true. Only the glass is dark. There is no error to
find because nothing is in an error state.

Get board facts from Elecrow's repository, not the wiki:
`github.com/Elecrow-RD/CrowPanel-1.28inch-HMI-ESP32-Rotary-Display-240-240-IPS-Round-Touch-Knob-Screen`.
It carries the factory source, the Eagle schematic and the factory firmware.
The wiki's pin map is correct as far as it goes and simply omits the two pins
that matter most.

### Retraction: the rail had not failed

An earlier version of this section, and commit 69de7b4, recorded this as dead
hardware -- "a rail feeding both has failed", "no GPIO or SPI setting will
bring it back" -- and further guessed that our own pin sweeps had damaged the
board. **All of that was wrong.** The board was never damaged. It was
unpowered by our own firmware, and it lit up the moment GPIO 1 and 2 went high.

Two mistakes produced it, both worth remembering:

- The power LED on GPIO 40 was called "not under firmware control" and used as
  evidence of a hardware fault. It is under firmware control, and an earlier
  sketch had set it low. A symptom we caused was read as proof of damage.
- Exhaustive testing of the wrong hypothesis was mistaken for proof. Every
  ruled-out item -- backlight high and low, SPI at four clock rates, PSRAM off,
  a different USB port -- assumed the panel was powered. None of them could
  have worked, so their failing said nothing.

What actually settled it was a single observation that no amount of our own
testing could substitute for: reflashing Elecrow's stock firmware, which lit
the screen. Once a known-good build works on the same board, the fault is
yours by definition. Reach for that first, and read the vendor's source before
theorising about the vendor's hardware.

### On pin sweeps

The earlier advice not to brute-force pin sweeps without a schematic stands,
though not for the reason given -- no damage ever occurred. The better reason
is that a sweep can be run correctly and still mislead. A one-pin-at-a-time
sweep across 18 candidates, each released to `INPUT` before the next, did
include GPIO 1 and GPIO 2 and still found nothing, because the rails and the
backlight have to be high *together*. Single-pin isolation is what makes a
sweep safe and is exactly what blinds it to a combination.

## When the board looks dead

**Check the boot mode before theorising about anything else.** An evening was
lost to a dark screen that turned out to be the board sitting in download mode:
no application code was running, so nothing drove the panel, nothing brought up
TinyUSB and nothing printed. A display self-test showing "all six stages dark"
did not mean the panel was dead -- it meant the test never ran.

```sh
.venv/bin/python - <<'PY'
import serial, time
s = serial.Serial()
s.port = "/dev/cu.usbmodem21101"   # the SHORT name: the ROM USB-Serial-JTAG
s.baudrate = 115200; s.timeout = 0
s.dtr = False; s.rts = False        # see below -- this matters
s.open()
t0 = time.time(); buf = b""
while time.time() - t0 < 6:
    buf += s.read(8000); time.sleep(0.2)
print(buf.decode(errors="replace"))
PY
```

Read the `boot:` field:

| Boot log | Meaning |
| --- | --- |
| `boot:0x18` / `0x8` (`SPI_FAST_FLASH_BOOT`) | healthy, the app is running |
| `boot:0x10` (`DOWNLOAD(USB/UART0)`) | **no app is running**; the chip is waiting for a flasher |

Download mode can latch and survive the soft chip resets that esptool performs
(`rst:0x15 USB_UART_CHIP_RESET`). **Only a real power-on reset clears it** --
unplug the USB cable for ten seconds. A healthy boot then reports `rst:0x1
(POWERON)`.

### Which port is which, and the DTR trap

| Port name | What it is |
| --- | --- |
| long, MAC-derived (`cu.usbmodem68EE8F5DC3401`) | TinyUSB CDC -- **our firmware is running** |
| short (`cu.usbmodem21101`) | ROM USB-Serial-JTAG -- the app is probably *not* running |

DTR and RTS must be handled differently on each, and getting it wrong looks
exactly like dead hardware:

- On the **JTAG** port they drive GPIO0 and EN, so an ordinary port open can
  itself force the chip into download mode. Deassert both.
- On **TinyUSB CDC** the device only transmits while DTR is asserted. Deassert
  it and the link goes silent even though everything is fine.

`knob-tune.py` picks the right behaviour from the port name.

## Volume

The level lives in both places. The knob applies a detent optimistically and
draws it at once -- a USB round trip before the number moves feels broken even
when it is only milliseconds -- and the host corrects it with `{"vol":n}`.

```json
{"vol": 42, "muted": false}
```

The host is the authority. It should send `vol` whenever the level changes for
any reason, including from the steering wheel or the phone, and the knob shows
the overlay on a host-driven change too, so the panel never disagrees with the
speakers. `hello` reports the knob's current `vol` and `muted` so the host can
sync on connect.

The overlay holds for 1.6 s after the last change and then falls back to
whichever context screen the mode calls for.

The level is drawn in `FreeSans48pt7b`, generated with Adafruit's `fontconvert`
from GNU FreeSans and **restricted to the digits**:

```sh
fontconvert FreeSans.ttf 48 48 57 > FreeSans48pt7b.h
```

That range is what makes a 48 pt face affordable -- 3.7 KB against roughly
20 KB for a full one -- and the readout never shows a letter. It is drawn at
native size and never scaled: doubling a 1-bit font is what put stair-steps on
every curve, and the fix is a face that is genuinely this big rather than a
small one stretched.

Once the overlay fades, a crossed-speaker icon sits low on the context screens
while muted, **pulsing smoothly over 2.2 s** rather than blinking. A steady icon
in the corner of a screen you are not looking at is one you stop seeing, but a
blink draws attention to the transition rather than to the state -- on a dark
screen an icon appearing from nothing reads like a warning light, which is too
urgent for "the audio is muted". A fade carries the same motion without ever
being an event.

Two details make it read as breathing rather than flicker. The curve is a
raised cosine, `(1 - cos 2*pi*p) / 2`, so there are no corners and the
turn-around is not a moment you can point at -- a triangle wave is smooth in
the middle but you can see it change direction. And it never reaches zero: the
floor is 28 %, so the icon stays legible throughout and a glance at the wrong
instant still finds it. Tunable as `MUTE_PULSE_MS` and `MUTE_PULSE_MIN`.

Note this means `animating()` is true while muted -- without it the screen
repaints only on change and the pulse would draw a single frame and stop.

## Not settled yet

- **Volume acceleration.** Rotation is a bare `+1`/`-1`, so a fast spin arrives
  as a burst and crossing the range takes a lot of wrist. Acceleration needs a
  timestamp or an accumulated delta, which means changing `rot`.
- **Who owns the menu contents.** Currently the host pushes a flat list.
  Submenus would need either nesting in the protocol or a menu stack on the
  host, and the second is probably right.
