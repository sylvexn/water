#!/usr/bin/env python3
"""
WaterH command tester — connects like the app does (direct by MAC, no scan).
Stop the collector first: sudo systemctl stop waterh
"""

import asyncio
from datetime import datetime
from bleak import BleakClient

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


async def send(client, cmd_hex: str, label: str, wait: float = 2.0):
    notifications.clear()
    cmd = bytes.fromhex(cmd_hex)
    print(f"\n>> {label}")
    print(f"   cmd: {cmd_hex}")
    try:
        await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
    except Exception as e:
        print(f"   Write error: {e}")
        return []
    await asyncio.sleep(wait)
    return [n[1] for n in notifications]


async def main():
    # Connect directly by MAC — no scanning, like the app does.
    # BleakClient will wait for the device to be connectable.
    print(f"Connecting directly to {ADDR} (no scan — waiting for device)...")
    print("Move/sip the bottle if it's asleep.\n")

    async with BleakClient(ADDR, timeout=30) as client:
        print(f"Connected!\n")
        await client.start_notify(NOTIFY_CHAR, on_notify)

        # =============================================
        print("=" * 70)
        print("1. GET BOTTLE DATA")
        print("=" * 70)
        pkts = await send(client, "47540001ff", "requestBottleData", wait=3)
        if pkts:
            rp = pkts[0]
            if len(rp) > 31:
                print(f"   Battery: {rp[6]}%")
                print(f"   Charging: {rp[31]}")
                fw = (rp[25] << 8) | rp[26]
                print(f"   Firmware: {fw}")

        # =============================================
        print("\n" + "=" * 70)
        print("2. SYNC TIME + GOAL 2500ml")
        print("=" * 70)
        now = datetime.now()
        goal = "09c4"  # 2500ml
        time_hex = (
            f"{(now.year - 2000):02x}"
            f"{now.month:02x}"
            f"{now.day:02x}"
            f"{now.hour:02x}"
            f"{now.minute:02x}"
            f"{now.second:02x}"
        )
        reminder = "00080014003c"
        cmd = f"505400140305{goal}0703{time_hex}0726{reminder}"
        await send(client, cmd, "syncSettings", wait=3)

        # =============================================
        print("\n" + "=" * 70)
        print("3. REQUEST WATER LOGS")
        print("=" * 70)
        pkts = await send(client, "4754000106", "requestWaterLogs", wait=5)
        for p in pkts:
            if len(p) >= 2:
                tag = ''.join(chr(b) if 32 <= b < 127 else '.' for b in p[:2])
                print(f"   Type: {tag}, len: {len(p)}")

        # =============================================
        print("\n" + "=" * 70)
        print("4. LED — Flash once")
        print("=" * 70)
        await send(client, "50540003021d01", "flash", wait=3)

        input("\n   >>> Did the LED flash? Press Enter to continue...")

        # =============================================
        print("\n" + "=" * 70)
        print("5. LED — Breathe Purple")
        print("=" * 70)
        await send(client, "5054000605fb" + "01" + "ff00ff", "breathe purple", wait=4)

        input("   >>> Press Enter for next mode...")

        # =============================================
        print("6. LED — Rainbow Pulse")
        await send(client, "5054000605fb" + "03" + "ffffff", "rainbow", wait=4)

        input("   >>> Press Enter for next mode...")

        # =============================================
        print("7. LED — Christmas Red")
        await send(client, "5054000605fb" + "06" + "ff0000", "christmas red", wait=4)

        input("   >>> Press Enter for next mode...")

        # =============================================
        print("8. LED — Calm Cyan")
        await send(client, "5054000605fb" + "02" + "00ffff", "calm cyan", wait=4)

        input("   >>> Press Enter for next mode...")

        # =============================================
        print("9. LED — Warmth Yellow")
        await send(client, "5054000605fb" + "05" + "ffff00", "warmth yellow", wait=4)

        input("   >>> Press Enter to restore default...")

        # =============================================
        print("10. LED — Default Blue (restore)")
        await send(client, "5054000605fb" + "00" + "0000ff", "default blue", wait=3)

        # =============================================
        print("\n" + "=" * 70)
        print("11. SCREEN — Set intake to 1250ml (should show 50%)")
        print("=" * 70)
        await send(client, "505400040304" + "04e2", "syncAmount 1250ml", wait=3)

        input("   >>> Check the screen — does it show 50%? Press Enter...")

        # Reset
        print("12. SCREEN — Reset to 0ml")
        await send(client, "505400040304" + "0000", "syncAmount 0ml", wait=2)

        # =============================================
        print("\n" + "=" * 70)
        print("13. REMINDER — Enable periodic 8am-10pm every 45min")
        print("=" * 70)
        await send(client, "505400080726" + "01" + "08" + "00" + "16" + "00" + "2d",
                   "reminder ON 8:00-22:00 45min", wait=2)

        input("   >>> Press Enter to disable reminder...")

        print("14. REMINDER — Disable")
        await send(client, "505400080726" + "00" + "08" + "00" + "14" + "00" + "3c",
                   "reminder OFF", wait=2)

        # =============================================
        print("\n" + "=" * 70)
        print("15. READ BOTTLE STATUS (second read)")
        print("=" * 70)
        pkts = await send(client, "47540001ff", "requestBottleData", wait=3)

        # =============================================
        print("\n" + "=" * 70)
        print("16. FINAL FLASH")
        print("=" * 70)
        await send(client, "50540003021d01", "flash", wait=2)

        await client.stop_notify(NOTIFY_CHAR)

    print("\n\nDone! All commands tested.")


asyncio.run(main())
