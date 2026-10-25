"""MELCloud Home client — HTTP-based auth (no browser) with ATA write support."""

import asyncio
import json as json_mod
import logging
import re
import time
from pathlib import Path
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

# MELCloud Home web BFF expects word strings for operation mode
ATA_MODE_MAP = {
    "heat": "Heat",
    "dry": "Dry",
    "cool": "Cool",
    "fan": "Fan",
    "auto": "Automatic",  # NOT "Auto" — the API uses "Automatic"
}

ATA_FAN_SPEED_MAP = {
    "auto": 0,
    "silent": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
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

# Reported status value -> canonical control value, for drift detection.
# A reported value missing from these maps means "can't judge" — never reconcile on it.
ACTUAL_MODE_TO_CANONICAL = {v: k for k, v in ATA_MODE_MAP.items()}  # "Automatic" -> "auto"

ACTUAL_FAN_TO_CANONICAL = {
    "Auto": "auto",
    "Silent": "silent",
    "One": "1",
    "Two": "2",
    "Three": "3",
    "Four": "4",
    "Five": "4",  # API reports "Five" for turbo (set value 5 = canonical "4")
}

ACTUAL_VANE_V_TO_CANONICAL = {
    "Auto": "auto",
    "One": "1", "Two": "2", "Three": "3", "Four": "4", "Five": "5",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "Swing": "swing", "7": "swing",
    "0": "auto",
}

ACTUAL_VANE_H_TO_CANONICAL = {
    "Auto": "auto",
    "One": "1", "Two": "2", "Three": "3", "Four": "4", "Five": "5", "Six": "6",
    "Left": "1", "MiddleLeft": "2", "Centre": "3",
    "MiddleRight": "4", "Right": "5", "Split": "6",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "Swing": "swing", "12": "swing",
    "0": "auto",
}

# Consecutive drifted polls before the reconciler re-asserts a field.
# The default filters MELCloud API lag after a write.
DRIFT_STREAK_DEFAULT = 2
# "Commanded off but still spinning" is judged from live telemetry, which can keep
# reporting a fan during a legitimate post-shutdown dry cycle. Give it a longer
# window so a normal wind-down is never mistaken for a unit ignoring the off.
DRIFT_STREAK_POWER_TELEMETRY = 5

# ActualFanSpeed values meaning the indoor fan is not turning.
FAN_IDLE_VALUES = ("Off", "")

# Fields a desired state can hold (same names as set_device_state kwargs)
DESIRED_FIELDS = (
    "power", "temperature", "mode", "fan_speed", "vane_vertical", "vane_horizontal",
)


def _mask_email(email: str | None) -> str:
    """Mask an email for logging: j***@example.com."""
    if not email or "@" not in email:
        return "unknown"
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}"


class MelCloudBridge:
    """Handles auth, polling, and device control for MELCloud Home."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._authenticated = False
        self._devices: list[Device] = []
        self._last_poll: float = 0
        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # device_id -> {field: canonical value} — last state commanded via the API
        self._desired: dict[str, dict[str, Any]] = {}
        # device_id -> {field: consecutive polls the field has been drifted}
        self._mismatch_streak: dict[str, dict[str, int]] = {}
        self._load_desired()

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

        current_url = form_action
        for i in range(15):  # max redirects through IdentityServer and BFF
            if method == "POST":
                async with self._session.post(
                    url, data=data, allow_redirects=False
                ) as resp:
                    status = resp.status
                    headers = resp.headers
                    body = await resp.text()
                    current_url = str(resp.url)
            else:
                async with self._session.get(url, allow_redirects=False) as resp:
                    status = resp.status
                    headers = resp.headers
                    body = await resp.text()
                    current_url = str(resp.url)

            if status in (301, 302, 303, 307, 308):
                url = headers["Location"]
                if not url.startswith("http"):
                    parsed = urlparse(current_url)
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
            logger.info("Auth verified — user: %s", _mask_email(data.get("email")))

    # --- Polling ---

    async def _poll_loop(self) -> None:
        consecutive_failures = 0
        while True:
            try:
                await self._refresh_devices()
                if settings.reconcile:
                    await self._reconcile()
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

    # --- Desired state (reconciliation) ---

    def _load_desired(self) -> None:
        path = Path(settings.desired_state_file)
        if not path.exists():
            return
        try:
            data = json_mod.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._desired = {
                    dev_id: {f: v for f, v in fields.items() if f in DESIRED_FIELDS}
                    for dev_id, fields in data.items()
                    if isinstance(fields, dict)
                }
                logger.info("Loaded desired state for %d device(s) from %s", len(self._desired), path)
        except Exception:
            logger.exception("Failed to load desired state from %s", path)

    def _save_desired(self) -> None:
        path = Path(settings.desired_state_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json_mod.dumps(self._desired, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.exception("Failed to save desired state to %s", path)

    def get_desired(self, device_id: str) -> dict[str, Any]:
        return dict(self._desired.get(device_id, {}))

    def clear_desired(self, device_id: str, field: str | None = None) -> dict[str, Any]:
        """Release a device (or one field) from reconciliation. Returns remaining desired state."""
        if field is None:
            self._desired.pop(device_id, None)
            self._mismatch_streak.pop(device_id, None)
        else:
            self._desired.get(device_id, {}).pop(field, None)
            self._mismatch_streak.get(device_id, {}).pop(field, None)
            if not self._desired.get(device_id):
                self._desired.pop(device_id, None)
        self._save_desired()
        return self.get_desired(device_id)

    def _record_desired(self, device_id: str, fields: dict[str, Any]) -> None:
        self._desired.setdefault(device_id, {}).update(fields)
        # Commanded fields start a fresh drift observation window
        streak = self._mismatch_streak.get(device_id)
        if streak:
            for f in fields:
                streak.pop(f, None)
        self._save_desired()

    @staticmethod
    def _fan_is_running(actual: dict[str, str]) -> bool | None:
        """True/False from fan telemetry; None when the device doesn't report it."""
        reported = actual.get("ActualFanSpeed")
        if reported is None:
            return None
        return reported not in FAN_IDLE_VALUES

    def _required_streak(
        self, field: str, desired: dict[str, Any], actual: dict[str, str]
    ) -> int:
        """Consecutive drifted polls required before re-asserting this field."""
        if (
            field == "power"
            and desired.get("power") is False
            and actual.get("Power") == "False"
        ):
            # Power itself agrees with desired, so only telemetry flagged this.
            return DRIFT_STREAK_POWER_TELEMETRY
        return DRIFT_STREAK_DEFAULT

    def _drifted_fields(self, desired: dict[str, Any], actual: dict[str, str]) -> dict[str, Any]:
        """Compare desired (canonical) vs reported settings. Unknown reported values are skipped."""
        drift: dict[str, Any] = {}

        if "power" in desired and "Power" in actual:
            if (actual["Power"] == "True") != desired["power"]:
                drift["power"] = desired["power"]

        # MELCloud's Power field records the state the cloud accepted, not what the
        # unit is doing: a unit that ignored an "off" keeps reporting Power=False
        # while its fan still turns. The check above cannot see that, so judge it
        # from live telemetry instead.
        # The inverse (commanded on, fan idle) is deliberately NOT treated as drift:
        # a satisfied thermostat legitimately stops the fan while powered on.
        if desired.get("power") is False and "power" not in drift:
            if self._fan_is_running(actual):
                drift["power"] = False

        if "temperature" in desired and "SetTemperature" in actual:
            try:
                if abs(float(actual["SetTemperature"]) - float(desired["temperature"])) >= 0.25:
                    drift["temperature"] = desired["temperature"]
            except (ValueError, TypeError):
                pass

        checks = [
            ("mode", "OperationMode", ACTUAL_MODE_TO_CANONICAL),
            ("fan_speed", "SetFanSpeed", ACTUAL_FAN_TO_CANONICAL),
            ("vane_vertical", "VaneVerticalDirection", ACTUAL_VANE_V_TO_CANONICAL),
            ("vane_horizontal", "VaneHorizontalDirection", ACTUAL_VANE_H_TO_CANONICAL),
        ]
        for field, setting_name, to_canonical in checks:
            if field in desired and setting_name in actual:
                reported = to_canonical.get(actual[setting_name])
                if reported is not None and reported != desired[field]:
                    drift[field] = desired[field]

        return drift

    async def _reconcile(self) -> None:
        """Re-assert desired state on devices that drifted (2 consecutive polls to confirm)."""
        for device in self._devices:
            desired = self._desired.get(device.id)
            if not desired or not device.is_connected:
                continue

            actual = {s.name: s.value for s in device.settings}
            drifted = self._drifted_fields(desired, actual)
            prev_streak = self._mismatch_streak.get(device.id, {})
            streak = {f: prev_streak.get(f, 0) + 1 for f in drifted}
            self._mismatch_streak[device.id] = streak

            # Only correct fields that stayed drifted long enough (filters API lag)
            confirmed = {
                f: v for f, v in drifted.items()
                if streak[f] >= self._required_streak(f, desired, actual)
            }
            if not confirmed:
                continue

            logger.warning(
                "Reconcile: '%s' drifted on %s — re-asserting desired state",
                device.given_display_name, sorted(confirmed),
            )
            if confirmed.get("power") is False and actual.get("Power") == "False":
                logger.warning(
                    "Reconcile: '%s' was commanded off but fan reports '%s' — "
                    "unit ignored the off, re-sending",
                    device.given_display_name, actual.get("ActualFanSpeed"),
                )
            try:
                await self.set_device_state(device_id=device.id, **confirmed)
                self._mismatch_streak[device.id] = {}
            except Exception:
                logger.exception("Reconcile write failed for '%s'", device.given_display_name)

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

    async def get_device(self, device_ref: str | None = None) -> Device | None:
        """Get device by UUID, name (case-insensitive), or first device if None."""
        if not self._devices:
            return None
        if device_ref is None:
            return self._devices[0]
        # Try UUID match first
        for d in self._devices:
            if d.id == device_ref:
                return d
        # Try name match (case-insensitive)
        ref_lower = device_ref.lower()
        for d in self._devices:
            if d.given_display_name.lower() == ref_lower:
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

        # MELCloud Home API requires ALL control fields in every PUT.
        # Unchanged fields must be explicitly null. Vane values are sent as strings.
        state_data: dict[str, Any] = {
            "power": None,
            "operationMode": None,
            "setFanSpeed": None,
            "vaneHorizontalDirection": None,
            "vaneVerticalDirection": None,
            "setTemperature": None,
            "temperatureIncrementOverride": None,
            "inStandbyMode": None,
        }

        changed = False
        commanded: dict[str, Any] = {}
        if power is not None:
            state_data["power"] = power
            commanded["power"] = power
            changed = True
        if temperature is not None:
            state_data["setTemperature"] = temperature
            commanded["temperature"] = temperature
            changed = True
        if mode is not None:
            if mode not in ATA_MODE_MAP:
                raise ValueError(f"Unknown mode: {mode}. Valid: {list(ATA_MODE_MAP.keys())}")
            state_data["operationMode"] = ATA_MODE_MAP[mode]
            commanded["mode"] = mode
            changed = True
        if fan_speed is not None:
            if fan_speed not in ATA_FAN_SPEED_MAP:
                raise ValueError(f"Unknown fan speed: {fan_speed}. Valid: {list(ATA_FAN_SPEED_MAP.keys())}")
            state_data["setFanSpeed"] = ATA_FAN_SPEED_MAP[fan_speed]
            commanded["fan_speed"] = fan_speed
            changed = True
        if vane_vertical is not None:
            if vane_vertical not in ATA_VANE_VERTICAL_MAP:
                raise ValueError(f"Unknown vane vertical: {vane_vertical}. Valid: {list(ATA_VANE_VERTICAL_MAP.keys())}")
            # Web BFF expects words for auto/swing, numeric strings for positions
            if vane_vertical == "swing":
                state_data["vaneVerticalDirection"] = "Swing"
            elif vane_vertical == "auto":
                state_data["vaneVerticalDirection"] = "Auto"
            else:
                state_data["vaneVerticalDirection"] = str(ATA_VANE_VERTICAL_MAP[vane_vertical])
            commanded["vane_vertical"] = vane_vertical
            changed = True
        if vane_horizontal is not None:
            if vane_horizontal not in ATA_VANE_HORIZONTAL_MAP:
                raise ValueError(f"Unknown vane horizontal: {vane_horizontal}. Valid: {list(ATA_VANE_HORIZONTAL_MAP.keys())}")
            # Web BFF expects words for auto/swing, numeric strings for positions
            if vane_horizontal == "swing":
                state_data["vaneHorizontalDirection"] = "Swing"
            elif vane_horizontal == "auto":
                state_data["vaneHorizontalDirection"] = "Auto"
            else:
                state_data["vaneHorizontalDirection"] = str(ATA_VANE_HORIZONTAL_MAP[vane_horizontal])
            commanded["vane_horizontal"] = vane_horizontal
            changed = True

        if not changed:
            raise ValueError("No control parameters provided")

        device_type = device.device_type or "ataunit"
        result = await self._api_put(f"{device_type}/{device.id}", state_data)

        # Remember what was commanded so the reconciler can re-assert it on drift
        self._record_desired(device.id, commanded)

        # Force next poll to refresh, but don't block the response
        self._last_poll = 0
        return result


bridge = MelCloudBridge()
