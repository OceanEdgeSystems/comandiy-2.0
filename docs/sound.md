# Sound: output, correction, calibration

Settings > Sound on the head unit. Everything here runs on the L560 box.

## Signal path

```
players (MPD, Bluetooth, CarPlay, video, warnings, calls)
   -> comand_eq        PipeWire filter-chain sink, the default sink, held at 100%
   -> comand_eq_out    its output stream
   -> chosen output    Settings > Sound > Output (built-in now, Gigaport later)
```

- `audio/comand-eq.py` writes the filter-chain into
  `~/.config/pipewire/filter-chain.conf.d/comand-eq.conf`; the user unit
  `filter-chain.service` runs it. `--write` rewrites and restarts only if the
  config changed; `--live` changes values on the running stage (no restart).
- Per channel: calibration gain -> user gain (balance/fader, boost headroom)
  -> delay -> bass shelf (100 Hz) -> treble shelf (10 kHz) -> 5 EQ bands
  (60/250/1k/4k/12k) -> 10 calibration peaking filters.
- **Volume stays on the hardware sink.** If `comand_eq` is ever missing,
  sound falls back to the output at the driver's volume, not at 100%.
- Ducking (warning sounds in the bridge, `car/voice.py`) skips the
  `comand_eq_out` stream, or the warning would duck itself.
- `phone/call-audio.py`: calls go through the stage too; the echo fix drops
  direct links to `alsa_output*` and `comand_eq:` ports.

Files on the box:

| File | What |
|---|---|
| `~/.config/comand/settings.json` | `output`, `eq_on`, `sound` (balance, fader, bass, treble, eq), `mic_serial` |
| `~/.config/comand/sound/eq.json` | the calibration in use (per channel gain, delay, bands): a copy of the chosen preset's |
| `~/.config/comand/sound/measurement.json` | its before/after/target curves, served at `GET /measurement` |
| `~/.config/comand/sound/presets/<id>/` | each preset's own `eq.json` + `measurement.json`; `settings.json` `sound_preset` is the one in use |
| `~/.config/comand/umik/<serial>.txt`, `<serial>_90deg.txt` | miniDSP correction files |

## Calibration

`audio/comand-cal.py`, started from Settings > Sound > Calibration.

### Presets (listening positions)

| Preset | Seats measured | Use |
|---|---|---|
| Driver | driver | levels and timing exact at the driver's head |
| Passenger | front passenger | the same for the passenger |
| Front seats | driver, passenger | two in front |
| All seats | driver, passenger, rear left, rear right | a full car |

Each seat is one recording. Per speaker the seats' spectra are averaged on power (a resonance every seat hears
stays and is cut; one seat's null fills in and is left alone) and their arrival times averaged, so a multi-seat
preset lines timing up for the middle rather than for one head. Settings > Sound > Position switches between the
calibrated presets live. A calibration from before presets became the Driver preset.

### The guide

Settings > Sound > Calibration lists the presets (when calibrated, which is in use) and the mic rows (UMIK-1,
serial, correction file). Choosing a preset opens five pages: what it does, a check (mic, correction file, output;
it won't start without them), preparing the car, placing the mic, and Ready. Then the run, from the tool's JSON
progress lines (`place` waits for a press, `countdown` gives 5 s to step back, `measuring` carries the speaker,
its index and the overall progress): the seat to put the mic at, the speaker playing with a progress bar and the
speakers as a row (done / now / to come), the analysis, and a summary per speaker (level, delay, filters) that
opens the graph. Back stops a run.

Steps of one run:

1. One recording with the UMIK-1, every channel in turn: a 6 s exponential
   sweep (20 Hz to 20 kHz, -12 dBFS, straight to the output, not through the
   correction), then 2.5 s of quiet. One recording so the channels' arrival
   times compare exactly.
2. Farina deconvolution -> impulse response per channel -> spectrum
   (5 ms before the peak to 300 ms after), less the UMIK's 90-degree file
   (mic pointing at the roof), 1/6-octave smoothed.
3. Fit against a car target (+5 dB below 40 Hz easing to flat at 150 Hz,
   flat mids, -1 dB/octave above 1 kHz), 30 Hz to 2 kHz, up to 10 bands:
   peaks cut up to 12 dB; boosts only 45 Hz to 1 kHz, at most +3 dB, and
   never into a narrow or deep dip (a cancellation that won't fill).
4. Level matched to the quietest channel, lowered by the largest boost;
   nearer channels delayed to arrive with the farthest.

`comand-cal.py --simulate` fits a made-up car without playing anything.

Mic: miniDSP UMIK-1, serial 713-8537 (gain switch 18 dB). Correction files
come from `https://www.minidsp.com/scripts/umikcal/umik.php/<serial>.txt` and
`umik90.php/<serial>_90deg.txt`; the site answers Python's default
User-Agent with 403.

### First calibration, 2026-11-03

It measured the **laptop's built-in speakers**: the headphone/line jack was
empty and the active port was "Speakers". Hence almost no output below
300 Hz and a peak around 600 Hz to 2 kHz. Calibrate again once the box feeds
the car's amplifiers.

## Findings from building it

- An audioconvert `filter-graph` inside a sink only takes effect when set at
  node creation; live changes are ignored. On a filter-chain node, live
  `pw-cli s <id> Props '{ params = [ "node:Control" value ] }'` works.
- Look node IDs up by exact `node.name` from `pw-dump` JSON. Grepping
  `pw-cli ls` returned a neighbouring node and gave false negatives.

## Planned hardware

Interface: **ESI Gigaport eX** (8 line outputs on RCA, USB bus powered).
New amplifier: **Pioneer GM-DX104** (4 channels, 45 W RMS x4 at 4 ohm, 2-8 ohm
stable, RCA inputs) for the cargo speakers, with two channels spare.

| Output | Speaker | Amp |
|---|---|---|
| 1 | Front left door woofer + mirror-triangle tweeter (Bose passive crossover) | Bose |
| 2 | Front right door woofer + tweeter | Bose |
| 3 | Rear left door | Bose |
| 4 | Rear right door | Bose |
| 5 | Cargo left (now in parallel with right) | GM-DX104 |
| 6 | Cargo right | GM-DX104 |
| 7 | Bose Acoustimass bass module | Bose |
| 8 | Extra subwoofer (now fed from the Bose sub output) | its own amp |

Notes:
- GM-DX104: filters off (the box does the filtering); remote turn-on from a
  12 V line live only with the audio system (e.g. the Bose amp's switched
  12 V after its turn-on relay); fuse and cable per its manual. Measure the
  cargo speakers' impedance first.
- Front tweeter/woofer split: not now. It would need 10 outputs (MOTU
  UltraLite mk5 has 10), two more amp channels and tweeter protection
  (series capacitor as a safety net; tweeter sweeps start above the
  crossover). Revisit if, after calibration, the front stage sounds low or
  smeared.
- More than 8 outputs: one interface with more outputs (single clock) is
  better than two interfaces; if two, keep each speaker group on one card.
- `comand-eq.py` currently builds stereo (`channels_of`); the 8-channel map
  comes with the Gigaport.
