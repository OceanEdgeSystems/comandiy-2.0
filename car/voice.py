#!/usr/bin/env python3
"""Voice commands for COMAND, offline, without the phone or Siri.

Press 7 (ui/comand-keys.py sends a datagram here) or say the wake word chosen in
Settings, hear a beep, say one phrase.
Vosk listens only for the fixed phrases below, so what it can mishear is limited
and it is fast enough on this laptop. Nothing is recorded or kept.

    voice.py                 the service
    voice.py --file x.wav    what a 16 kHz mono WAV would do, without doing it
"""
import argparse
import json
import math
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave

import vosk

BRIDGE = "http://127.0.0.1:8080"
PORT = 7342
MODEL = "/var/lib/comand/vosk/vosk-model-small-en-us-0.15"
MIC = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
RATE = 16000
LISTEN_S = 5.0
# Below this, Vosk was guessing: better to say so than to do the wrong thing.
MIN_CONF = 0.65
DIR = "/var/lib/comand/voice"
# Spoken with no key pressed, so it has to be surer than a command after a beep.
WAKE_CONF = 0.90
WAKE_WORDS = {"comand": "hey command", "ml": "hey m l", "mercedes": "hey mercedes"}
# Reports reach other drivers: never from a wake word that may have misfired.
KEY_ONLY = ("report camera", "report danger")

# Station names as they are said, where the name on the tile can't be spoken
# as written. Anything else is matched on its own name.
SAY_STATION = {
    "DR P5 Syd": ["p five", "d r p five"],
    "Radio Monte Carlo": ["monte carlo", "radio monte carlo"],
    "Radio VLR Vejle-Kolding": ["v l r", "radio v l r"],
    "Cafe del Mar": ["cafe del mar", "cafe"],
    "Los 40": ["los forty", "forty"],
    "Cadena 100": ["cadena", "cadena one hundred", "one hundred"],
}
NUMBERS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
           "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def post(payload: dict) -> None:
    req = urllib.request.Request(BRIDGE + "/cmd", json.dumps(payload).encode(), method="POST")
    urllib.request.urlopen(req, timeout=3).read()


def state() -> dict:
    with urllib.request.urlopen(BRIDGE + "/state", timeout=3) as r:
        return json.load(r)


def stations() -> list[str]:
    with urllib.request.urlopen(BRIDGE + "/stations", timeout=3) as r:
        return [s["name"] for s in json.load(r)]


def toast(text: str) -> None:
    post({"toast": {"icon": "bluetooth", "text": text, "ms": 2500}})


def last_station() -> str:
    """The station playing or last chosen, or the first preset."""
    return state().get("radio") or stations()[0]


def step_volume(d: int) -> dict:
    return {"vol": max(0, min(100, int(state()["vol"]) + d))}


def step_station(d: int) -> dict:
    names = stations()
    now = state().get("radio")
    i = names.index(now) if now in names else -d
    return {"radio": {"play": names[(i + d) % len(names)]}}


def phrases() -> dict:
    """Everything that can be said: phrase -> (what to show, what to do)."""
    out = {
        "volume up": ("Volume up", lambda: step_volume(10)),
        "louder": ("Volume up", lambda: step_volume(10)),
        "volume down": ("Volume down", lambda: step_volume(-10)),
        "quieter": ("Volume down", lambda: step_volume(-10)),
        "mute": ("Mute", lambda: {"muted": True}),
        "sound on": ("Sound on", lambda: {"muted": False}),
        "play radio": ("Radio", lambda: {"radio": {"play": last_station()}}),
        "radio on": ("Radio", lambda: {"radio": {"play": last_station()}}),
        "radio": ("Radio", lambda: {"radio": {"play": last_station()}}),
        "radio off": ("Radio off", lambda: {"radio": {"stop": True}}),
        "stop radio": ("Radio off", lambda: {"radio": {"stop": True}}),
        "next station": ("Next station", lambda: step_station(1)),
        "previous station": ("Previous station", lambda: step_station(-1)),
        "open car play": ("CarPlay", lambda: {"activate": "Carplay"}),
        "car play": ("CarPlay", lambda: {"activate": "Carplay"}),
        "open radio": ("Radio", lambda: {"activate": "Radio"}),
        "open telephone": ("Telephone", lambda: {"activate": "Telephone"}),
        "show menu": ("Menu", lambda: {"activate": "Home"}),
        # The guest remote's QR code (Settings > App control): volume and music on a passenger's phone.
        "app control": ("App control", lambda: {"guest": "show"}),
        "show remote": ("App control", lambda: {"guest": "show"}),
        "main menu": ("Menu", lambda: {"activate": "Home"}),
        "answer": ("Answering", lambda: {"phone": {"answer": True}}),
        "hang up": ("Hanging up", lambda: {"phone": {"hangup": True}}),
        "report camera": ("Camera reported", lambda: {"ooono": {"btn": 120}}),
        "report danger": ("Danger reported", "double"),
    }
    for word, n in NUMBERS.items():
        out[f"volume {word}"] = (f"Volume {n}", lambda n=n: {"vol": n})
    for name in stations():
        for said in SAY_STATION.get(name, [re.sub(r"[^a-z ]", "", name.lower())]):
            act = (f"Playing {name}", lambda name=name: {"radio": {"play": name}})
            out[f"play {said}"] = act
    return out


def recogniser(model: vosk.Model, table: dict) -> vosk.KaldiRecognizer:
    usable = [p for p in table if all(model.vosk_model_find_word(w) >= 0 for w in p.split())]
    missing = sorted(set(table) - set(usable))
    if missing:
        print(f"not in the model's vocabulary: {missing}", flush=True)
    rec = vosk.KaldiRecognizer(model, RATE, json.dumps(usable + ["[unk]"]))
    rec.SetWords(True)
    return rec


def wake_listener(model: vosk.Model, wakes: queue.Queue, busy: threading.Event) -> None:
    """Listen for the chosen wake word, only while it is on and nothing else is going on."""
    mic, rec, word, checked, steady = None, None, None, 0.0, 0
    while True:
        if time.monotonic() - checked > 2:
            checked = time.monotonic()
            try:
                st = state()
                want = WAKE_WORDS.get(st.get("wakeWord")) if not st.get("call") else None
            except (OSError, ValueError):
                want = None
            if want != word or (busy.is_set() and mic):
                if mic:
                    mic.kill()
                    mic = None
                word = want
                print(f"wake word: {word}", flush=True)
        if not word or busy.is_set():
            time.sleep(0.2)
            continue
        if not mic:
            rec = vosk.KaldiRecognizer(model, RATE, json.dumps([word, "[unk]"]))
            rec.SetWords(True)
            mic = open_mic()
        data = mic.stdout.read(3200)
        if not data:
            mic.kill()
            mic = None
            time.sleep(1)
            continue
        # The moment the words have been steady for 0.2 s, not after the silence
        # that ends a phrase: that wait was most of the delay.
        ended = rec.AcceptWaveform(data)
        if not ended:
            steady = steady + 1 if json.loads(rec.PartialResult()).get("partial") == word else 0
            if steady < 2:
                continue
        steady = 0
        if True:
            result = json.loads(rec.Result() if ended else rec.FinalResult())
            words = result.get("result") or []
            conf = sum(w.get("conf", 0) for w in words) / len(words) if words else 0
            if result.get("text") == word and conf >= WAKE_CONF:
                print(f"woken by {word!r}, confidence {conf:.2f}", flush=True)
                busy.set()
                mic.kill()
                mic = None
                wakes.put("wake")


def judge(result: dict, table: dict):
    """The command heard, or None and the reason."""
    text = (result.get("text") or "").strip()
    words = result.get("result") or []
    conf = sum(w.get("conf", 0) for w in words) / len(words) if words else 0
    print(f"heard {text!r}, confidence {conf:.2f}", flush=True)
    if not text or "[unk]" in text or text not in table:
        return None
    if conf < MIN_CONF:
        return None
    return table[text]


def tone(name: str, notes: list) -> str:
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, name + ".wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            for freq, secs in notes:
                n = int(48000 * secs)
                w.writeframes(b"".join(struct.pack("<h", int(
                    14000 * min(1, i / 240, (n - i) / 240) * math.sin(2 * math.pi * freq * i / 48000)))
                    for i in range(n)))
    return path


def _note(freq: float, secs: float, amp: float = .5, decay: float | None = None,
          overtones: tuple = ()) -> list[float]:
    out = []
    n = int(48000 * secs)
    total = 1 + sum(a for _, a in overtones)
    for i in range(n):
        t = i / 48000
        shape = min(1, t / .004) * (math.exp(-t / decay) if decay else min(1, (n - i) / 288))
        v = math.sin(2 * math.pi * freq * t) + sum(a * math.sin(2 * math.pi * freq * m * t)
                                                   for m, a in overtones)
        out.append(v * amp * shape / total)
    return out


def _sweep(start: float, end: float, secs: float, amp: float = .45) -> list[float]:
    out, phase = [], 0.0
    n = int(48000 * secs)
    for i in range(n):
        phase += 2 * math.pi * start * (end / start) ** (i / n) / 48000
        out.append(math.sin(phase) * amp * min(1, i / 192, (n - i) / 288))
    return out


# The sound that says "listening", chosen in Settings > Voice sound.
LISTEN_SOUNDS = {
    "current": lambda: _note(880, .07, .45) + _note(1320, .09, .45),
    "bell": lambda: _note(1047, .7, .5, .18, ((2.0, .35), (3.0, .12))),
    "chime": lambda: _note(784, .28, .5, .12, ((2.0, .3),)) + _note(1175, .5, .5, .16, ((2.0, .3),)),
    "sweep": lambda: _sweep(500, 1600, .18),
    "pop": lambda: _note(420, .12, .6, .035),
}


def listen_sound(name: str) -> str:
    """The file for a listening sound, made the first time it is needed."""
    name = name if name in LISTEN_SOUNDS else "current"
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, f"listen-{name}.wav")
    if not os.path.exists(path):
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767))
                                   for x in LISTEN_SOUNDS[name]()))
    return path


def play(path: str) -> None:
    subprocess.run(["paplay", path], timeout=4, check=False)


def duck(on: bool) -> None:
    """Music down while listening, so the mic hears the driver and not the speakers."""
    def work():
        # In the background and on a short leash: pactl can hang for 30 s on one
        # stream, and the driver is waiting for the beep.
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                 text=True, check=False, timeout=1.5).stdout
            # Not the sound correction's own output: ducking it ducks the beep too.
            ids = [str(i["index"]) for i in json.loads(out or "[]")
                   if i.get("properties", {}).get("node.name") != "comand_eq_out"]
            for idx in ids:
                subprocess.Popen(["timeout", "2", "pactl", "set-sink-input-volume", idx,
                                  "25%" if on else "100%"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
            print(f"duck: {e}", flush=True)
    threading.Thread(target=work, daemon=True).start()


def open_mic() -> subprocess.Popen:
    return subprocess.Popen(["parec", "--device", MIC, "--format=s16le", f"--rate={RATE}",
                             "--channels=1", "--raw", "--latency-msec=50"], stdout=subprocess.PIPE)


def listen(rec: vosk.KaldiRecognizer) -> dict:
    mic = open_mic()
    until = time.monotonic() + LISTEN_S
    try:
        while time.monotonic() < until:
            data = mic.stdout.read(3200)
            if not data:
                break
            if rec.AcceptWaveform(data):
                return json.loads(rec.Result())
        return json.loads(rec.FinalResult())
    finally:
        mic.kill()
        rec.Reset()


def act(command) -> None:
    label, what = command
    if what == "double":
        post({"ooono": {"btn": 120}})
        time.sleep(0.2)
        post({"ooono": {"btn": 120}})
    else:
        post(what())
    toast(label)


def service(model: vosk.Model) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", PORT))
    wakes: queue.Queue = queue.Queue()
    busy = threading.Event()

    def keys():
        while True:
            word = sock.recv(64).decode(errors="replace")
            if word.startswith("preview "):
                play(listen_sound(word.split()[1]))
                continue
            if not busy.is_set():         # presses during listening don't queue up
                busy.set()
                wakes.put("key")

    threading.Thread(target=keys, daemon=True).start()
    threading.Thread(target=wake_listener, args=(model, wakes, busy), daemon=True).start()
    fail = tone("fail", [(440, .10), (330, .16)])
    done = tone("done", [(1320, .08)])
    print("ready", flush=True)
    while True:
        how = wakes.get()
        table = phrases()
        if how == "wake":
            for phrase in KEY_ONLY:
                table.pop(phrase, None)
        rec = recogniser(model, table)
        duck(True)
        try:
            play(listen_sound(state().get("voiceSound", "current")))
            toast("Listening...")
            command = judge(listen(rec), table)
        finally:
            duck(False)
        if command:
            play(done)
            act(command)
        else:
            play(fail)
            toast("Didn't catch that")
        busy.clear()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--wake-test", help="count wake words heard in a 16 kHz mono WAV")
    a = ap.parse_args()
    vosk.SetLogLevel(-1)
    model = vosk.Model(MODEL)
    if a.wake_test:
        with wave.open(a.wake_test) as w:
            frames = w.readframes(w.getnframes()) + bytes(RATE * 2)   # a second of quiet
        for word in WAKE_WORDS.values():
            rec = vosk.KaldiRecognizer(model, RATE, json.dumps([word, "[unk]"]))
            rec.SetWords(True)
            found = []
            for i in range(0, len(frames), 3200):
                if rec.AcceptWaveform(frames[i:i + 3200]):
                    r = json.loads(rec.Result())
                    ws = r.get("result") or []
                    if r.get("text") == word:
                        found.append(round(sum(x["conf"] for x in ws) / len(ws), 2))
            print(word, "heard", found, "over", WAKE_CONF, sum(c >= WAKE_CONF for c in found), flush=True)
        return 0
    if not a.file:
        service(model)
        return 1
    table = phrases()
    rec = recogniser(model, table)
    with wave.open(a.file) as w:
        rec.AcceptWaveform(w.readframes(w.getnframes()))
    command = judge(json.loads(rec.FinalResult()), table)
    print("->", command[0] if command else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
