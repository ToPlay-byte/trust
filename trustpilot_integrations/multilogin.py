import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


MULTILOGIN_PROFILE_CREATE_URL = "https://api.multilogin.com/profile/create"


class MultiloginApiError(Exception):
    pass


@dataclass(frozen=True)
class MultiloginCreateProfileResult:
    profile_ids: list[str]
    http_code: int
    response: dict[str, Any]


class MultiloginClient:
    def __init__(self, token: str, timeout: int = 60) -> None:
        self.token = token
        self.timeout = timeout

    def create_profile(self, payload: dict[str, Any]) -> MultiloginCreateProfileResult:
        response = requests.post(
            MULTILOGIN_PROFILE_CREATE_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            json=payload,
            timeout=self.timeout,
        )

        try:
            response_json = response.json()
        except ValueError as exc:
            raise MultiloginApiError(
                f"Multilogin returned non-JSON response. HTTP={response.status_code}"
            ) from exc

        status = response_json.get("status") or {}
        message = status.get("message") or "Multilogin profile creation failed"

        if response.status_code != 201:
            raise MultiloginApiError(f"{message}. HTTP={response.status_code}")

        data = response_json.get("data") or {}
        profile_ids = [str(profile_id).strip() for profile_id in data.get("ids", []) if str(profile_id).strip()]

        if not profile_ids:
            raise MultiloginApiError("Multilogin response does not contain created profile IDs.")

        return MultiloginCreateProfileResult(
            profile_ids=profile_ids,
            http_code=response.status_code,
            response=response_json,
        )


def build_profile_payload(
    *,
    name: str,
    folder_id: str,
    notes: str = "",
    browser_type: str = "mimic",
    os_type: str = "windows",
    start_url: str = "https://www.trustpilot.com/",
    core_version: int | None = None,
    auto_update_core: bool | None = None,
    proxy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "browser_type": browser_type,
        "folder_id": folder_id,
        "os_type": os_type,
        "times": 1,
        "notes": notes,
        "parameters": {
            "storage": {
                "is_local": False,
                "save_service_worker": False,
            },
            "custom_start_urls": [start_url],
        },
    }

    if core_version is not None:
        payload["core_version"] = core_version

    if auto_update_core is not None:
        payload["auto_update_core"] = auto_update_core

    if proxy:
        payload["parameters"]["proxy"] = proxy

    return payload
