"""MELCloud Home client — HTTP-based auth (no browser) with ATA write support."""

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.config import settings
from app.models import Device, UserProfile

logger = logging.getLogger(__name__)

BASE_URL = "https://www.melcloudhome.com/api/"
LOGIN_URL = "https://www.melcloudhome.com/bff/login?returnUrl=/dashboard"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

API_HEADERS = {
    "x-csrf": "1",
    "User-Agent": USER_AGENT,
}

# Mitsubishi ATA operation mode codes
ATA_MODE_MAP = {
    "heat": 1,
    "dry": 2,
    "cool": 3,
    "fan": 7,
    "auto": 8,
}

ATA_FAN_SPEED_MAP = {
    "auto": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}

ATA_VANE_VERTICAL_MAP = {
    "auto": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "swing": 7,
}

ATA_VANE_HORIZONTAL_MAP = {
    "auto": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "swing": 12,
}


class MelCloudBridge:
    """Handles auth, polling, and device control for MELCloud Home."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self._devices: list[Device] = []
        self._last_poll: float = 0
        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        # Use a cookie jar that handles all domains and don't auto-follow redirects
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers={"User-Agent": USER_AGENT},
        )
        await self._login()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        self._authenticated = False

    # --- Authentication via HTTP redirect chain ---

    async def _login(self) -> None:
        """
        Authenticate by following the full OIDC redirect chain:
        Homepage → BFF → IdentityServer → Cognito → POST credentials → follow redirects back
        """
        logger.info("Authenticating with MELCloud Home...")

        try:
            # Step 0: Visit homepage to establish initial cookies/state
            async with self._session.get("https://www.melcloudhome.com/") as resp:
                logger.info("Homepage loaded: %d", resp.status)

            # Step 1: Hit /bff/login — follow redirects to Cognito login page
            cognito_url, cognito_html = await self._follow_redirects_to_cognito()

            # Step 2: Extract CSRF token and form action from the Cognito page
            form_action, csrf_token = self._parse_cognito_form(cognito_html, cognito_url)

            # Step 3: POST credentials to Cognito
            await self._submit_cognito_credentials(form_action, csrf_token)

            # Step 4: Verify we're authenticated by hitting the API
            await self._verify_auth()

            self._authenticated = True
            logger.info("Authentication successful.")
        except Exception:
            self._authenticated = False
            logger.exception("Authentication failed")
            raise

    async def _follow_redirects_to_cognito(self) -> tuple[str, str]:
        """Follow the redirect chain from /bff/login to the Cognito login page."""
        url = LOGIN_URL
        for i in range(10):  # max redirects
            async with self._session.get(url, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    url = resp.headers["Location"]
                    if not url.startswith("http"):
                        # Relative redirect
                        parsed = urlparse(str(resp.url))
                        url = f"{parsed.scheme}://{parsed.netloc}{url}"
                    logger.debug("Redirect %d: %s", i, url[:100])
                    continue
                elif resp.status == 200:
                    html = await resp.text()
                    if "cognitoSignInForm" in html:
                        logger.info("Reached Cognito login page")
                        return str(resp.url), html
                    else:
                        logger.warning("Got 200 but no login form at: %s", str(resp.url)[:100])
                        # Maybe it's an intermediate page, check for auto-redirect in HTML
                        raise RuntimeError(f"Unexpected page at {str(resp.url)[:100]}")
                else:
                    raise RuntimeError(f"Unexpected status {resp.status} at {url[:100]}")

        raise RuntimeError("Too many redirects")

    def _parse_cognito_form(self, html: str, page_url: str) -> tuple[str, str]:
        """Extract form action and CSRF token from the Cognito login page."""
        import re

        # Extract CSRF token
        csrf_match = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
        if not csrf_match:
            raise RuntimeError("Could not find CSRF token in Cognito form")
        csrf_token = csrf_match.group(1)

        # Extract form action (it's a relative or absolute URL)
        action_match = re.search(
            r'<form[^>]*name="cognitoSignInForm"[^>]*action="([^"]+)"', html
        )
        if action_match:
            form_action = action_match.group(1)
            # The action URL is HTML-encoded
            form_action = form_action.replace("&amp;", "&")
            if not form_action.startswith("http"):
                parsed = urlparse(page_url)
                form_action = f"{parsed.scheme}://{parsed.netloc}{form_action}"
        else:
            # Fallback: form posts to itself
            form_action = page_url

        logger.info("Form action: %s", form_action[:100])
        return form_action, csrf_token

    async def _submit_cognito_credentials(self, form_action: str, csrf_token: str) -> None:
        """POST credentials to Cognito and follow the redirect chain back to BFF."""
        form_data = {
            "_csrf": csrf_token,
            "username": settings.melcloud_email,
            "password": settings.melcloud_password,
            "cognitoAsfData": "",
            "signInSubmitButton": "Sign in",
        }

        # POST to Cognito — follow the full redirect chain back
        url = form_action
        method = "POST"
        data = form_data

        for i in range(15):  # max redirects through IdentityServer and BFF
            if method == "POST":
                async with self._session.post(
                    url, data=data, allow_redirects=False
                ) as resp:
                    status = resp.status
                    headers = resp.headers
                    body = await resp.text()
            else:
                async with self._session.get(url, allow_redirects=False) as resp:
                    status = resp.status
                    headers = resp.headers
                    body = await resp.text()

            if status in (301, 302, 303, 307, 308):
                url = headers["Location"]
                if not url.startswith("http"):
                    parsed = urlparse(str(resp.url) if hasattr(resp, 'url') else form_action)
                    url = f"{parsed.scheme}://{parsed.netloc}{url}"
                # 303 always becomes GET, 301/302 typically become GET after POST
                method = "GET" if status != 307 else method
                data = None
                logger.debug("Auth redirect %d (%d): %s", i, status, url[:100])
                continue
            elif status == 200:
                # Check if this is a form_post response (IdentityServer callback)
                if 'method="post"' in body.lower() or "method='post'" in body.lower():
                    # Auto-submit form — extract action and hidden fields
                    import re
                    action_match = re.search(r'action="([^"]+)"', body)
                    if action_match:
                        url = action_match.group(1).replace("&amp;", "&")
                        # Extract all hidden input fields
                        fields = dict(re.findall(
                            r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', body
                        ))
                        method = "POST"
                        data = fields
                        logger.debug("Auto-submit form to: %s with %d fields", url[:100], len(fields))
                        continue
                # We're done — should be on the dashboard
                logger.info("Login chain completed at: %s", url[:100])
                return
            else:
                # Check for error message
                if "Incorrect username or password" in body:
                    raise RuntimeError("Incorrect username or password")
                raise RuntimeError(f"Unexpected status {status} during auth at {url[:100]}")

        raise RuntimeError("Too many redirects during auth")

    async def _verify_auth(self) -> None:
        """Verify authentication by hitting the API."""
        async with self._session.get(
            f"{BASE_URL}user/context", headers=API_HEADERS
        ) as resp:
            if resp.status == 401 or resp.status == 403:
                raise RuntimeError(f"API returned {resp.status} after login — session cookies not valid")
            resp.raise_for_status()
            data = await resp.json()
            logger.info("Auth verified — user: %s", data.get("email", "unknown"))

    # --- Polling ---

    async def _poll_loop(self) -> None:
        consecutive_failures = 0
        while True:
            try:
                await self._refresh_devices()
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception("Poll cycle failed (%d consecutive)", consecutive_failures)
                # After 3 consecutive failures, try re-authenticating
                if consecutive_failures >= 3:
                    logger.warning("Multiple poll failures, attempting re-authentication...")
                    try:
                        await self._login()
                        consecutive_failures = 0
                    except Exception:
                        logger.exception("Re-authentication failed")
            # Back off on repeated failures: 60s, 120s, 240s, max 15min
            backoff = min(settings.poll_interval * (2 ** min(consecutive_failures, 4)), 900)
            await asyncio.sleep(backoff if consecutive_failures else settings.poll_interval)

    async def _refresh_devices(self) -> None:
        async with self._lock:
            data = await self._api_get("user/context")
            profile = UserProfile.model_validate(data)
            devices = []
            for building in profile.buildings:
                for d in building.air_to_air_units:
                    d.device_type = "ataunit"
                    devices.append(d)
                for d in building.air_to_water_units:
                    d.device_type = "atwunit"
                    devices.append(d)
            self._devices = devices
            self._last_poll = time.time()
            logger.info("Refreshed %d devices", len(devices))

    # --- API helpers ---

    async def _api_get(self, endpoint: str) -> dict[str, Any]:
        url = f"{BASE_URL}{endpoint}"
        async with self._session.get(url, headers=API_HEADERS) as resp:
            if resp.status in (401, 403):
                logger.warning("Session expired, re-authenticating...")
                await self._login()
                async with self._session.get(url, headers=API_HEADERS) as retry:
                    retry.raise_for_status()
                    return await retry.json()
            resp.raise_for_status()
            return await resp.json()

    async def _api_put(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        import json as json_mod
        url = f"{BASE_URL}{endpoint}"
        async with self._session.put(url, json=data, headers=API_HEADERS) as resp:
            if resp.status in (401, 403):
                logger.warning("Session expired, re-authenticating...")
                await self._login()
                async with self._session.put(url, json=data, headers=API_HEADERS) as retry:
                    retry.raise_for_status()
                    text = await retry.text()
                    return json_mod.loads(text) if text.strip() else {"status": "ok"}
            resp.raise_for_status()
            text = await resp.text()
            return json_mod.loads(text) if text.strip() else {"status": "ok"}

    # --- Public interface ---

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
        return list(self._devices)

    async def get_device(self, device_id: str | None = None) -> Device | None:
        if not self._devices:
            return None
        if device_id is None:
            return self._devices[0]
        for d in self._devices:
            if d.id == device_id:
                return d
        return None

    async def get_status(
        self, device_id: str | None = None, fresh: bool = False
    ) -> dict[str, Any] | None:
        if fresh:
            await self._refresh_devices()

        device = await self.get_device(device_id)
        if device is None:
            return None

        settings_dict = {s.name: s.value for s in device.settings}

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
        fan_speed: str | None = None,
        vane_vertical: str | None = None,
        vane_horizontal: str | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        device = await self.get_device(device_id)
        if device is None:
            raise ValueError("Device not found")

        if not device.is_connected:
            raise ValueError("Device is not connected")

        state_data: dict[str, Any] = {}

        if power is not None:
            state_data["power"] = power
        if temperature is not None:
            state_data["setTemperature"] = temperature
        if mode is not None:
            if mode not in ATA_MODE_MAP:
                raise ValueError(f"Unknown mode: {mode}. Valid: {list(ATA_MODE_MAP.keys())}")
            state_data["operationMode"] = ATA_MODE_MAP[mode]
        if fan_speed is not None:
            if fan_speed not in ATA_FAN_SPEED_MAP:
                raise ValueError(f"Unknown fan speed: {fan_speed}. Valid: {list(ATA_FAN_SPEED_MAP.keys())}")
            state_data["setFanSpeed"] = ATA_FAN_SPEED_MAP[fan_speed]
        if vane_vertical is not None:
            if vane_vertical not in ATA_VANE_VERTICAL_MAP:
                raise ValueError(f"Unknown vane vertical: {vane_vertical}. Valid: {list(ATA_VANE_VERTICAL_MAP.keys())}")
            state_data["vaneVerticalDirection"] = ATA_VANE_VERTICAL_MAP[vane_vertical]
        if vane_horizontal is not None:
            if vane_horizontal not in ATA_VANE_HORIZONTAL_MAP:
                raise ValueError(f"Unknown vane horizontal: {vane_horizontal}. Valid: {list(ATA_VANE_HORIZONTAL_MAP.keys())}")
            state_data["vaneHorizontalDirection"] = ATA_VANE_HORIZONTAL_MAP[vane_horizontal]

        if not state_data:
            raise ValueError("No control parameters provided")

        device_type = device.device_type or "ataunit"
        result = await self._api_put(f"{device_type}/{device.id}", state_data)

        # Force refresh on next poll
        self._last_poll = 0
        return result


bridge = MelCloudBridge()
