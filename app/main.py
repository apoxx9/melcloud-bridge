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


@app.get("/status/{device}", response_model=DeviceStatus)
async def get_status_by_device(
    device: str,
    fresh: bool = Query(False, description="Force a live query instead of cache"),
):
    status = await bridge.get_status(device_id=device, fresh=fresh)
    if status is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return DeviceStatus(**status)


# Mode string -> numeric mapping for Loxone
LOXONE_MODE_MAP = {
    "Auto": 1,
    "Heat": 2,
    "Cool": 3,
    "Dry": 4,
    "Fan": 5,
}

# Reverse: numeric -> mode name (for /control/{device}/mode/{number})
LOXONE_MODE_REVERSE = {v: k.lower() for k, v in LOXONE_MODE_MAP.items()}

LOXONE_FAN_MAP = {
    "Auto": 0,
    "Off": 0,
    "Silent": 1,
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5,
}

# Vane string -> Loxone number (for status output)
LOXONE_VANE_V_MAP = {
    "Auto": 0,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
    "Swing": 6,
}

LOXONE_VANE_H_MAP = {
    "Auto": 0,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "Left": 1, "MiddleLeft": 2, "Centre": 3, "MiddleRight": 4, "Right": 5, "Split": 6,
    "Swing": 7,
}

# Numeric -> fan speed name (for /control/{device}/fan/{number})
LOXONE_FAN_REVERSE = {
    0: "auto",
    1: "silent",
    2: "1",
    3: "2",
    4: "3",
    5: "4",
}


@app.get("/status/{device}/loxone")
async def get_status_loxone(
    device: str,
    fresh: bool = Query(False, description="Force a live query instead of cache"),
):
    """Loxone-friendly status with flat numeric values."""
    status = await bridge.get_status(device_id=device, fresh=fresh)
    if status is None:
        raise HTTPException(status_code=404, detail="Device not found")

    s = status["settings"]
    return {
        "room_temp": float(s.get("RoomTemperature", 0)),
        "set_temp": float(s.get("SetTemperature", 0)),
        "power": 1 if s.get("Power") == "True" else 0,
        "mode": LOXONE_MODE_MAP.get(s.get("OperationMode", ""), 0),
        "fan_speed": LOXONE_FAN_MAP.get(s.get("ActualFanSpeed", ""), 0),
        "set_fan_speed": LOXONE_FAN_MAP.get(s.get("SetFanSpeed", ""), 0),
        "vane_vertical": LOXONE_VANE_V_MAP.get(s.get("VaneVerticalDirection", ""), 0),
        "vane_horizontal": LOXONE_VANE_H_MAP.get(s.get("VaneHorizontalDirection", ""), 0),
        "is_connected": 1 if status["is_connected"] else 0,
        "is_in_error": 1 if status["is_in_error"] else 0,
    }


@app.post("/control")
async def control(
    body: ControlRequest,
    device_id: str | None = Query(None, description="Device ID (defaults to first device)"),
):
    has_any = any(v is not None for v in [
        body.power, body.temperature, body.mode,
        body.fan_speed, body.vane_vertical, body.vane_horizontal,
    ])
    if not has_any:
        raise HTTPException(status_code=400, detail="At least one control parameter required")

    try:
        result = await bridge.set_device_state(
            power=body.power,
            temperature=body.temperature,
            mode=body.mode.value if body.mode else None,
            fan_speed=body.fan_speed.value if body.fan_speed else None,
            vane_vertical=body.vane_vertical.value if body.vane_vertical else None,
            vane_horizontal=body.vane_horizontal.value if body.vane_horizontal else None,
            device_id=device_id,
        )
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Control command failed")
        raise HTTPException(status_code=502, detail=f"MELCloud API error: {e}")


# --- GET convenience endpoints for Loxone / simple HTTP clients ---


async def _control_device(device_ref: str | None = None, **kwargs) -> dict:
    """Shared helper for GET control endpoints."""
    try:
        result = await bridge.set_device_state(device_id=device_ref, **kwargs)
        return {"status": "ok", "result": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Control command failed")
        raise HTTPException(status_code=502, detail=f"MELCloud API error: {e}")


@app.get("/control/{device}/on")
async def control_on(device: str):
    return await _control_device(device, power=True)


@app.get("/control/{device}/off")
async def control_off(device: str):
    return await _control_device(device, power=False)


@app.get("/control/{device}/mode/{mode}")
async def control_mode(device: str, mode: str):
    # Support numeric mode (1-5) for Loxone connectors
    try:
        mode_num = int(mode)
        if mode_num in LOXONE_MODE_REVERSE:
            mode = LOXONE_MODE_REVERSE[mode_num]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown mode number: {mode_num}. Valid: 1-5")
    except ValueError:
        mode = mode.lower()  # Normalize to lowercase
    return await _control_device(device, mode=mode)


@app.get("/control/{device}/temp/{value}")
async def control_temp(device: str, value: float):
    if value < 16 or value > 31:
        raise HTTPException(status_code=400, detail="Temperature must be between 16 and 31")
    return await _control_device(device, temperature=value)


@app.get("/control/{device}/fan/{speed}")
async def control_fan(device: str, speed: str):
    # Support numeric fan speed (0-5) for Loxone connectors
    try:
        speed_num = int(speed)
        if speed_num in LOXONE_FAN_REVERSE:
            speed = LOXONE_FAN_REVERSE[speed_num]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown fan speed number: {speed_num}. Valid: 0-5")
    except ValueError:
        speed = speed.lower()  # Normalize to lowercase
    return await _control_device(device, fan_speed=speed)


# Numeric -> vane name (for Loxone connectors)
# Vertical: 0=auto, 1-5=positions, 6=swing
LOXONE_VANE_V_REVERSE = {0: "auto", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "swing"}
# Horizontal: 0=auto, 1-6=positions, 7=swing
LOXONE_VANE_H_REVERSE = {0: "auto", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "swing"}


@app.get("/control/{device}/vane/vertical/{direction}")
async def control_vane_vertical(device: str, direction: str):
    try:
        num = int(direction)
        if num in LOXONE_VANE_V_REVERSE:
            direction = LOXONE_VANE_V_REVERSE[num]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown vane vertical number: {num}. Valid: 0-6")
    except ValueError:
        direction = direction.lower()
    return await _control_device(device, vane_vertical=direction)


@app.get("/control/{device}/vane/horizontal/{direction}")
async def control_vane_horizontal(device: str, direction: str):
    try:
        num = int(direction)
        if num in LOXONE_VANE_H_REVERSE:
            direction = LOXONE_VANE_H_REVERSE[num]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown vane horizontal number: {num}. Valid: 0-7")
    except ValueError:
        direction = direction.lower()
    return await _control_device(device, vane_horizontal=direction)
