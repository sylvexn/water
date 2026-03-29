import asyncio
from bleak import BleakScanner

async def main():
    print("Scanning for BLE devices (10s)...")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)
    for addr, (device, adv) in sorted(devices.items(), key=lambda x: x[1][1].rssi, reverse=True):
        print(f"{adv.rssi:>4} dBm  {addr}  {device.name or 'unknown'}")

asyncio.run(main())
