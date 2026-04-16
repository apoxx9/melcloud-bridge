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
| 0 | Auto |
| 1 | Silent |
| 2 | Speed 1 (low) |
| 3 | Speed 2 (medium) |
| 4 | Speed 3 (high) |
| 5 | Speed 4 (turbo) |

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
http://192.168.1.x:8099/control/5e672a9f-aa0c-498e-9da0-7672dd9ec392/off
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

| Variable          | Default   | Description                          |
|-------------------|-----------|--------------------------------------|
| MELCLOUD_EMAIL    | required  | MELCloud Home email                  |
| MELCLOUD_PASSWORD | required  | MELCloud Home password               |
| POLL_INTERVAL     | 60        | Status poll interval in seconds (10-3600) |
| API_HOST          | 0.0.0.0   | API bind address                     |
| API_PORT          | 8099      | API port                             |

## Known Limits

- **No official API rate limits documented** - MELCloud Home API is reverse-engineered, not publicly documented.
- **Poll interval**: 60s is safe and matches the Home Assistant integration. Minimum enforced is 10s.
- **Control commands**: avoid sending more than one command per second. The bridge does not debounce, so the caller is responsible.
- **Session expiry**: cookies expire after ~1 hour. The bridge automatically re-authenticates on 401/403 responses.
- **Auth lockout**: too many failed login attempts will temporarily lock the Cognito account (~15-30 min).
- **Polling failures**: after 3 consecutive poll failures, the bridge automatically re-authenticates. Backoff increases exponentially up to 15 minutes.

## Architecture

- **FastAPI** async REST API
- **HTTP-only auth** - follows the full OIDC redirect chain without a browser
- **Polling cache** refreshes device state every `POLL_INTERVAL` seconds
- **ATA write support** for air-to-air unit control (power, temp, mode, fan, vanes)
- **Lightweight Docker image** - python:3.12-slim, no Chromium
