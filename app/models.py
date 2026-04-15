from enum import Enum

from pydantic import BaseModel, Field


# --- API request/response models ---


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


# --- MELCloud Home data models ---


class Setting(BaseModel):
    name: str
    value: str


class Device(BaseModel):
    id: str
    device_type: str | None = None
    given_display_name: str = Field(alias="givenDisplayName")
    settings: list[Setting] = Field(default_factory=list)
    is_connected: bool = Field(alias="isConnected")
    is_in_error: bool = Field(alias="isInError")

    model_config = {"extra": "allow", "populate_by_name": True}


class Building(BaseModel):
    id: str
    name: str
    air_to_air_units: list[Device] = Field(default_factory=list, alias="airToAirUnits")
    air_to_water_units: list[Device] = Field(default_factory=list, alias="airToWaterUnits")

    model_config = {"populate_by_name": True}


class UserProfile(BaseModel):
    id: str
    buildings: list[Building]

    model_config = {"extra": "allow"}
