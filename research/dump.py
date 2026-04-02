#!/usr/bin/env python3
"""
WaterH BLE protocol probe — find commands to dump/clear bottle storage.

Run on fridge after stopping the collector:
  sudo systemctl stop waterh
  python3 dump.py
  sudo systemctl start waterh
"""

import asyncio
from datetime import datetime
from bleak import BleakClient, BleakScanner

ADDR = "A4:C1:38:32:D7:DE"
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_CHAR  = "0000ffe9-0000-1000-8000-00805f9b34fb"

notifications: list[tuple[str, bytes]] = []


def on_notify(sender, data: bytearray):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    raw = bytes(data)
    ascii_repr = ''.join(chr(b) if 32 <= b < 127 else '.' for b in raw)
    print(f"  << [{ts}] ({len(raw):>2}B) {raw.hex(' ')}  |{ascii_repr}|")
    notifications.append((ts, raw))


async def send(client, cmd: bytes, label: str = "", wait: float = 2.0):
    """Send a command, wait for notifications, return received packets."""
    notifications.clear()
    hex_str = cmd.hex(' ')
    print(f"\n>> {label or hex_str} [{hex_str}]")
    try:
        await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
    except Exception as e:
        print(f"   Write error: {e}")
        return []
    await asyncio.sleep(wait)
    return [n[1] for n in notifications]


async def main():
    print(f"Scanning for {ADDR}...")
    device = await BleakScanner.find_device_by_address(ADDR, timeout=15)
    if not device:
        print("Not found!")
        return

    async with BleakClient(device) as client:
        print(f"Connected to {device.name}\n")
        await client.start_notify(NOTIFY_CHAR, on_notify)

        # === 1. Time sync ===
        print("=" * 70)
        print("PHASE 1: Set bottle clock to current time")
        print("=" * 70)
        now = datetime.now()
        time_cmd = bytes([0x02, now.year - 2000, now.month, now.day,
                          now.hour, now.minute, now.second])
        await send(client, time_cmd, "TIME SET", wait=2)

        # === 2. Basic sync (what we currently use) ===
        print("\n" + "=" * 70)
        print("PHASE 2: Current sync command (0x03)")
        print("=" * 70)
        pkts = await send(client, bytes([0x03]), "SYNC 0x03", wait=4)
        if pkts:
            for p in pkts:
                if p[:2] == b"PT":
                    print(f"   PT header (6B): {p[:6].hex(' ')}  payload: {len(p)-6}B")

        # === 3. Sync with parameters — maybe page/offset ===
        print("\n" + "=" * 70)
        print("PHASE 3: Sync 0x03 with parameters (page/offset?)")
        print("=" * 70)
        for param in [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0xFF]:
            await send(client, bytes([0x03, param]), f"SYNC 0x03 + 0x{param:02x}", wait=3)

        # === 4. Commands 0x04-0x0F — look for other data commands ===
        print("\n" + "=" * 70)
        print("PHASE 4: Single byte commands 0x04-0x0F")
        print("=" * 70)
        for i in range(0x04, 0x10):
            await send(client, bytes([i]), f"CMD 0x{i:02x}", wait=2)

        # === 5. Commands 0x10-0x20 ===
        print("\n" + "=" * 70)
        print("PHASE 5: Single byte commands 0x10-0x20")
        print("=" * 70)
        for i in range(0x10, 0x21):
            await send(client, bytes([i]), f"CMD 0x{i:02x}", wait=1.5)

        # === 6. Try GT (Get Today?), DT (Delete?), ST (Set?), etc. ===
        print("\n" + "=" * 70)
        print("PHASE 6: Two-byte ASCII commands that might dump/clear data")
        print("=" * 70)
        for prefix in [b"GT", b"DT", b"ST", b"CL", b"DL", b"DC", b"RS",
                        b"RD", b"RC", b"DR", b"WR", b"GS", b"GA"]:
            await send(client, prefix, f"ASCII '{prefix.decode()}'", wait=2)

        # === 7. Send sync with today's date ===
        print("\n" + "=" * 70)
        print("PHASE 7: Sync 0x03 + today's date bytes")
        print("=" * 70)
        date_bytes = bytes([now.year - 2000, now.month, now.day])
        await send(client, bytes([0x03]) + date_bytes, "SYNC + today date", wait=4)
        # Also try with 0x00 prefix
        await send(client, bytes([0x03, 0x00]) + date_bytes, "SYNC + 0x00 + today", wait=4)

        # === 8. After all probing, try the basic sync again ===
        print("\n" + "=" * 70)
        print("PHASE 8: Final sync (did any command change state?)")
        print("=" * 70)
        pkts = await send(client, bytes([0x03]), "SYNC 0x03 (final)", wait=4)
        if pkts:
            for p in pkts:
                if p[:2] == b"PT":
                    print(f"   PT header (6B): {p[:6].hex(' ')}  payload: {len(p)-6}B")

        # === 9. Passive listen — take a sip during this window ===
        print("\n" + "=" * 70)
        print("PHASE 9: Passive listen 30s — TAKE A SIP NOW")
        print("=" * 70)
        notifications.clear()
        await asyncio.sleep(30)

        if notifications:
            print(f"\n   Got {len(notifications)} passive notifications!")
        else:
            print("\n   No passive notifications received.")

        # === 10. Sync again after sip ===
        print("\n" + "=" * 70)
        print("PHASE 10: Sync after sip")
        print("=" * 70)
        await send(client, bytes([0x03]), "SYNC 0x03 (post-sip)", wait=4)

        await client.stop_notify(NOTIFY_CHAR)

    print("\n\nDone.")


asyncio.run(main())
