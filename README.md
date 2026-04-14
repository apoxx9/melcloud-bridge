# melcloud-bridge

Docker-based REST API bridge for MELCloud Home (home.melcloud.com) heat pump control.

Built for the Mitsubishi MSZ-LN60VG with MAC-597IFB-E WiFi adapter, but works with any MELCloud Home device.

## Why

MELCloud Home uses browser-based OAuth (AWS Cognito) — there's no simple API key or HTTP login. This bridge handles the headless browser authentication via pymelcloudhome and exposes a clean REST API.

## Endpoints

| Method | Path       | Description                              |
|--------|------------|------------------------------------------|
| GET    | /health    | Auth status, device count, cache age     |
| GET    | /devices   | List all registered devices              |
| GET    | /status    | Device status (add `?fresh=true` to skip cache) |
| POST   | /control   | Control device (power, temperature, mode)|

### POST /control body

```json
{
  "power": true,
  "temperature": 22.0,
  "mode": "heat"
}
```

All fields are optional. Valid modes: `heat`, `cool`, `dry`, `fan`, `auto`.

## Setup

1. Copy `.env.example` to `.env` and fill in your MELCloud Home credentials
2. Run with Docker Compose:

```bash
docker compose up -d
```

3. Check health: `curl http://localhost:8099/health`
4. API docs: `http://localhost:8099/docs`

## Configuration

| Variable          | Default   | Description                    |
|-------------------|-----------|--------------------------------|
| MELCLOUD_EMAIL    | required  | MELCloud Home email            |
| MELCLOUD_PASSWORD | required  | MELCloud Home password          |
| POLL_INTERVAL     | 60        | Status poll interval (seconds) |
| API_HOST          | 0.0.0.0   | API bind address               |
| API_PORT          | 8099      | API port                       |

## Architecture

- **FastAPI** REST API with async endpoints
- **pymelcloudhome** for MELCloud Home OAuth + device communication
- **Headless Chromium** inside the container for the OAuth flow
- **Polling cache** refreshes device state every `POLL_INTERVAL` seconds
- ATA (air-to-air) write support extended beyond upstream library
