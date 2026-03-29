import asyncio
from bleak import BleakClient, BleakScanner
from datetime import datetime

ADDR = "A4:C1:38:32:D7:DE"
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"
OTHER_WRITE = "00010203-0405-0607-0809-0a0b0c0d2b12"

all_notifications = []

def on_notify(sender, data: bytearray):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    ascii_repr = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
    print(f"  [{ts}] << ({len(data):>2}B) {data.hex(' ')}  |{ascii_repr}|")
    all_notifications.append((ts, data))

async def send(client, char, cmd, label=""):
    hex_str = cmd.hex(' ')
    print(f"  >> {label} [{hex_str}]")
    try:
        await client.write_gatt_char(char, cmd, response=False)
    except Exception as e:
        print(f"     Write error: {e}")
    await asyncio.sleep(1.5)

async def main():
    print(f"Scanning for {ADDR}...")
    device = await BleakScanner.find_device_by_address(ADDR, timeout=10)
    if not device:
        print("Not found!")
        return

    async with BleakClient(device) as client:
        print(f"Connected to {device.name}\n")
        await client.start_notify(NOTIFY_CHAR, on_notify)

        # === Phase 1: Single byte commands 0x00-0x20 ===
        print("=" * 60)
        print("PHASE 1: Single byte commands (0x00 - 0x20)")
        print("=" * 60)
        for i in range(0x21):
            await send(client, WRITE_CHAR, bytes([i]), f"cmd 0x{i:02x}")

        # === Phase 2: Two-letter ASCII prefixes seen in responses ===
        print("\n" + "=" * 60)
        print("PHASE 2: ASCII prefix commands (RT, RP, PT, ST, GT, etc.)")
        print("=" * 60)
        prefixes = [
            b"RT", b"RP", b"PT", b"ST", b"GT", b"GS", b"GA",
            b"RD", b"RS", b"RL", b"RC", b"RW", b"RA",
            b"DT", b"DS", b"DL", b"DC", b"DR",
            b"TM", b"TP", b"TL", b"TS",
            b"WL", b"WR", b"WS", b"WT",
            b"LV", b"LD", b"LC",
        ]
        for p in prefixes:
            await send(client, WRITE_CHAR, p, f"ascii '{p.decode()}'")

        # === Phase 3: ASCII prefixes with trailing 0x00 or 0x01 ===
        print("\n" + "=" * 60)
        print("PHASE 3: ASCII prefix + 0x00/0x01 payload")
        print("=" * 60)
        for p in [b"RT", b"RP", b"PT", b"ST", b"GT", b"RD", b"TP", b"WL"]:
            await send(client, WRITE_CHAR, p + b"\x00", f"'{p.decode()}' + 0x00")
            await send(client, WRITE_CHAR, p + b"\x01", f"'{p.decode()}' + 0x01")
            await send(client, WRITE_CHAR, p + b"\x00\x01", f"'{p.decode()}' + 0x00 0x01")

        # === Phase 4: ABCloudz frame format (AA sync) ===
        print("\n" + "=" * 60)
        print("PHASE 4: ABCloudz frame format (0xAA sync byte)")
        print("=" * 60)
        # AA LEN ID TYPE DATA CSUM
        for opcode in range(0x50, 0x60):
            frame = bytes([0xAA, 0x03, 0x01, opcode])
            csum = sum(frame) & 0xFF
            frame += bytes([csum])
            await send(client, WRITE_CHAR, frame, f"AA frame opcode=0x{opcode:02x}")

        for opcode in range(0x00, 0x10):
            frame = bytes([0xAA, 0x03, 0x01, opcode])
            csum = sum(frame) & 0xFF
            frame += bytes([csum])
            await send(client, WRITE_CHAR, frame, f"AA frame opcode=0x{opcode:02x}")

        # === Phase 5: Read the other characteristic ===
        print("\n" + "=" * 60)
        print("PHASE 5: Other characteristic read/write")
        print("=" * 60)
        try:
            val = await client.read_gatt_char(OTHER_WRITE)
            print(f"  Other char value: {val.hex(' ')}")
        except Exception as e:
            print(f"  Read error: {e}")

        for cmd in [b"\x00", b"\x01", b"\x02", b"\xFF"]:
            await send(client, OTHER_WRITE, cmd, f"other char 0x{cmd.hex()}")

        # === Phase 6: Longer probes - request with today's date ===
        print("\n" + "=" * 60)
        print("PHASE 6: Date-based requests")
        print("=" * 60)
        # Try requesting data for today: 2026-03-29
        date_bytes = bytes([0x1a, 0x03, 0x1d])  # year=26, month=3, day=29
        for prefix in [b"RT", b"RP", b"PT", b"GT", b"RD", b"DT"]:
            await send(client, WRITE_CHAR, prefix + b"\x00\x01" + date_bytes,
                       f"'{prefix.decode()}' + date 2026-03-29")

        # === Phase 7: Wait for passive notifications ===
        print("\n" + "=" * 60)
        print("PHASE 7: Passive listen (60s - take sips now!)")
        print("=" * 60)
        await asyncio.sleep(60)

        await client.stop_notify(NOTIFY_CHAR)

    # === Summary ===
    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(all_notifications)} notifications received")
    print("=" * 60)
    for ts, data in all_notifications:
        ascii_repr = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
        print(f"  [{ts}] ({len(data):>2}B) {data.hex(' ')}  |{ascii_repr}|")

asyncio.run(main())
