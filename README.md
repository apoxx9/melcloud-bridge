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

| Field            | Values                                                     |
|------------------|------------------------------------------------------------|
| power            | `true`, `false`                                            |
| temperature      | float (device-dependent range, typically 16-31)            |
| mode             | `heat`, `cool`, `dry`, `fan`, `auto`                       |
| fan_speed        | `auto`, `1`, `2`, `3`, `4`                                 |
| vane_vertical    | `auto`, `1`, `2`, `3`, `4`, `5`, `swing`                   |
| vane_horizontal  | `auto`, `1`-`6`, `swing`                                   |

For multi-device setups, add `?device_id=YOUR_ID` to the URL.

The endpoint rejects commands if the device is disconnected.

## Setup

1. Copy `.env.example` to `.env` and fill in your MELCloud Home credentials
2. Run with Docker Compose:

```bash
docker compose up -d
```

3. Check health: `curl http://localhost:8099/health`
4. API docs: `http://localhost:8099/docs`

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
