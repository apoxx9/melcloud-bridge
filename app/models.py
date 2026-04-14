from enum import Enum
from pydantic import BaseModel


class OperationMode(str, Enum):
    HEAT = "heat"
    COOL = "cool"
    DRY = "dry"
    FAN = "fan"
    AUTO = "auto"


class ControlRequest(BaseModel):
    power: bool | None = None
    temperature: float | None = None
    mode: OperationMode | None = None


class DeviceSummary(BaseModel):
    id: str
    name: str
    device_type: str | None
    is_connected: bool
    is_in_error: bool


class DeviceStatus(BaseModel):
    id: str
    name: str
    is_connected: bool
    is_in_error: bool
    settings: dict[str, str]
    cached: bool = True


class HealthResponse(BaseModel):
    status: str
    authenticated: bool
    device_count: int
    cache_age_seconds: float | None
