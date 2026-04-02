# WaterH

Reverse-engineered BLE protocol for the WaterH-Boost-24oz smart water bottle. Live hydration tracking dashboard at [water.syl.rest](https://water.syl.rest).

## Architecture

```
WaterH Bottle ──BLE──▶ Collector (fridge) ──POST──▶ API Server (Coolify VPS)
                        Python + bleak              FastAPI + SQLite
                        full app-protocol sync      water.syl.rest
                        local SQLite buffer         dashboard + JSON API
```

**Collector** connects to the bottle over BLE, replicates the official WaterH app's full sync protocol (bottle info, time sync, goal + reminder push, water log download, ack + clear, display update), deduplicates against local SQLite, and pushes new records to the remote API. Includes BlueZ zombie connection cleanup for reliable reconnection on Linux.

**Server** receives sip data, stores it, and serves both the dashboard frontend and a JSON API.

## BLE Protocol

The bottle uses a CC2541-based BLE UART bridge (no pairing/bonding required). Protocol was reverse-engineered by decompiling the official Android app (`com.waterh` by ABCloudz) with jadx.

| UUID | Direction | Purpose |
|------|-----------|---------|
| `0000ffe4` (service `0000ffe0`) | notify | Data from bottle |
| `0000ffe9` (service `0000ffe5`) | write-no-response | Commands to bottle |

See [protocol.md](protocol.md) for the complete command reference, response formats, sync flow, LED control, sensor data, and BlueZ notes.

### What we can control

- Read battery, temperature, TDS (water quality), water volume, charging status
- Sync time, daily goal, and reminder schedule
- Push today's intake total → updates the % shown on the bottle's screen
- Download sip history and clear it from bottle storage (prevents overflow)
- Set LED mode (default, breathe, calm, rainbow, warmth, christmas) with arbitrary RGB color
- Flash the LED on demand
- Recalibrate the water level sensor
- Send custom text to the bottle's display (text signature — not yet implemented)

## Project Structure

```
collector/          BLE polling service (runs on local host)
  collector.py      Full app-protocol sync, parse, push to API
  waterh.service    systemd unit

server/             API + dashboard (deployed via Coolify)
  server.py         FastAPI endpoints
  Dockerfile

frontend/           Dashboard SPA
  src/main.js       Vanilla JS + Chart.js
  index.html
  Dockerfile        nginx serving static build

research/           Reverse engineering scripts
  scan.py           BLE device scanner
  enumerate.py      GATT service enumerator
  listen.py         Notification listener
  probe.py          Command fuzzer
  dump.py           Protocol discovery
  commands.py       Interactive command tester (all LED modes, display, reminders)
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/ingest` | Receive sip batch from collector (authed) |
| `GET` | `/api/today` | Today's intake summary |
| `GET` | `/api/history` | Daily aggregates |
| `GET` | `/api/sips` | Raw sip log with pagination |
| `GET` | `/api/status` | Collector connection state |

## Running

### Collector

Requires a BLE-capable Linux host with the bottle in range.

```bash
sudo pacman -S bluez bluez-utils python-bleak
sudo systemctl start bluetooth
pip install -r collector/requirements.txt
python3 collector/collector.py
```

### Server

```bash
docker build -t waterh-server server/
docker run -p 8000:8000 -v waterh-data:/data waterh-server
```

## License

MIT
