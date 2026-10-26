# melcloud-bridge

Docker-based REST API bridge for MELCloud Home (home.melcloud.com) heat pump control.

Built for the Mitsubishi MSZ-LN60VG with MAC-597IFB-E WiFi adapter, but works with any MELCloud Home ATA/ATW device.

## Why

MELCloud Home uses a three-layer OIDC auth chain (Cognito -> IdentityServer -> BFF). There's no simple API key or token. This bridge handles the full authentication flow via HTTP and exposes a clean REST API.

## Endpoints

| Method | Path       | Description                              |
|--------|------------|------------------------------------------|
| GET    | /health    | Auth status, device count, cache age     |
| GET    | /devices   | List all registered devices              |
| GET    | /status    | Device status (add `?fresh=true` to skip cache) |
| POST   | /control   | Control device (power, temperature, mode, fan, vanes) |
| GET    | /desired/{device} | Desired state held by the reconciler |
| DELETE | /desired/{device} | Release device (or `/{field}`) from reconciliation |

### GET /devices

Returns a list of all devices with their IDs. Use the `id` field as `device_id` in other endpoints.

```bash
curl http://localhost:8099/devices
```

### GET /status

```bash
# Default (first device, cached)
curl http://localhost:8099/status

# Specific device, fresh from API
curl "http://localhost:8099/status?device_id=YOUR_ID&fresh=true"
```

### POST /control

```json
{
  "power": true,
  "temperature": 22.0,
  "mode": "heat",
  "fan_speed": "auto",
  "vane_vertical": "auto",
  "vane_horizontal": "3"
}
```

All fields are optional. Send only what you want to change.

```bash
# Turn on
curl -X POST http://localhost:8099/control -H "Content-Type: application/json" -d '{"power": true}'

# Heat to 22 degrees
curl -X POST http://localhost:8099/control -H "Content-Type: application/json" -d '{"power": true, "mode": "heat", "temperature": 22}'

# Turn off
curl -X POST http://localhost:8099/control -H "Content-Type: application/json" -d '{"power": false}'
```

| Field            | Values                                                     |
|------------------|------------------------------------------------------------|
| power            | `true`, `false`                                            |
| temperature      | float (device-dependent range, typically 16-31)            |
| mode             | `heat`, `cool`, `dry`, `fan`, `auto`                       |
| fan_speed        | `auto`, `silent`, `1`, `2`, `3`, `4`                       |
| vane_vertical    | `auto`, `1`, `2`, `3`, `4`, `5`, `swing`                   |
| vane_horizontal  | `auto`, `1`-`6`, `swing`                                   |

For multi-device setups, add `?device_id=YOUR_ID` to the URL.

The endpoint rejects commands if the device is disconnected.

### GET control shortcuts

Single-URL GET endpoints for simple HTTP clients that can't send POST requests with JSON bodies. The `{device}` segment accepts either the device **name** (case-insensitive) or **UUID**.

| URL | Action |
|-----|--------|
| `/status/{device}` | Get device status (add `?fresh=true` to skip cache) |
| `/status/{device}/loxone` | Loxone-friendly status with flat numeric values |
| `/control/{device}/on` | Power on |
| `/control/{device}/off` | Power off |
| `/control/{device}/mode/{mode}` | Set mode by name or number (see table below) |
| `/control/{device}/temp/{value}` | Set temperature (16-31) |
| `/control/{device}/fan/{speed}` | Set fan speed by name or number (see table below) |
| `/control/{device}/vane/vertical/{dir}` | Set vertical vane by name or number (see table below) |
| `/control/{device}/vane/horizontal/{dir}` | Set horizontal vane by name or number (see table below) |

All controls accept both names and numbers:

| Number | Mode |
|--------|------|
| 1 | Auto |
| 2 | Heat |
| 3 | Cool |
| 4 | Dry |
| 5 | Fan |

| Number | Fan Speed |
|--------|-----------|
| 1 | Auto |
| 2 | Silent |
| 3 | Speed 1 (low) |
| 4 | Speed 2 (medium) |
| 5 | Speed 3 (high) |
| 6 | Speed 4 (turbo) |

Note: in the status direction, `fan_speed` (actual) reports **1 for both Auto and
a stopped fan** — the reported scale has no separate number for "not turning", so
an idle auto unit and a switched-off one look identical. Use `running` to tell
them apart. `set_fan_speed` is unambiguous; this affects the actual-speed value
only.

| Number | Vane Vertical |
|--------|---------------|
| 1 | Auto |
| 2-6 | Position 1-5 |
| 7 | Swing |

| Number | Vane Horizontal |
|--------|-----------------|
| 1 | Auto |
| 2-7 | Position 1-6 |
| 8 | Swing |

Examples:

```
http://192.168.1.x:8099/status/living/loxone
http://192.168.1.x:8099/control/living/on
http://192.168.1.x:8099/control/living/mode/heat
http://192.168.1.x:8099/control/living/mode/2
http://192.168.1.x:8099/control/living/temp/22
http://192.168.1.x:8099/control/YOUR_DEVICE_ID/off
```

### Loxone integration

**Virtual HTTP Input** — poll `http://BRIDGE_IP:8099/status/living/loxone` every 30s:

| Input Command | Parse pattern |
|---|---|
| Room Temperature | `"room_temp":\i` |
| Set Temperature | `"set_temp":\i` |
| Power | `"power":\i` |
| Mode | `"mode":\i` |
| Fan Speed (actual) | `"fan_speed":\i` |
| Fan Speed (set) | `"set_fan_speed":\i` |
| Running (actual on/off) | `"running":\i` |
| Connected | `"is_connected":\i` |
| Vane Vertical | `"vane_vertical":\i` |
| Vane Horizontal | `"vane_horizontal":\i` |
| Error | `"is_in_error":\i` |

**Virtual Output** — base address `http://BRIDGE_IP:8099`, then add connectors:

| Connector | Type | Command ON | Command OFF / Value |
|---|---|---|---|
| Power | Digital | `/control/living/on` | `/control/living/off` |
| Temperature | Analog | `/control/living/temp/<v.1>` | |
| Mode | Analog | `/control/living/mode/<v.0>` | |
| Fan Speed | Analog | `/control/living/fan/<v.0>` | |
| Vane Vertical | Analog | `/control/living/vane/vertical/<v.0>` | |
| Vane Horizontal | Analog | `/control/living/vane/horizontal/<v.0>` | |

`<v.1>` = value with 1 decimal place, `<v.0>` = integer.

**Use `running`, not `power`, for on/off status.** `power` mirrors MELCloud's
`Power` field, which records the state the cloud accepted - what was *asked for*.
`running` is derived from live fan telemetry - what the unit is *doing*. They
diverge when a unit ignores a command: a failed "off" leaves `power` at 0 while
the unit keeps heating, and anything reading `power` shows it as off. Wire both,
drive your status display from `running`, and treat a sustained
`power == 0 AND running == 1` as a unit that is not obeying commands.

`running` is 1 whenever the fan turns, which includes the normal post-shutdown
dry cycle, so expect it to stay 1 for a few minutes after a legitimate off.

## Deploy

### Option A: Pull pre-built image (recommended for homelab/Portainer)

The image is automatically built and pushed to GitHub Container Registry on every commit.

```bash
docker run -d \
  --name melcloud-bridge \
  --restart unless-stopped \
  -p 8099:8099 \
  -e MELCLOUD_EMAIL=your-email@example.com \
  -e MELCLOUD_PASSWORD=your-password \
  -e POLL_INTERVAL=60 \
  -e RECONCILE=true \
  -v melcloud-bridge-data:/app/data \
  ghcr.io/apoxx9/melcloud-bridge:latest
```

Or as a **Portainer Stack** (Stacks -> Add Stack -> paste this):

```yaml
services:
  melcloud-bridge:
    image: ghcr.io/apoxx9/melcloud-bridge:latest
    container_name: melcloud-bridge
    restart: unless-stopped
    ports:
      - "8099:8099"
    environment:
      - MELCLOUD_EMAIL=your-email@example.com
      - MELCLOUD_PASSWORD=your-password
      - POLL_INTERVAL=60
      - RECONCILE=true
    volumes:
      - melcloud-bridge-data:/app/data

volumes:
  melcloud-bridge-data:
```

The image supports both `linux/amd64` and `linux/arm64`.

### Option B: Build from source

```bash
git clone https://github.com/apoxx9/melcloud-bridge.git
cd melcloud-bridge
cp .env.example .env
# Edit .env with your credentials
docker compose up -d
```

### Verify

```bash
curl http://localhost:8099/health
```

API docs are available at `http://localhost:8099/docs`.

## Configuration

| Variable           | Default   | Description                          |
|--------------------|-----------|--------------------------------------|
| MELCLOUD_EMAIL     | required  | MELCloud Home email                  |
| MELCLOUD_PASSWORD  | required  | MELCloud Home password               |
| POLL_INTERVAL      | 60        | Status poll interval in seconds (10-3600) |
| API_HOST           | 0.0.0.0   | API bind address                     |
| API_PORT           | 8099      | API port                             |
| RECONCILE          | false     | Re-assert last commanded state if the device drifts |
| DESIRED_STATE_FILE | data/desired_state.json | Where desired state is persisted |

## Reconciliation

Devices sometimes drift from what was last commanded: a lost command, a power
blip, the unit reverting vanes on its own, or someone using the IR remote. With
`RECONCILE=true` the bridge actively corrects this:

- Every `/control` command (POST or GET shortcut) records the commanded fields
  as the device's **desired state**. Only fields you have actually commanded
  are managed — if you never set vanes through the bridge, vanes stay free.
- On each regular poll, actual state is compared against desired state. A field
  that mismatches on **two consecutive polls** is re-asserted with a single
  corrective command (one-poll blips are ignored to filter MELCloud reporting lag).
- **Commanded off but still running**: MELCloud's `Power` field records the state
  the cloud accepted, not what the unit is doing — a unit that ignores an "off"
  keeps reporting `Power=False` while its fan still turns, which the comparison
  above cannot see. The reconciler also checks live fan telemetry
  (`ActualFanSpeed`) for this case and re-sends the off after **five consecutive
  polls** — a longer window than normal drift, so a legitimate post-shutdown dry
  cycle is not mistaken for a failed command. The inverse (commanded on, fan idle)
  is deliberately not treated as drift: a satisfied thermostat legitimately stops
  the fan while powered on.
- Desired state is persisted to `DESIRED_STATE_FILE` and survives restarts —
  mount a volume at `/app/data` (the bundled compose file does this).
- `GET /desired/{device}` shows what the bridge is holding the device to.
  `DELETE /desired/{device}` releases the whole device;
  `DELETE /desired/{device}/{field}` releases one field
  (`power`, `temperature`, `mode`, `fan_speed`, `vane_vertical`, `vane_horizontal`).

Note: with reconciliation on, changes made via the IR remote or the MELCloud
app are treated as drift and reverted within ~2 poll cycles. The bridge (e.g.
Loxone) is the source of truth for every managed field.

## Known Limits

- **No official API rate limits documented** - MELCloud Home API is reverse-engineered, not publicly documented.
- **Poll interval**: 60s is safe and matches the Home Assistant integration. Minimum enforced is 10s.
- **Control commands**: avoid sending more than one command per second. The bridge does not debounce, so the caller is responsible.
- **Session expiry**: cookies expire after ~1 hour. The bridge automatically re-authenticates on 401/403 responses.
- **Auth lockout**: too many failed login attempts will temporarily lock the Cognito account (~15-30 min).
- **Polling failures**: after 3 consecutive poll failures, the bridge automatically re-authenticates. Backoff increases exponentially up to 15 minutes.

## Polling and Caching

There are two independent poll cycles:

```
MELCloud API  ←──(POLL_INTERVAL, default 60s)──  Bridge  ←──(30s recommended)──  Loxone
```

**Loxone polls the bridge locally**, not MELCloud. This means Loxone's poll speed has zero impact on MELCloud rate limits — you can poll as frequently as you want.

**After any control command** (power, mode, temp, fan, vane), the bridge immediately fetches fresh state from MELCloud. This means Loxone's next status poll will reflect the change, even if the bridge's regular poll cycle hasn't run yet.

**Changes made on the physical remote** or via the MELCloud app are only picked up on the bridge's next MELCloud poll — up to `POLL_INTERVAL` seconds of lag.

**Recommended setup:**
- Loxone Virtual HTTP Input poll interval: **30 seconds**
- Bridge `POLL_INTERVAL`: **30-60 seconds** (lower = faster detection of remote/physical changes, higher = fewer API calls)
- Lowering Loxone's interval below `POLL_INTERVAL` won't improve freshness — the bridge cache only updates on its own schedule or after commands

## Architecture

- **FastAPI** async REST API
- **HTTP-only auth** - follows the full OIDC redirect chain without a browser
- **Polling cache** refreshes device state every `POLL_INTERVAL` seconds
- **ATA write support** for air-to-air unit control (power, temp, mode, fan, vanes)
- **Lightweight Docker image** - python:3.12-slim, no Chromium
