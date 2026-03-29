import asyncio
from bleak import BleakClient, BleakScanner
from datetime import datetime

ADDR = "A4:C1:38:32:D7:DE"
NOTIFY_CHAR = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000ffe9-0000-1000-8000-00805f9b34fb"

def on_notify(sender, data: bytearray):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] NOTIFY ({len(data)}B): {data.hex(' ')} | {data}")

async def main():
    print(f"Scanning for {ADDR}...")
    device = await BleakScanner.find_device_by_address(ADDR, timeout=10)
    if not device:
        print("Not found!")
        return

    async with BleakClient(device) as client:
        print(f"Connected to {device.name}")

        # Subscribe to notifications
        await client.start_notify(NOTIFY_CHAR, on_notify)
        print(f"Subscribed to {NOTIFY_CHAR}")
        print("Listening for notifications... (interact with the bottle)")
        print()

        # Try some common command bytes to provoke a response
        probe_cmds = [
            bytes([0x01]),
            bytes([0x02]),
            bytes([0x03]),
            bytes([0x04]),
            bytes([0x05]),
            bytes([0xAA, 0x01]),
            bytes([0xAA, 0x02]),
        ]

        for cmd in probe_cmds:
            try:
                print(f"  >> Sending command: {cmd.hex(' ')}")
                await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
                await asyncio.sleep(1)
            except Exception as e:
                print(f"  >> Write error: {e}")

        # Keep listening for 30 more seconds
        print("\nWaiting 30s for more notifications (try drinking/moving the bottle)...")
        await asyncio.sleep(30)
        await client.stop_notify(NOTIFY_CHAR)

    print("Done.")

asyncio.run(main())
