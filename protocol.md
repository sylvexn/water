# WaterH BLE Protocol

Reverse-engineered from APK decompilation of `com.waterh` (Android app by ABCloudz).

## Hardware

- **Chip:** CC2541 (Texas Instruments BLE SoC)
- **Connection:** Open GATT — no pairing, no bonding, no PIN
- **FCC ID:** 2A8K8WHB001
- **Models:** WaterH-Boost-24oz (`RP` byte 3 = `0x27`), WaterH-Vita (`RP` byte 3 = `0x31`)
- **Device name prefix:** `WaterH` (app scans for this)

## GATT Services

| Service UUID | Characteristic UUID | Properties | Purpose |
|---|---|---|---|
| `0000FFE0` | `0000FFE4` | Notify | Data from bottle |
| `0000FFE5` | `0000FFE9` | Write-no-response | Commands to bottle |
| `00010203-...-1912` | `00010203-...-2B12` | Write | OTA firmware updates |

## Linux / BlueZ Connection Notes

Android's `connectGatt()` + `gatt.close()` + `refreshDeviceCache()` fully manages the BLE connection lifecycle. On Linux with BlueZ, this doesn't happen automatically, causing **zombie connections** where BlueZ thinks it's still connected but the GATT layer is dead. The bottle won't re-advertise while BlueZ holds a stale connection.

**Required cleanup before reconnecting:**

```bash
bluetoothctl remove A4:C1:38:32:D7:DE   # clear stale connection + GATT cache
```

**Full adapter reset (if scan keeps failing):**

```bash
bluetoothctl remove A4:C1:38:32:D7:DE
bluetoothctl power off
sleep 1
bluetoothctl power on
```

The collector calls `bluetoothctl remove` on startup, after every disconnect, and on failed scans. After 3 consecutive scan failures it does a full power cycle.

## Command Reference

All commands are hex-encoded byte strings written to `FFE9`. Responses arrive as notifications on `FFE4`.

### Data Commands

| Command (hex) | Function | Notes |
|---|---|---|
| `47540001ff` | **Get bottle data** | Returns RP packet with model info, battery, firmware |
| `4754000106` | **Request water logs** | Triggers PT packets with sip history |
| `525000040306{len}` | **Ack water logs** | `{len}` = total bytes received (4 hex chars). Clears acked logs from bottle storage |
| `50540003021c05` | **Clear offline data** | Nuclear clear of all stored offline data |

### Settings Commands

| Command (hex) | Function | Notes |
|---|---|---|
| `505400140305{goal}0703{time}0726{reminder}` | **Sync settings** | Sets goal + time + reminder in one shot |
| `505400040305{goal}` | **Update goal** | `{goal}` = daily goal in ml as 4 hex chars (e.g. `09c4` = 2500ml) |
| `505400040304{amount}` | **Sync today's amount** | Push current day total to bottle display — updates the % on screen |
| `505400080726{reminder}` | **Update reminder** | 6-byte reminder schedule config |

### LED Commands

| Command (hex) | Function | Notes |
|---|---|---|
| `50540003021d01` | **Flash LED** | One-shot LED pulse |
| `5054000605fb{mode}{color}` | **Set reminder light** | Persistent mode + color for reminder LED. See tables below |

### Device Commands

| Command (hex) | Function | Notes |
|---|---|---|
| `50540003021c01` | **Register bottle** | Initial registration with app |
| `50540003021b01` | **Set language** | |
| `5054000302A101` | **Recalibrate (full)** | Tell bottle it's full for water level sensor |
| `5054000302A601` | **Recalibrate (empty)** | Tell bottle it's empty |
| `5054000302f400` | **Exit factory mode** | |

### Time Sync (in Sync Settings)

The time bytes in the sync settings command:

```
0703 {YY} {MM} {DD} {HH} {mm} {ss}
```

Where each value is a single hex byte (year offset from 2000).

### Reminder Config (6 bytes)

```
Byte 0: type — 00=off, 01=periodic on, 02=smart
Byte 1: wake hour (e.g. 08)
Byte 2: wake minute (e.g. 00)
Byte 3: sleep hour (e.g. 14 = 20 decimal)
Byte 4: sleep minute
Byte 5: interval in minutes (e.g. 3c = 60, 2d = 45, 1e = 30)
```

Default: `00 08 00 14 00 3c` = off, 8:00–20:00, every 60 min.

## Response Packet Types

All responses are notifications on `FFE4`. First two bytes identify the type:

| Prefix | ASCII | Type | Content |
|---|---|---|---|
| `52 50` | `RP` | Response | Bottle data, settings acks, registration state |
| `52 54` | `RT` | Report | Registration events, recalibrate results, sensor updates |
| `50 54` | `PT` | Past Telemetry | Water log records (sip history) |

### RP Response Subtypes

Identified by `value[2..3]` and `value[5]`:

| Bytes 2-3 | Byte 5 | Meaning |
|---|---|---|
| `00 27` | — | Boost model: new bottle data |
| `00 31` | — | Vita model: new bottle data |
| `00 0F` | — | Sync settings result (byte 10 = `0x00` means time sync ok) |
| — | `05` | Update goal ack (goal at bytes 6-7) |
| — | `06` | Water log response: byte 6 = `01` (data found) or `00` (no data) |
| — | `1C` | Registration: byte 6 = `02` (start), `06` (success + data cleared) |
| — | `20` | Signature response (text-to-display feature) |
| — | `26` | Update reminder ack (reminder bytes at 6-11) |

### RP Bottle Data (Boost model, `00 27`)

Confirmed from live capture and APK `buildFrom003Data()`:

```
Offset  Field
6       Battery % (0-100)
9-14    Bottle clock: YY MM DD HH mm ss
25-26   Firmware version (big-endian uint16)
31      Charging status (1 or 2 = charging, 0 = not)
```

### RT Report Subtypes

Identified by `value[5]`:

| Byte 5 | Byte 6 | Meaning |
|---|---|---|
| `01` | temp | Temperature update (°C as signed byte) |
| `02` | pwr | Battery power update |
| `08` | vol_hi, vol_lo | Water volume update (bytes 6-7) |
| `17` | state | Charging state update |
| `1C` | code | Registration event: `03` = user confirmed, `04` = failed |
| `28` | tds_hi, tds_lo | TDS (water quality) update (bytes 6-7) |
| `A1` | code | Recalibrate result |

### PT Water Log Format

First PT packet:

```
Bytes 0-1:  "PT" (0x50 0x54)
Bytes 2-3:  Total payload length (big-endian, in bytes)
Byte  4:    Records in this packet × 13
Byte  5:    0x06 (command echo)
Bytes 6+:   Sip records (13 bytes each)
```

Continuation packets:

```
Byte  0:    Records in this packet × 13
Byte  1:    0x06 (command echo)
Bytes 2+:   Sip records (13 bytes each)
```

### Sip Record (13 bytes)

```
Offset  Size  Field
0       1     Year (+ 2000)
1       1     Month (1-12)
2       1     Day (1-31)
3       1     Hour (0-23)
4       1     Minute (0-59)
5       1     Second (0-59)
6       2     Intake in ml (big-endian)
8       2     TDS / water quality (big-endian)
10      2     Temperature in °C × 10 (big-endian)
12      1     Padding / alignment
```

## Full Sync Flow

This is the sequence our collector follows, matching the official app:

```
1. bluetoothctl remove {addr}          (clear stale BlueZ state)
2. Scan for device
3. Connect (no pairing needed)
4. Discover services, enable notifications on FFE4
5. requestBottleData                 → write 47540001ff
   ← RP with battery, firmware, charging status
6. requestSyncData                   → write 505400140305{goal}0703{time}0726{reminder}
   ← RP with sync result (byte 10 = 0x00 means success)
7. syncTodayAmount                   → write 505400040304{amount}
   (pushes current day total to bottle screen %)
8. requestWaterLogs                  → write 4754000106
   ← RP with byte 5=0x06, byte 6=0x01 if data exists
   ← PT packets with sip records (may span multiple notifications)
9. ackWaterLogs                      → write 525000040306{totalBytes}
   (clears acked records from bottle storage)
10. syncTodayAmount again            → write 505400040304{newTotal}
    (updates screen with post-sync total)
11. Wait POLL_INTERVAL, repeat from step 5
```

On disconnect: `bluetoothctl remove {addr}` then loop back to step 1.

## Bottle Storage

The bottle stores sip records in flash memory. Observed capacity: 48 records per full dump (624 bytes). Exact max unknown but estimated at ~500 records.

- Without ack/clear: bottle replays full history each sync, eventually fills up and loses oldest data
- With ack/clear (step 9): bottle deletes acked records, freeing space
- `requestClearOfflineData` (`50540003021c05`) is a nuclear fallback that wipes everything

## Bottle Screen

The bottle has a small display showing:
- **Intake %** — calculated from synced goal and today's amount
- **Battery level**
- **Temperature**

The screen is updated by pushing today's total via `505400040304{amount}` and the goal via `505400040305{goal}`. The bottle calculates and displays the percentage.

The bottle also supports a **text signature** feature — the app renders text to a bitmap, rasterizes to 1-bit pixel data, chunks into 100-byte packets, and transfers to the bottle's display via RP characteristic with command byte `0x20`. This is used for custom text on the screen.

## LED Light Modes

Set with `5054000605fb{mode}{color}`:

| Mode byte | Name | Description |
|---|---|---|
| `00` | Default blue | Solid color |
| `01` | Breathe | Pulse in/out |
| `02` | Calm | Gentle transition |
| `03` | Rainbow pulse | Color cycling |
| `05` | Warmth | Warm tone effect |
| `06` | Christmas | Red/green alternating |

All modes tested and confirmed working.

## LED Colors

Any 6-char RGB hex value works. App presets:

| Name | RGB hex |
|---|---|
| Red | `ff0000` |
| Yellow | `ffff00` |
| Green | `00ff00` |
| Cyan | `00ffff` |
| Blue | `0000ff` |
| Purple | `ff00ff` |
| White | `ffffff` |

## Bottle Sensors

Data reported via RT update packets during connection:

| Sensor | RT byte 5 | Data | Notes |
|---|---|---|---|
| Temperature | `01` | byte 6 = °C | Water temperature |
| Battery | `02` | byte 6 = % | 0-100 |
| Volume | `08` | bytes 6-7 | Water level (big-endian) |
| Charging | `17` | byte 6 | 1 or 2 = charging |
| TDS | `28` | bytes 6-7 | Total dissolved solids (water quality, big-endian) |
