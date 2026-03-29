import asyncio
from bleak import BleakClient, BleakScanner

TARGET = "A4:C1:38:32:D7:DE"

async def main():
    print(f"Scanning for device matching {TARGET}...")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)

    addr = None
    for a, (device, adv) in devices.items():
        if TARGET.lower() in a.lower():
            addr = a
            print(f"Found: {a}  {device.name or 'unknown'}  {adv.rssi} dBm")
            break

    if not addr:
        print("Device not found!")
        return

    print(f"\nConnecting to {addr}...")
    async with BleakClient(addr) as client:
        print(f"Connected: {client.is_connected}")
        print(f"\n{'='*60}")
        for svc in client.services:
            print(f"\nService: {svc.uuid}")
            print(f"  Description: {svc.description}")
            for char in svc.characteristics:
                print(f"  Characteristic: {char.uuid}")
                print(f"    Description: {char.description}")
                print(f"    Properties: {char.properties}")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        print(f"    Value: {val.hex()} ({val})")
                    except Exception as e:
                        print(f"    Read error: {e}")

asyncio.run(main())
