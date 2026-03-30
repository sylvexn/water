#!/usr/bin/env python3
"""
WaterH BLE Collector
Connects to the bottle, polls every 30s, stores in local SQLite,
and pushes new sips to the remote API server.
"""

import asyncio
import json
import os
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

# --- Config ---
BOTTLE_ADDR = os.environ.get("WATERH_ADDR", "A4:C1:38:32:D7:DE")
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"
SYNC_CMD = bytes([0x03])
POLL_INTERVAL = int(os.environ.get("WATERH_POLL_INTERVAL", "30"))
API_URL = os.environ.get("WATERH_API_URL", "https://water.syl.rest/api/ingest")
API_TOKEN = os.environ.get("WATERH_API_TOKEN", "")
DB_PATH = os.environ.get("WATERH_DB_PATH", str(Path(__file__).parent / "waterh.db"))


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
                print(f"[PUSH] Pushed {len(ids)} sips to remote")
            else:
                print(f"[PUSH] Remote returned {resp.status}")
    except urllib.error.URLError as e:
        print(f"[PUSH] Failed: {e}")
    except Exception as e:
        print(f"[PUSH] Error: {e}")


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


# --- BLE ---

pending_packets = []


def on_notify(sender, data: bytearray):
    pending_packets.append(bytes(data))


async def ble_loop():
    db = init_db()
    print(f"[DB] Initialized at {DB_PATH}")

    while True:
        try:
            print(f"[BLE] Scanning for {BOTTLE_ADDR}...")
            device = await BleakScanner.find_device_by_address(BOTTLE_ADDR, timeout=15)
            if not device:
                print("[BLE] Bottle not found, retrying in 10s...")
                await asyncio.sleep(10)
                continue

            async with BleakClient(device) as client:
                print(f"[BLE] Connected to {device.name}")
                await client.start_notify(NOTIFY_CHAR, on_notify)

                while client.is_connected:
                    try:
                        await client.write_gatt_char(WRITE_CHAR, SYNC_CMD, response=False)
                    except Exception as e:
                        print(f"[BLE] Write error: {e}")
                        break

                    await asyncio.sleep(3)

                    packets = list(pending_packets)
                    pending_packets.clear()

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

                    print(f"[BLE] {len(packets)} pkts, types: {[p[:2].hex() for p in packets]}, pt_payload: {len(pt_payload)}B")
                    sips = parse_pt(pt_payload)

                    new_count = store_sips(db, sips, rt_raw, rp_raw)
                    total_today = db.execute(
                        "SELECT COALESCE(SUM(intake_ml), 0) FROM sips WHERE DATE(timestamp) = DATE('now')"
                    ).fetchone()[0]

                    print(f"[BLE] Synced — {len(sips)} sips ({new_count} new), {total_today}ml today")

                    push_to_remote(db)
                    await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"[BLE] Error: {e}")

        print("[BLE] Disconnected, reconnecting in 5s...")
        await asyncio.sleep(5)


def main():
    print(f"[INIT] WaterH Collector")
    print(f"[INIT] Bottle: {BOTTLE_ADDR}")
    print(f"[INIT] API: {API_URL}")
    print(f"[INIT] Poll interval: {POLL_INTERVAL}s")
    asyncio.run(ble_loop())


if __name__ == "__main__":
    main()
