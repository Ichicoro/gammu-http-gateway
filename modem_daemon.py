#!/usr/bin/env python3
"""
Systemd-compatible daemon that:
  - Exposes modem data (network, signal, battery, device info) via HTTP + WebSocket
  - Polls for new SMS every POLL_INTERVAL seconds, saves to SQLite, and optionally
    executes ON_SMS_SCRIPT with SMS_1_NUMBER / SMS_1_TEXT env vars

Config (env vars):
  HOST              bind address          (default: 0.0.0.0)
  PORT              bind port             (default: 8080)
  POLL_INTERVAL     SMS poll seconds; 0=disabled (default: 30)
  ON_SMS_SCRIPT     path to script to exec on new SMS (default: none)
  DB_PATH           SQLite db path        (default: sms.db)
  GAMMU_CONFIG      gammu config file     (default: gammu auto-detect)
  STATUS_INTERVAL   status push/script interval secs (default: 60)
  ON_STATUS_SCRIPT  path to script to exec on each status tick (default: none)

HTTP endpoints:
  GET /status       network + signal + battery
  GET /network      network info
  GET /signal       signal quality
  GET /battery      battery status
  GET /info         device info (IMEI, model, firmware)
  GET /messages     messages from DB (?limit=N, default 100)

WebSocket:
  WS /ws            receives {"event":"sms",...} and {"event":"status",...} pushes
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import dotenv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import gammu
from aiohttp import web, ClientSession

dotenv.load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("modem_daemon")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
ON_SMS_SCRIPT = os.environ.get("ON_SMS_SCRIPT", "")
ON_SMS_WEBHOOK = os.environ.get("ON_SMS_WEBHOOK", "")
ON_STATUS_SCRIPT = os.environ.get("ON_STATUS_SCRIPT", "")
ON_STATUS_WEBHOOK = os.environ.get("ON_STATUS_WEBHOOK", "")
DB_PATH = os.environ.get("DB_PATH", "sms.db")
GAMMU_CONFIG = os.environ.get("GAMMU_CONFIG", "")
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "60"))

_executor = ThreadPoolExecutor(max_workers=2)
_ws_clients: set[web.WebSocketResponse] = set()
_sm: Optional[gammu.StateMachine] = None


# ---------------------------------------------------------------------------
# Gammu helpers (all blocking — run in executor)
# ---------------------------------------------------------------------------

def _get_state_machine() -> gammu.StateMachine:
    global _sm
    if _sm is None:
        sm = gammu.StateMachine()
        if GAMMU_CONFIG:
            sm.ReadConfig(Filename=GAMMU_CONFIG)
        else:
            sm.ReadConfig()
        sm.Init()
        _sm = sm
    return _sm


def _network_info() -> dict:
    try:
        info = _get_state_machine().GetNetworkInfo()
        return {
            "network_name": info.get("NetworkName", ""),
            "state": str(info.get("State", "")),
            "network_code": info.get("NetworkCode", ""),
            "cell_id": info.get("CID", ""),
            "lac": info.get("LAC", ""),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _signal_quality() -> dict:
    try:
        sig = _get_state_machine().GetSignalQuality()
        return {
            "signal_strength": sig.get("SignalStrength", -1),
            "signal_percent": sig.get("SignalPercent", -1),
            "bit_error_rate": sig.get("BitErrorRate", -1),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _battery_status() -> dict:
    try:
        bat = _get_state_machine().GetBatteryCharge()
        return {
            "battery_percent": bat.get("BatteryPercent", -1),
            "charge_state": str(bat.get("ChargeState", "")),
            "battery_voltage": bat.get("BatteryVoltage", -1),
            "charge_voltage": bat.get("ChargeVoltage", -1),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _device_info() -> dict:
    try:
        sm = _get_state_machine()
        model = sm.GetModel()
        firmware = sm.GetFirmware()
        return {
            "imei": sm.GetIMEI(),
            "manufacturer": sm.GetManufacturer(),
            "model": model[0] if model else "",
            "model_extended": model[1] if model and len(model) > 1 else "",
            "firmware_version": firmware[0] if firmware else "",
            "firmware_date": firmware[1] if firmware and len(firmware) > 1 else "",
        }
    except Exception as exc:
        return {"error": str(exc)}


def _read_all_sms() -> list[dict]:
    """Return all SMS currently stored on the modem (folder 0 = inbox)."""
    sm = _get_state_machine()
    messages = []
    start = True
    location = 0
    while True:
        try:
            parts = sm.GetNextSMS(Folder=0, Start=start, Location=location)
            start = False
            for msg in parts:
                location = msg.get("Location", location)
                messages.append({
                    "number": msg.get("Number", ""),
                    "text": msg.get("Text", ""),
                    "date": str(msg.get("DateTime", "")),
                    "location": msg.get("Location"),
                    "state": str(msg.get("State", "")),
                })
        except gammu.GSMError:
            break
    return messages


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def _db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            number       TEXT    NOT NULL,
            text         TEXT    NOT NULL,
            date         TEXT    NOT NULL,
            state        TEXT    NOT NULL DEFAULT '',
            direction    TEXT    NOT NULL DEFAULT 'incoming',
            location     INTEGER,
            received_at  TEXT    NOT NULL DEFAULT (datetime('now', 'utc'))
        )
    """)
    con.commit()
    con.close()


def _db_save(number: str, text: str, date: str, state: str = "",
             direction: str = "incoming", location: Optional[int] = None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO messages (number, text, date, state, direction, location)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (number, text, date, state, direction, location),
    )
    con.commit()
    con.close()


def _db_messages(limit: int = 100) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM messages ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

async def _broadcast(data: dict):
    if not _ws_clients:
        return
    payload = json.dumps(data)
    dead: set[web.WebSocketResponse] = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def handle_status(req: web.Request) -> web.Response:
    network, signal, battery = await asyncio.gather(
        _run(_network_info),
        _run(_signal_quality),
        _run(_battery_status),
    )
    return web.json_response({
        "network": network,
        "signal": signal,
        "battery": battery,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_network(req: web.Request) -> web.Response:
    return web.json_response(await _run(_network_info))


async def handle_signal(req: web.Request) -> web.Response:
    return web.json_response(await _run(_signal_quality))


async def handle_battery(req: web.Request) -> web.Response:
    return web.json_response(await _run(_battery_status))


async def handle_device_info(req: web.Request) -> web.Response:
    return web.json_response(await _run(_device_info))


async def handle_messages(req: web.Request) -> web.Response:
    limit = int(req.query.get("limit", "100"))
    return web.json_response(_db_messages(limit))


async def handle_ws(req: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    _ws_clients.add(ws)
    log.info("WS connected (total=%d)", len(_ws_clients))
    try:
        async for _ in ws:
            pass  # clients are receive-only; ignore any incoming frames
    finally:
        _ws_clients.discard(ws)
        log.info("WS disconnected (total=%d)", len(_ws_clients))
    return ws


# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------

async def _run_script(script: str, number: str, text: str):
    env = {**os.environ, "SMS_1_NUMBER": number, "SMS_1_TEXT": text}
    try:
        proc = await asyncio.create_subprocess_exec(
            script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            log.info("[script] %s", line)
        if proc.returncode != 0:
            log.warning("[script] exited with code %d", proc.returncode)
    except Exception as exc:
        log.error("ON_SMS_SCRIPT exec failed: %s", exc)


async def _call_webhook(url: str, payload: dict):
    try:
        async with ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    log.warning("[webhook] %s returned %d", url, resp.status)
    except Exception as exc:
        log.error("[webhook] %s failed: %s", url, exc)


async def _run_status_script(script: str, network: dict, signal: dict, battery: dict, timestamp: str):
    flat = {
        "STATUS_NETWORK_NAME":     network.get("network_name", ""),
        "STATUS_NETWORK_STATE":    network.get("state", ""),
        "STATUS_NETWORK_CODE":     network.get("network_code", ""),
        "STATUS_CELL_ID":          network.get("cell_id", ""),
        "STATUS_LAC":              network.get("lac", ""),
        "STATUS_SIGNAL_STRENGTH":  str(signal.get("signal_strength", "")),
        "STATUS_SIGNAL_PERCENT":   str(signal.get("signal_percent", "")),
        "STATUS_BIT_ERROR_RATE":   str(signal.get("bit_error_rate", "")),
        "STATUS_BATTERY_PERCENT":  str(battery.get("battery_percent", "")),
        "STATUS_CHARGE_STATE":     battery.get("charge_state", ""),
        "STATUS_BATTERY_VOLTAGE":  str(battery.get("battery_voltage", "")),
        "STATUS_CHARGE_VOLTAGE":   str(battery.get("charge_voltage", "")),
        "STATUS_TIMESTAMP":        timestamp,
    }
    env = {**os.environ, **flat}
    try:
        proc = await asyncio.create_subprocess_exec(
            script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            log.info("[status-script] %s", line)
        if proc.returncode != 0:
            log.warning("[status-script] exited with code %d", proc.returncode)
    except Exception as exc:
        log.error("ON_STATUS_SCRIPT exec failed: %s", exc)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def task_poll_sms():
    loop = asyncio.get_event_loop()

    # Seed known locations from the modem at startup so we don't re-fire
    # the script for messages that already existed before this daemon started.
    known: set[int] = set()
    try:
        existing = await loop.run_in_executor(_executor, _read_all_sms)
        for m in existing:
            if m["location"] is not None:
                known.add(m["location"])
        log.info("Seeded %d existing SMS locations", len(known))
    except Exception as exc:
        log.warning("Could not seed existing SMS: %s", exc)

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            messages = await loop.run_in_executor(_executor, _read_all_sms)
        except Exception as exc:
            log.error("SMS read failed: %s", exc)
            continue

        for msg in messages:
            loc = msg["location"]
            if loc in known:
                continue
            known.add(loc)

            log.info("New SMS from %s (loc=%s)", msg["number"], loc)
            _db_save(
                number=msg["number"],
                text=msg["text"],
                date=msg["date"],
                state=msg["state"],
                location=loc,
            )

            await _broadcast({"event": "sms", "message": msg})

            if ON_SMS_SCRIPT:
                asyncio.create_task(
                    _run_script(ON_SMS_SCRIPT, msg["number"], msg["text"])
                )
            if ON_SMS_WEBHOOK:
                asyncio.create_task(
                    _call_webhook(ON_SMS_WEBHOOK, {"number": msg["number"], "text": msg["text"]})
                )


async def task_broadcast_status():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(STATUS_INTERVAL)
        if not _ws_clients and not ON_STATUS_SCRIPT and not ON_STATUS_WEBHOOK:
            continue
        try:
            network, signal, battery = await asyncio.gather(
                loop.run_in_executor(_executor, _network_info),
                loop.run_in_executor(_executor, _signal_quality),
                loop.run_in_executor(_executor, _battery_status),
            )
            timestamp = datetime.now(timezone.utc).isoformat()
            if _ws_clients:
                await _broadcast({
                    "event": "status",
                    "network": network,
                    "signal": signal,
                    "battery": battery,
                    "timestamp": timestamp,
                })
            if ON_STATUS_SCRIPT:
                asyncio.create_task(
                    _run_status_script(ON_STATUS_SCRIPT, network, signal, battery, timestamp)
                )
            if ON_STATUS_WEBHOOK:
                asyncio.create_task(
                    _call_webhook(ON_STATUS_WEBHOOK, {
                        "network": network, "signal": signal,
                        "battery": battery, "timestamp": timestamp,
                    })
                )
        except Exception as exc:
            log.error("Status broadcast failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    _db_init()

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_executor, _get_state_machine)
        log.info("Gammu initialized")
    except Exception as exc:
        log.error("Gammu init failed: %s", exc)
        sys.exit(1)

    app = web.Application()
    app.router.add_get("/status", handle_status)
    app.router.add_get("/network", handle_network)
    app.router.add_get("/signal", handle_signal)
    app.router.add_get("/battery", handle_battery)
    app.router.add_get("/info", handle_device_info)
    app.router.add_get("/messages", handle_messages)
    app.router.add_get("/ws", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("Listening on %s:%d", HOST, PORT)

    tasks = [asyncio.create_task(task_broadcast_status())]
    if POLL_INTERVAL > 0:
        tasks.append(asyncio.create_task(task_poll_sms()))
        log.info(
            "SMS polling every %ds | script: %s",
            POLL_INTERVAL,
            ON_SMS_SCRIPT or "(none)",
        )
    else:
        log.info("SMS polling disabled (POLL_INTERVAL=0)")

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
