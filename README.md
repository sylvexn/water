# WaterH

Reverse-engineered BLE protocol for the WaterH-Boost-24oz smart water bottle. Live hydration tracking dashboard at [water.syl.rest](https://water.syl.rest).

## Architecture

```
WaterH Bottle ──BLE──▶ Collector (fridge) ──POST──▶ API Server (Coolify VPS)
                        Python + bleak              FastAPI + SQLite
                        polls every 30s             water.syl.rest
                        local SQLite buffer         dashboard + JSON API
```

**Collector** connects to the bottle over BLE, sends command `0x04`, parses the notification response (sip history, temperature, device status), deduplicates against local SQLite, and pushes new records to the remote API.

**Server** receives sip data, stores it, and serves both the dashboard frontend and a JSON API.

## BLE Protocol

The bottle uses a CC2541-based BLE UART bridge. Two relevant GATT characteristics:

| UUID | Direction | Purpose |
|------|-----------|---------|
| `0000ffe4` (service `0000ffe0`) | notify, read | Data from bottle |
| `0000ffe9` (service `0000ffe5`) | write-without-response | Commands to bottle |

Sending `0x04` triggers a data dump as three notification packets:

| Prefix | Type | Content |
|--------|------|---------|
| `RT` | Real-Time | Device status byte |
| `RP` | Report | Config/firmware info |
| `PT` | Past Telemetry | Timestamped sip records |

### Sip Record Format (13 bytes)

```
Offset  Size  Field
0       1     Year (offset from 2000)
1       1     Month
2       1     Day
3       1     Hour
4       1     Minute
5       1     Second
6       2     Intake (ml), big-endian
8       2     Unknown
10      2     Temperature (C * 10), big-endian
12      1     Padding
```

## Project Structure

```
collector/          BLE polling service (runs on local host)
  collector.py      Connect, poll, parse, push to API
  waterh.service    systemd unit
  install.sh        Setup script

server/             API + dashboard (deployed via Coolify)
  server.py         FastAPI endpoints
  Dockerfile

frontend/           Dashboard SPA
  src/main.js       Vanilla JS + Chart.js
  index.html
  Dockerfile        nginx serving static build

research/           Original reverse engineering scripts
  scan.py           BLE device scanner
  enumerate.py      GATT service enumerator
  listen.py         Notification listener
  probe.py          Command fuzzer
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
