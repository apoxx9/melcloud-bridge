"""MELCloud Home client wrapper with ATA write support and polling cache."""

import asyncio
import logging
import time
from typing import Any

import aiohttp
from pymelcloudhome import MelCloudHomeClient
from pymelcloudhome.models import Device

from app.config import settings

logger = logging.getLogger(__name__)

# MELCloud Home ATA operation mode mapping
ATA_MODE_MAP = {
    "heat": 1,
    "dry": 2,
    "cool": 3,
    "fan": 7,
    "auto": 8,
}

ATA_MODE_REVERSE = {v: k for k, v in ATA_MODE_MAP.items()}


class MelCloudBridge:
    """Wraps pymelcloudhome with ATA write support and a polling cache."""

    def __init__(self) -> None:
        self._client: MelCloudHomeClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self._devices: list[Device] = []
        self._last_poll: float = 0
        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Authenticate and start the polling loop."""
        self._session = aiohttp.ClientSession()
        self._client = MelCloudHomeClient(
            session=self._session,
            chromium_executable_path=settings.chromium_path,
        )
        await self._login()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling and close the session."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._authenticated = False

    async def _login(self) -> None:
        """Perform browser-based OAuth login."""
        logger.info("Authenticating with MELCloud Home...")
        try:
            await self._client.login(
                settings.melcloud_email,
                settings.melcloud_password,
            )
            self._authenticated = True
            logger.info("Authentication successful.")
        except Exception:
            self._authenticated = False
            logger.exception("Authentication failed")
            raise

    async def _poll_loop(self) -> None:
        """Periodically refresh device data."""
        while True:
            try:
                await self._refresh_devices()
            except Exception:
                logger.exception("Poll cycle failed")
            await asyncio.sleep(settings.poll_interval)

    async def _refresh_devices(self) -> None:
        """Fetch fresh device list from MELCloud Home."""
        async with self._lock:
            self._devices = await self._client.list_devices()
            self._last_poll = time.time()
            logger.debug("Refreshed %d devices", len(self._devices))

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def device_count(self) -> int:
        return len(self._devices)

    @property
    def cache_age_seconds(self) -> float | None:
        if self._last_poll == 0:
            return None
        return time.time() - self._last_poll

    async def get_devices(self) -> list[Device]:
        """Return cached device list."""
        return list(self._devices)

    async def get_device(self, device_id: str | None = None) -> Device | None:
        """Get a specific device by ID, or the first device if no ID given."""
        if not self._devices:
            return None
        if device_id is None:
            return self._devices[0]
        for d in self._devices:
            if d.id == device_id:
                return d
        return None

    async def get_status(self, device_id: str | None = None, fresh: bool = False) -> dict[str, Any] | None:
        """Get device status. If fresh=True, force a refresh first."""
        if fresh:
            await self._refresh_devices()

        device = await self.get_device(device_id)
        if device is None:
            return None

        settings_dict = {}
        if device.settings:
            for s in device.settings:
                settings_dict[s.name] = s.value

        return {
            "id": device.id,
            "name": device.given_display_name,
            "is_connected": device.is_connected,
            "is_in_error": device.is_in_error,
            "settings": settings_dict,
            "cached": not fresh,
        }

    async def set_device_state(
        self,
        power: bool | None = None,
        temperature: float | None = None,
        mode: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Set ATA device state via the MELCloud Home API.

        pymelcloudhome only supports ATW writes out of the box.
        We extend it here by calling the same PUT pattern for ATA units.
        """
        device = await self.get_device(device_id)
        if device is None:
            raise ValueError("Device not found")

        state_data: dict[str, Any] = {}

        if power is not None:
            state_data["power"] = power

        if temperature is not None:
            state_data["setTemperature"] = temperature

        if mode is not None:
            if mode not in ATA_MODE_MAP:
                raise ValueError(f"Unknown mode: {mode}. Valid: {list(ATA_MODE_MAP.keys())}")
            state_data["operationMode"] = ATA_MODE_MAP[mode]

        if not state_data:
            raise ValueError("No control parameters provided")

        # Use the library's set_device_state for ATW units
        device_type = device.device_type or "ataunit"

        if device_type == "atwunit":
            result = await self._client.set_device_state(device.id, device_type, state_data)
        else:
            # ATA write: call the API directly since pymelcloudhome doesn't support it
            result = await self._ata_set_state(device.id, state_data)

        # Invalidate cache so next read gets fresh data
        self._last_poll = 0

        return result

    async def _ata_set_state(self, device_id: str, state_data: dict[str, Any]) -> dict[str, Any]:
        """Direct PUT to MELCloud Home API for ATA unit control."""
        url = f"https://www.melcloudhome.com/api/ataunit/{device_id}"
        headers = {
            "x-csrf": "1",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }

        async with self._session.put(url, json=state_data, headers=headers) as resp:
            if resp.status == 401:
                logger.warning("Got 401 on ATA write, re-authenticating...")
                await self._login()
                async with self._session.put(url, json=state_data, headers=headers) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.json()
            resp.raise_for_status()
            return await resp.json()


# Singleton instance
bridge = MelCloudBridge()
