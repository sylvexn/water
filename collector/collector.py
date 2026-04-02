#!/usr/bin/env python3
"""
WaterH BLE Collector — full app-protocol implementation.

Replicates the official WaterH app's sync flow:
  1. Clean BlueZ state (remove stale connections)
  2. Connect (no pairing)
  3. Request bottle info
  4. Sync time + goal + reminder settings
  5. Request water logs (sip history)
  6. Ack received logs (clears them from bottle storage)
  7. Push new sips to remote API
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
POLL_INTERVAL = int(os.environ.get("WATERH_POLL_INTERVAL", "60"))
GOAL_ML = int(os.environ.get("WATERH_GOAL_ML", "2500"))
API_URL = os.environ.get("WATERH_API_URL", "https://water.syl.rest/api/ingest")
HEARTBEAT_URL = os.environ.get("WATERH_HEARTBEAT_URL", "https://water.syl.rest/api/heartbeat")
API_TOKEN = os.environ.get("WATERH_API_TOKEN", "")
DB_PATH = os.environ.get("WATERH_DB_PATH", str(Path(__file__).parent / "waterh.db"))

CMD_PORT = int(os.environ.get("WATERH_CMD_PORT", "7700"))

MAX_SCAN_FAILURES = 3
MAX_EMPTY_POLLS = 5
BACKOFF_BASE = 5
BACKOFF_CAP = 300


# --- Protocol commands ---

def cmd_bottle_data() -> bytes:
    return bytes.fromhex("47540001ff")


def cmd_sync_settings(goal_ml: int = GOAL_ML) -> bytes:
    now = datetime.now()
    goal_hex = f"{goal_ml:04x}"
    reminder_hex = "00080014003c"
    time_hex = (
        f"{(now.year - 2000):02x}"
        f"{now.month:02x}"
        f"{now.day:02x}"
        f"{now.hour:02x}"
        f"{now.minute:02x}"
        f"{now.second:02x}"
    )
    return bytes.fromhex(f"505400140305{goal_hex}0703{time_hex}0726{reminder_hex}")


def cmd_request_water_logs() -> bytes:
    return bytes.fromhex("4754000106")


def cmd_ack_water_logs(total_bytes: int) -> bytes:
    return bytes.fromhex(f"525000040306{total_bytes:04x}")


def cmd_sync_today_amount(ml: int) -> bytes:
    return bytes.fromhex(f"505400040304{ml:04x}")


def cmd_clear_offline() -> bytes:
    return bytes.fromhex("50540003021c05")


def cmd_flash_led() -> bytes:
    return bytes.fromhex("50540003021d01")


def cmd_set_led(mode: str, color: str) -> bytes:
    modes = {
        "default": "00", "breathe": "01", "calm": "02",
        "rainbow": "03", "warmth": "05", "christmas": "06",
    }
    colors = {
        "red": "ff0000", "yellow": "ffff00", "green": "00ff00",
        "cyan": "00ffff", "blue": "0000ff", "purple": "ff00ff",
        "white": "ffffff",
    }
    mode_hex = modes.get(mode, "00")
    # Accept named color or raw hex
    color_hex = colors.get(color, color if len(color) == 6 else "0000ff")
    return bytes.fromhex(f"5054000605fb{mode_hex}{color_hex}")


def cmd_set_reminder(on: bool, wake_h: int, wake_m: int, sleep_h: int, sleep_m: int, interval_min: int) -> bytes:
    type_byte = "01" if on else "00"
    return bytes.fromhex(
        f"505400080726{type_byte}"
        f"{wake_h:02x}{wake_m:02x}"
        f"{sleep_h:02x}{sleep_m:02x}"
        f"{interval_min:02x}"
    )


def cmd_set_goal(ml: int) -> bytes:
    return bytes.fromhex(f"505400040305{ml:04x}")


def cmd_recalibrate(full: bool) -> bytes:
    return bytes.fromhex("5054000302A101" if full else "5054000302A601")


# --- Command queue (shared between HTTP server and BLE loop) ---
# Initialized in ble_loop() once the event loop is running.

cmd_queue: asyncio.Queue[tuple[bytes, str]] | None = None


# --- Command HTTP server ---

async def handle_cmd_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Minimal HTTP handler for /commands endpoint."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        request_str = request_line.decode(errors="replace")
        # Read headers
        content_length = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":")[1].strip())

        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5)

        method = request_str.split(" ")[0] if request_str else ""
        path = request_str.split(" ")[1] if len(request_str.split(" ")) > 1 else "/"

        if method == "GET" and path == "/commands":
            resp = {"commands": [
                "POST /commands/flash",
                "POST /commands/led    {mode, color}",
                "POST /commands/goal   {ml}",
                "POST /commands/intake {ml}",
                "POST /commands/reminder {on, wake, sleep, interval}",
                "POST /commands/calibrate {full}",
                "POST /commands/raw    {hex}",
            ]}
            send_json(writer, 200, resp)

        elif method == "POST" and path == "/commands/flash":
            cmd_queue.put_nowait((cmd_flash_led(), "flash"))
            send_json(writer, 200, {"ok": True, "queued": "flash"})

        elif method == "POST" and path == "/commands/led":
            data = json.loads(body) if body else {}
            mode = data.get("mode", "default")
            color = data.get("color", "blue")
            cmd_queue.put_nowait((cmd_set_led(mode, color), f"led {mode} {color}"))
            send_json(writer, 200, {"ok": True, "queued": f"led {mode} {color}"})

        elif method == "POST" and path == "/commands/goal":
            data = json.loads(body) if body else {}
            ml = int(data.get("ml", GOAL_ML))
            cmd_queue.put_nowait((cmd_set_goal(ml), f"goal {ml}ml"))
            send_json(writer, 200, {"ok": True, "queued": f"goal {ml}ml"})

        elif method == "POST" and path == "/commands/intake":
            data = json.loads(body) if body else {}
            ml = int(data.get("ml", 0))
            cmd_queue.put_nowait((cmd_sync_today_amount(ml), f"intake {ml}ml"))
            send_json(writer, 200, {"ok": True, "queued": f"intake {ml}ml"})

        elif method == "POST" and path == "/commands/reminder":
            data = json.loads(body) if body else {}
            on = data.get("on", False)
            wake = data.get("wake", "08:00").split(":")
            slp = data.get("sleep", "20:00").split(":")
            interval = int(data.get("interval", 60))
            cmd = cmd_set_reminder(on, int(wake[0]), int(wake[1]), int(slp[0]), int(slp[1]), interval)
            label = f"reminder {'on' if on else 'off'}"
            cmd_queue.put_nowait((cmd, label))
            send_json(writer, 200, {"ok": True, "queued": label})

        elif method == "POST" and path == "/commands/calibrate":
            data = json.loads(body) if body else {}
            full = data.get("full", True)
            cmd_queue.put_nowait((cmd_recalibrate(full), f"calibrate {'full' if full else 'empty'}"))
            send_json(writer, 200, {"ok": True, "queued": f"calibrate {'full' if full else 'empty'}"})

        elif method == "POST" and path == "/commands/raw":
            data = json.loads(body) if body else {}
            hex_str = data.get("hex", "").replace(" ", "")
            if not hex_str:
                send_json(writer, 400, {"error": "missing hex"})
            else:
                cmd_queue.put_nowait((bytes.fromhex(hex_str), f"raw {hex_str}"))
                send_json(writer, 200, {"ok": True, "queued": f"raw {hex_str}"})

        else:
            send_json(writer, 404, {"error": "not found"})

    except Exception as e:
        log.error(f"[HTTP] Request error: {e}")
        try:
            send_json(writer, 500, {"error": str(e)})
        except Exception:
            pass
    finally:
        writer.close()
        await writer.wait_closed()


def send_json(writer: asyncio.StreamWriter, status: int, data: dict):
    body = json.dumps(data).encode()
    status_text = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    writer.write(
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"\r\n".encode() + body
    )


async def start_cmd_server():
    server = await asyncio.start_server(handle_cmd_request, "0.0.0.0", CMD_PORT)
    log.info(f"[HTTP] Command server listening on :{CMD_PORT}")
    return server


# --- BlueZ cleanup (what Android does with gatt.close() + refreshDeviceCache) ---

def bluez_remove_device(addr: str):
    """Remove device from BlueZ to clear stale connections and GATT cache.
    This is the Linux equivalent of Android's gatt.close() + refreshDeviceCache().
    Without this, BlueZ can hold zombie connections that prevent the bottle
    from advertising."""
    log.info(f"[BLE] Clearing BlueZ state for {addr}")
    try:
        subprocess.run(
            ["bluetoothctl", "remove", addr],
            capture_output=True, timeout=5
        )
    except Exception:
        pass  # device might not exist in bluez, that's fine


def bluez_power_cycle():
    """Power cycle the Bluetooth adapter."""
    log.warning("[BLE] Power cycling Bluetooth adapter")
    try:
        subprocess.run(["bluetoothctl", "power", "off"], capture_output=True, timeout=5)
        time.sleep(1)
        subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=5)
        time.sleep(2)
    except Exception as e:
        log.error(f"[BLE] Power cycle failed: {e}")


def bluez_full_reset(addr: str):
    """Full cleanup: remove device + power cycle. Use before reconnecting
    after a stale/zombie connection."""
    bluez_remove_device(addr)
    time.sleep(1)
    bluez_power_cycle()


# --- Database ---

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sips (
            id INTEGER PRIMARY KEY,
            timestamp TEXT UNIQUE NOT NULL,
            intake_ml INTEGER NOT NULL,
            temp_c REAL,
            tds INTEGER,
            raw_hex TEXT,
            synced INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS syncs (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            sip_count INTEGER,
            new_count INTEGER,
            acked_bytes INTEGER
        )
    """)
    db.commit()
    return db


def store_sips(db, sips):
    new_count = 0
    for sip in sips:
        try:
            db.execute(
                "INSERT INTO sips (timestamp, intake_ml, temp_c, tds, raw_hex) VALUES (?, ?, ?, ?, ?)",
                (sip["timestamp"], sip["intake_ml"], sip["temp_c"], sip["tds"], sip["raw"]),
            )
            new_count += 1
        except sqlite3.IntegrityError:
            pass
    db.commit()
    return new_count


def log_sync(db, sip_count, new_count, acked_bytes):
    db.execute(
        "INSERT INTO syncs (timestamp, sip_count, new_count, acked_bytes) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), sip_count, new_count, acked_bytes),
    )
    db.commit()


def get_unsynced(db):
    rows = db.execute(
        "SELECT id, timestamp, intake_ml, temp_c, tds, raw_hex FROM sips WHERE synced = 0"
    ).fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "intake_ml": r[2], "temp_c": r[3], "tds": r[4], "raw_hex": r[5]}
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
    if not API_TOKEN:
        return
    unsynced = get_unsynced(db)
    if not unsynced:
        return
    payload = json.dumps({"sips": unsynced}).encode()
    req = urllib.request.Request(
        API_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"},
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
    except Exception as e:
        log.warning(f"[PUSH] Failed: {e}")


# --- Heartbeat ---

def post_heartbeat(state: str, detail: str = ""):
    if not API_TOKEN:
        return
    payload = json.dumps({
        "state": state, "detail": detail,
        "timestamp": datetime.now().isoformat(),
    }).encode()
    req = urllib.request.Request(
        HEARTBEAT_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_TOKEN}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# --- Packet parsing ---

def parse_pt_packets(packets: list[bytes]) -> tuple[list[dict], int]:
    pt_payload = b""
    in_pt = False
    for pkt in packets:
        if len(pkt) >= 2 and pkt[0] == 0x50 and pkt[1] == 0x54:
            pt_payload = pkt[6:]
            in_pt = True
        elif in_pt and len(pkt) >= 2:
            pt_payload += pkt[2:]

    records = []
    record_size = 13
    for i in range(0, len(pt_payload) - record_size + 1, record_size):
        rec = pt_payload[i : i + record_size]
        year = 2000 + rec[0]
        month, day = rec[1], rec[2]
        hour, minute, second = rec[3], rec[4], rec[5]
        intake_ml = (rec[6] << 8) | rec[7]
        tds = (rec[8] << 8) | rec[9]
        temp_c = ((rec[10] << 8) | rec[11]) / 10.0
        try:
            ts = datetime(year, month, day, hour, minute, second).isoformat()
        except ValueError:
            ts = f"{year}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
        records.append({
            "timestamp": ts, "intake_ml": intake_ml,
            "temp_c": temp_c, "tds": tds, "raw": rec.hex(" "),
        })
    return records, len(pt_payload)


# --- BLE helpers ---

def drain_queue(q: asyncio.Queue) -> list[bytes]:
    items = []
    while not q.empty():
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def ble_write(client, cmd: bytes, label: str):
    log.info(f"[BLE] >> {label} ({cmd.hex(' ')})")
    await client.write_gatt_char(WRITE_CHAR, cmd, response=False)


async def ble_write_and_wait(client, cmd: bytes, label: str, queue: asyncio.Queue, wait: float = 2.0) -> list[bytes]:
    drain_queue(queue)
    await ble_write(client, cmd, label)
    await asyncio.sleep(wait)
    return drain_queue(queue)


# --- Sync cycle ---

async def sync_cycle(client, queue: asyncio.Queue, db) -> bool:
    # Step 1: Request bottle data
    pkts = await ble_write_and_wait(client, cmd_bottle_data(), "bottle-data", queue, wait=2.0)
    rp_pkts = [p for p in pkts if len(p) >= 2 and p[0] == 0x52 and p[1] == 0x50]
    if rp_pkts:
        rp = rp_pkts[0]
        if len(rp) > 31:
            log.info(f"[BLE] Battery: {rp[6]}%, charging: {rp[31]}")
    else:
        log.warning("[BLE] No bottle data response")

    # Step 2: Sync settings (time + goal + reminder)
    pkts = await ble_write_and_wait(client, cmd_sync_settings(), "sync-settings", queue, wait=2.0)
    rp_pkts = [p for p in pkts if len(p) >= 2 and p[0] == 0x52 and p[1] == 0x50]
    if rp_pkts:
        rp = rp_pkts[0]
        sync_ok = len(rp) > 10 and rp[10] == 0x00
        log.info(f"[BLE] Settings sync: {'ok' if sync_ok else 'check response'}")

    # Step 3: Sync today's amount to bottle display
    total_today = db.execute(
        "SELECT COALESCE(SUM(intake_ml), 0) FROM sips WHERE DATE(timestamp) = DATE('now')"
    ).fetchone()[0]
    await ble_write_and_wait(client, cmd_sync_today_amount(total_today), "sync-display", queue, wait=1.0)

    # Step 4: Request water logs
    pkts = await ble_write_and_wait(client, cmd_request_water_logs(), "request-logs", queue, wait=4.0)

    has_data = False
    for p in pkts:
        if len(p) >= 7 and p[0] == 0x52 and p[1] == 0x50 and p[5] == 0x06:
            has_data = p[6] == 0x01
            log.info(f"[BLE] Water logs: {'data found' if has_data else 'no data'}")

    if not has_data:
        log.info(f"[BLE] No new water logs, {total_today}ml today")
        log_sync(db, 0, 0, 0)
        return True

    # Step 5: Collect all PT packets
    all_packets = list(pkts)
    await asyncio.sleep(2.0)
    all_packets.extend(drain_queue(queue))

    sips, pt_bytes = parse_pt_packets(all_packets)
    log.info(f"[BLE] Received {len(sips)} sip records ({pt_bytes}B)")

    # Step 6: Store locally
    new_count = store_sips(db, sips)
    total_today = db.execute(
        "SELECT COALESCE(SUM(intake_ml), 0) FROM sips WHERE DATE(timestamp) = DATE('now')"
    ).fetchone()[0]
    log.info(f"[BLE] Stored {len(sips)} sips ({new_count} new), {total_today}ml today")

    # Step 7: Ack + clear from bottle
    if sips:
        ack_bytes = len(sips) * 13
        await ble_write_and_wait(client, cmd_ack_water_logs(ack_bytes), "ack-logs", queue, wait=1.0)
        log.info(f"[BLE] Acked {ack_bytes}B ({len(sips)} records)")

    # Step 8: Update bottle display with new total
    await ble_write_and_wait(client, cmd_sync_today_amount(total_today), "sync-display", queue, wait=1.0)

    log_sync(db, len(sips), new_count, len(sips) * 13 if sips else 0)
    push_to_remote(db)
    return True


# --- BLE main loop ---

async def ble_loop():
    global cmd_queue
    cmd_queue = asyncio.Queue()

    db = init_db()
    log.info(f"[DB] Initialized at {DB_PATH}")

    # Start command HTTP server
    await start_cmd_server()

    packet_queue: asyncio.Queue[bytes] = asyncio.Queue()
    scan_failures = 0
    backoff = BACKOFF_BASE

    post_heartbeat("starting")

    # Clean start: remove any stale BlueZ state from previous runs
    bluez_remove_device(BOTTLE_ADDR)

    while True:
        # --- Scan ---
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
                # Full reset: remove device + power cycle adapter
                bluez_full_reset(BOTTLE_ADDR)
                scan_failures = 0
            else:
                # Light cleanup: just remove stale device reference
                bluez_remove_device(BOTTLE_ADDR)
            log.warning(f"[BLE] Not found (attempt {scan_failures}), retry in {backoff}s")
            post_heartbeat("scanning", f"not found, retry {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)
            continue

        scan_failures = 0
        backoff = BACKOFF_BASE

        # --- Connect ---
        disconnected_event = asyncio.Event()

        def on_disconnect(client):
            log.warning("[BLE] Disconnected")
            disconnected_event.set()

        try:
            async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                log.info(f"[BLE] Connected to {device.name}")
                post_heartbeat("connected")

                def on_notify(sender, data: bytearray):
                    packet_queue.put_nowait(bytes(data))

                await client.start_notify(NOTIFY_CHAR, on_notify)

                empty_cycles = 0
                while client.is_connected and not disconnected_event.is_set():
                    # Process any queued commands from the HTTP server
                    while not cmd_queue.empty():
                        try:
                            cmd, label = cmd_queue.get_nowait()
                            await ble_write(client, cmd, f"cmd: {label}")
                            await asyncio.sleep(0.5)
                        except Exception as e:
                            log.error(f"[BLE] Command error: {e}")

                    try:
                        success = await sync_cycle(client, packet_queue, db)
                        if success:
                            empty_cycles = 0
                            post_heartbeat("connected", "sync ok")
                        else:
                            empty_cycles += 1
                    except Exception as e:
                        log.error(f"[BLE] Sync cycle error: {e}")
                        empty_cycles += 1

                    if empty_cycles >= MAX_EMPTY_POLLS:
                        log.warning(f"[BLE] {empty_cycles} failed cycles, forcing reconnect")
                        post_heartbeat("scanning", "stale connection")
                        break

                    backoff = BACKOFF_BASE

                    # While waiting for next poll, check for commands every second
                    for _ in range(POLL_INTERVAL):
                        if disconnected_event.is_set():
                            break
                        if not cmd_queue.empty():
                            while not cmd_queue.empty():
                                try:
                                    cmd, label = cmd_queue.get_nowait()
                                    await ble_write(client, cmd, f"cmd: {label}")
                                    await asyncio.sleep(0.5)
                                except Exception as e:
                                    log.error(f"[BLE] Command error: {e}")
                        await asyncio.sleep(1)

        except Exception as e:
            log.error(f"[BLE] Connection error: {e}")
            post_heartbeat("error", str(e))

        # Clean up BlueZ state before reconnecting — this is the critical step
        # that prevents zombie connections. Android does this in gatt.close().
        log.info(f"[BLE] Cleaning up BlueZ state before reconnect...")
        bluez_remove_device(BOTTLE_ADDR)

        log.info(f"[BLE] Reconnecting in {backoff}s...")
        post_heartbeat("scanning", "reconnecting")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_CAP)


def main():
    log.info("[INIT] WaterH Collector (full protocol)")
    log.info(f"[INIT] Bottle: {BOTTLE_ADDR}")
    log.info(f"[INIT] Goal: {GOAL_ML}ml")
    log.info(f"[INIT] Poll interval: {POLL_INTERVAL}s")
    log.info(f"[INIT] API: {API_URL}")
    log.info(f"[INIT] Command server: :{CMD_PORT}")
    asyncio.run(ble_loop())


if __name__ == "__main__":
    main()
