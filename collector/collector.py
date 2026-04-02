#!/usr/bin/env python3
"""
WaterH BLE Collector
Connects to the bottle, polls every 30s, stores in local SQLite,
and pushes new sips to the remote API server.
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("waterh")

# --- Config ---
BOTTLE_ADDR = os.environ.get("WATERH_ADDR", "A4:C1:38:32:D7:DE")
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"
SYNC_CMD = bytes([0x03])
POLL_INTERVAL = int(os.environ.get("WATERH_POLL_INTERVAL", "30"))
API_URL = os.environ.get("WATERH_API_URL", "https://water.syl.rest/api/ingest")
HEARTBEAT_URL = os.environ.get("WATERH_HEARTBEAT_URL", "https://water.syl.rest/api/heartbeat")
API_TOKEN = os.environ.get("WATERH_API_TOKEN", "")
DB_PATH = os.environ.get("WATERH_DB_PATH", str(Path(__file__).parent / "waterh.db"))

MAX_SCAN_FAILURES = 3
BACKOFF_BASE = 5       # seconds
BACKOFF_CAP = 300      # 5 minutes


# --- Database ---

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sips (
            id INTEGER PRIMARY KEY,
            timestamp TEXT UNIQUE NOT NULL,
            intake_ml INTEGER NOT NULL,
            temp_c REAL,
            unknown INTEGER,
            raw_hex TEXT,
            synced INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS syncs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            rt_raw TEXT,
            rp_raw TEXT,
            sip_count INTEGER
        )
    """)
    db.commit()
    return db


def store_sips(db, sips, rt_raw=None, rp_raw=None):
    """Store sips, deduplicating by timestamp. Returns count of new sips."""
    new_count = 0
    for sip in sips:
        try:
            db.execute(
                "INSERT INTO sips (timestamp, intake_ml, temp_c, unknown, raw_hex) VALUES (?, ?, ?, ?, ?)",
                (sip["timestamp"], sip["intake_ml"], sip["temp_c"], sip["unknown"], sip["raw"]),
            )
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # duplicate timestamp, skip

    db.execute(
        "INSERT INTO syncs (timestamp, rt_raw, rp_raw, sip_count) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), rt_raw, rp_raw, len(sips)),
    )
    db.commit()
    return new_count


def get_unsynced(db):
    """Get sips not yet pushed to remote."""
    rows = db.execute(
        "SELECT id, timestamp, intake_ml, temp_c, unknown, raw_hex FROM sips WHERE synced = 0"
    ).fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "intake_ml": r[2], "temp_c": r[3], "unknown": r[4], "raw_hex": r[5]}
        for r in rows
    ]


def mark_synced(db, ids):
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    db.execute(f"UPDATE sips SET synced = 1 WHERE id IN ({placeholders})", ids)
    db.commit()


# --- Remote push ---

def push_to_remote(db):
    """Push unsynced sips to the API server."""
    if not API_TOKEN:
        return

    unsynced = get_unsynced(db)
    if not unsynced:
        return

    payload = json.dumps({"sips": unsynced}).encode()
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                ids = [s["id"] for s in unsynced]
                mark_synced(db, ids)
                log.info(f"[PUSH] Pushed {len(ids)} sips to remote")
            else:
                log.warning(f"[PUSH] Remote returned {resp.status}")
    except urllib.error.URLError as e:
        log.warning(f"[PUSH] Failed: {e}")
    except Exception as e:
        log.error(f"[PUSH] Error: {e}")


# --- Heartbeat ---

def post_heartbeat(state: str, detail: str = ""):
    """POST collector state to backend. Fire-and-forget, never raises."""
    if not API_TOKEN:
        return
    payload = json.dumps({
        "state": state,
        "detail": detail,
        "timestamp": datetime.now().isoformat(),
    }).encode()
    req = urllib.request.Request(
        HEARTBEAT_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_TOKEN}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # best-effort


# --- Adapter reset ---

def reset_adapter():
    """Reset the Bluetooth adapter to recover from a wedged BlueZ stack."""
    log.warning("[BLE] Resetting hci0 adapter")
    try:
        subprocess.run(["hciconfig", "hci0", "reset"], check=True, timeout=10)
        time.sleep(2)
    except FileNotFoundError:
        log.warning("[BLE] hciconfig not found, trying bluetoothctl")
        try:
            subprocess.run(["bluetoothctl", "power", "off"], check=True, timeout=5)
            time.sleep(1)
            subprocess.run(["bluetoothctl", "power", "on"], check=True, timeout=5)
            time.sleep(2)
        except Exception as e:
            log.error(f"[BLE] bluetoothctl reset failed: {e}")
    except Exception as e:
        log.error(f"[BLE] Adapter reset failed: {e}")


# --- Packet parsers ---

def parse_pt(payload: bytes):
    """Parse PT payload — sip history records (header already stripped)."""
    record_size = 13
    if len(payload) < record_size:
        return []
    records = []

    for i in range(0, len(payload) - record_size + 1, record_size):
        rec = payload[i : i + record_size]
        year = 2000 + rec[0]
        month = rec[1]
        day = rec[2]
        hour = rec[3]
        minute = rec[4]
        second = rec[5]
        intake_ml = (rec[6] << 8) | rec[7]
        unknown = (rec[8] << 8) | rec[9]
        temp_raw = (rec[10] << 8) | rec[11]
        temp_c = temp_raw / 10.0

        try:
            ts = datetime(year, month, day, hour, minute, second)
            timestamp = ts.isoformat()
        except ValueError:
            timestamp = f"{year}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"

        records.append({
            "timestamp": timestamp,
            "intake_ml": intake_ml,
            "temp_c": temp_c,
            "unknown": unknown,
            "raw": rec.hex(" "),
        })

    return records


# --- Queue helpers ---

def drain_queue(q: asyncio.Queue) -> list[bytes]:
    items = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


# --- BLE ---

async def ble_loop():
    db = init_db()
    log.info(f"[DB] Initialized at {DB_PATH}")

    packet_queue: asyncio.Queue[bytes] = asyncio.Queue()
    scan_failures = 0
    backoff = BACKOFF_BASE

    post_heartbeat("starting")

    while True:
        # --- Scan phase ---
        log.info(f"[BLE] Scanning for {BOTTLE_ADDR}...")
        post_heartbeat("scanning")

        try:
            device = await BleakScanner.find_device_by_address(BOTTLE_ADDR, timeout=15)
        except Exception as e:
            log.error(f"[BLE] Scan error: {e}")
            device = None

        if not device:
            scan_failures += 1
            if scan_failures >= MAX_SCAN_FAILURES:
                reset_adapter()
                scan_failures = 0

            log.warning(f"[BLE] Bottle not found (attempt {scan_failures}), retry in {backoff}s")
            post_heartbeat("scanning", f"not found, retry {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)
            continue

        # Successful scan — reset counters
        scan_failures = 0
        backoff = BACKOFF_BASE

        # --- Connect phase ---
        disconnected_event = asyncio.Event()

        def on_disconnect(client):
            log.warning("[BLE] Disconnect callback fired")
            disconnected_event.set()

        try:
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                log.info(f"[BLE] Connected to {device.name}")
                post_heartbeat("connected")

                def on_notify(sender, data: bytearray):
                    packet_queue.put_nowait(bytes(data))

                await client.start_notify(NOTIFY_CHAR, on_notify)

                while client.is_connected and not disconnected_event.is_set():
                    try:
                        await client.write_gatt_char(WRITE_CHAR, SYNC_CMD, response=False)
                    except Exception as e:
                        log.error(f"[BLE] Write error: {e}")
                        break

                    await asyncio.sleep(3)

                    packets = drain_queue(packet_queue)

                    rt_raw = None
                    rp_raw = None
                    pt_payload = b""
                    in_pt = False

                    for pkt in packets:
                        if pkt[:2] == b"RT":
                            rt_raw = pkt.hex(" ")
                            in_pt = False
                        elif pkt[:2] == b"RP":
                            rp_raw = pkt.hex(" ")
                            in_pt = False
                        elif pkt[:2] == b"PT":
                            pt_payload = pkt[6:]  # skip 6-byte PT header
                            in_pt = True
                        elif in_pt:
                            pt_payload += pkt[2:]  # skip 2-byte continuation header

                    log.info(f"[BLE] {len(packets)} pkts, types: {[p[:2].hex() for p in packets]}, pt_payload: {len(pt_payload)}B")
                    sips = parse_pt(pt_payload)

                    new_count = store_sips(db, sips, rt_raw, rp_raw)
                    total_today = db.execute(
                        "SELECT COALESCE(SUM(intake_ml), 0) FROM sips WHERE DATE(timestamp) = DATE('now')"
                    ).fetchone()[0]

                    log.info(f"[BLE] Synced — {len(sips)} sips ({new_count} new), {total_today}ml today")

                    push_to_remote(db)
                    post_heartbeat("connected", f"{len(sips)} sips, {new_count} new")

                    # Reset backoff on successful poll cycle
                    backoff = BACKOFF_BASE
                    await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error(f"[BLE] Error: {e}")
            post_heartbeat("error", str(e))

        log.info(f"[BLE] Disconnected, reconnecting in {backoff}s...")
        post_heartbeat("scanning", "disconnected, reconnecting")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_CAP)


def main():
    log.info(f"[INIT] WaterH Collector")
    log.info(f"[INIT] Bottle: {BOTTLE_ADDR}")
    log.info(f"[INIT] API: {API_URL}")
    log.info(f"[INIT] Heartbeat: {HEARTBEAT_URL}")
    log.info(f"[INIT] Poll interval: {POLL_INTERVAL}s")
    asyncio.run(ble_loop())


if __name__ == "__main__":
    main()
