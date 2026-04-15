"""MELCloud Bridge — REST API for MELCloud Home ATA/ATW devices."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.melcloud import bridge
from app.models import ControlRequest, DeviceStatus, DeviceSummary, HealthResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Suppress noisy pyppeteer debug logs
logging.getLogger("pyppeteer").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting MELCloud Bridge...")
    await bridge.start()
    yield
    logger.info("Shutting down MELCloud Bridge...")
    await bridge.stop()


app = FastAPI(
    title="MELCloud Bridge",
    description="REST API bridge for MELCloud Home heat pump control",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok" if bridge.is_authenticated else "unauthenticated",
        authenticated=bridge.is_authenticated,
        device_count=bridge.device_count,
        cache_age_seconds=bridge.cache_age_seconds,
    )


@app.get("/devices", response_model=list[DeviceSummary])
async def list_devices():
    devices = await bridge.get_devices()
    return [
        DeviceSummary(
            id=d.id,
            name=d.given_display_name or "Unknown",
            device_type=d.device_type,
            is_connected=d.is_connected,
            is_in_error=d.is_in_error,
        )
        for d in devices
    ]


@app.get("/status", response_model=DeviceStatus)
async def get_status(
    device_id: str | None = Query(None, description="Device ID (defaults to first device)"),
    fresh: bool = Query(False, description="Force a live query instead of cache"),
):
    status = await bridge.get_status(device_id=device_id, fresh=fresh)
    if status is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return DeviceStatus(**status)


@app.post("/control")
async def control(
    body: ControlRequest,
    device_id: str | None = Query(None, description="Device ID (defaults to first device)"),
):
    if body.power is None and body.temperature is None and body.mode is None:
        raise HTTPException(status_code=400, detail="At least one control parameter required")

    try:
        result = await bridge.set_device_state(
            power=body.power,
            temperature=body.temperature,
            mode=body.mode.value if body.mode else None,
            device_id=device_id,
        )
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Control command failed")
        raise HTTPException(status_code=502, detail=f"MELCloud API error: {e}")
