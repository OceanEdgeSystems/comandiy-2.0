#!/usr/bin/env python3
"""The car's own state, from its Home Assistant, for the Vehicle page.

A Home Assistant in the car decodes the car's own data. This
reads the handful of values the Vehicle page shows over its websocket
(subscribe_entities, so only those entities, pushed as they change) and hands
them to the bridge as {"vehicle": {...}}, at most a few times a second. When
Home Assistant cannot be reached it says so once, as {"vehicle": None}, and the
page shows that rather than numbers from an hour ago.

The addresses come from /etc/default/comand. The token is a read-only
Home Assistant user's, kept in /etc/default/comand-vehicle:

    HA_TOKEN=...
    HA_URLS="http://<address>:8123"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

BRIDGE = "http://127.0.0.1:8080/cmd"
URLS = os.environ.get("HA_URLS", "").split()
TOKEN = os.environ.get("HA_TOKEN", "")
P = os.environ.get("HA_ENTITY_PREFIX", "car_")
MIN_GAP_S = 0.4          # the page redraws at most this often

# page key -> entity. Binary sensors come out as booleans, sensors as numbers
# where they are numbers.
ENTITIES = {
    "can": f"binary_sensor.{P}engine_can_link",
    "running": f"binary_sensor.{P}engine_running",
    "speed": f"sensor.{P}vehicle_speed",
    "rpm": f"sensor.{P}engine_rpm",
    "selector": f"sensor.{P}gear_selector",
    "gear": f"sensor.{P}transmission_gear",
    "coolant": f"sensor.{P}coolant_temperature",
    "oil": f"sensor.{P}oil_temperature",
    "fuel_left": f"sensor.{P}fuel_left_estimate",
    "odometer": f"sensor.{P}odometer_cluster_can",
    "battery_v": "sensor.main_battery_voltmeter",
    "battery_soc": "sensor.car_battery_soc",
    "park_brake": f"binary_sensor.{P}parking_brake",
    "park_lights": f"binary_sensor.{P}parking_lights",
    "low_beam": f"binary_sensor.{P}low_beam",
    "fog": f"binary_sensor.{P}fog_lights",
    "mil": f"binary_sensor.{P}k_line_engine_check_engine_light_mil",
    "check_engine": f"binary_sensor.{P}check_engine",
    "cruise": f"binary_sensor.{P}cruise_control_engaged",
    "limiter": f"binary_sensor.{P}speed_limiter_engaged",
    "limiter_selected": f"binary_sensor.{P}speed_limiter_selected",
    # gearbox (w163_can gearbox.py)
    "target": f"sensor.{P}transmission_target_gear",
    "lockup": f"sensor.{P}converter_lockup",
    "slip": f"sensor.{P}converter_slip",
    "range_limit": f"sensor.{P}gear_range_limit",
    "shifting": f"binary_sensor.{P}transmission_shifting",
    "last_shift": f"sensor.{P}last_shift",
    "drive": f"sensor.{P}gearbox_this_drive",
}
KEY = {eid: k for k, eid in ENTITIES.items()}
# entities whose attributes the page needs too, posted as "<key>_attrs"
WITH_ATTRS = {"last_shift", "drive"}
# The Gearbox view's shift log: the last few shifts from the integration's own log (websocket w163_can/gearbox_log,
# allowed for a read-only user), fetched on connect and after each new shift, posted as "shifts" (newest first).
SHIFT_LOG_N = 6
SHIFT_LOG_S = 3 * 3600


def compact(r: dict) -> dict:
    return {k: r.get(k) for k in ("t", "from", "to", "kind", "s", "kmh", "pedal", "rpm0", "rpm1")}


def value(entity_id: str, s: str):
    if s in ("unavailable", "unknown", "", None):
        return None
    if entity_id.startswith("binary_sensor."):
        return s == "on"
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def post(vehicle) -> None:
    req = urllib.request.Request(BRIDGE, data=json.dumps({"vehicle": vehicle}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception as e:  # noqa: BLE001 - the bridge restarting is not our problem
        print(f"bridge: {e}", flush=True)


async def session(url: str) -> None:
    ws_url = url.replace("http", "ws", 1) + "/api/websocket"
    async with websockets.connect(ws_url, open_timeout=6, ping_interval=20, ping_timeout=20,
                                  max_size=4 * 1024 * 1024) as ws:
        await ws.recv()                                   # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        ok = json.loads(await ws.recv())
        if ok.get("type") != "auth_ok":
            raise PermissionError(f"auth refused at {url}: {ok.get('message', ok)}")
        await ws.send(json.dumps({"id": 1, "type": "subscribe_entities",
                                  "entity_ids": list(ENTITIES.values())}))
        print(f"connected to {url}", flush=True)
        car: dict = {}
        sent_at, dirty = 0.0, False
        log_id, last_seen, want_log, asked_at = 100, None, True, 0.0
        while True:
            if want_log:
                # a shift's record is written once its time is known (up to 3 s after it), so ask a moment later too
                log_id += 1
                await ws.send(json.dumps({"id": log_id, "type": "w163_can/gearbox_log", "log": "shifts",
                                          "since": time.time() - SHIFT_LOG_S}))
                want_log = False
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=MIN_GAP_S)
                msg = json.loads(raw)
                if msg.get("type") == "result" and msg.get("id", 0) > 100:
                    if msg.get("success") and isinstance(msg.get("result"), list):
                        car["shifts"] = [compact(r) for r in msg["result"][-SHIFT_LOG_N:]][::-1]; dirty = True
                    continue
                ev = msg.get("event") or {}
                # subscribe_entities: "a" whole states, "c" changes ("+" holds the new state)
                for eid, st in (ev.get("a") or {}).items():
                    if eid in KEY:
                        car[KEY[eid]] = value(eid, st.get("s")); dirty = True
                        if KEY[eid] in WITH_ATTRS:
                            car[KEY[eid] + "_attrs"] = st.get("a") or {}
                for eid, ch in (ev.get("c") or {}).items():
                    new = (ch or {}).get("+") or {}
                    if eid in KEY and "s" in new:
                        car[KEY[eid]] = value(eid, new["s"]); dirty = True
                    if eid in KEY and KEY[eid] in WITH_ATTRS and "a" in new:
                        car[KEY[eid] + "_attrs"] = {**car.get(KEY[eid] + "_attrs", {}), **new["a"]}; dirty = True
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            ls = (car.get("last_shift"), (car.get("last_shift_attrs") or {}).get("seconds"), car.get("drive"))
            if ls != last_seen:
                if last_seen is not None:
                    asked_at = now
                last_seen = ls
            if asked_at and now - asked_at >= 3.5:
                want_log, asked_at = True, 0.0
            if dirty and now - sent_at >= MIN_GAP_S:
                await asyncio.to_thread(post, {**car, "at": time.time()})
                sent_at, dirty = now, False


async def main() -> int:
    if not TOKEN:
        print("no HA_TOKEN (see /etc/default/comand-vehicle)", file=sys.stderr)
        return 1
    online = None
    while True:
        for url in URLS:
            try:
                await session(url)
            except PermissionError as e:
                print(e, flush=True)
            except Exception as e:  # noqa: BLE001 - unreachable, closed, timed out
                print(f"{url}: {type(e).__name__}: {e}", flush=True)
            if online is not False:
                await asyncio.to_thread(post, None)      # say once that the car is not reachable
                online = False
        await asyncio.sleep(10)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
