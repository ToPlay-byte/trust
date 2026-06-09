import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any

import requests
from requests.exceptions import JSONDecodeError, RequestException

from services.task_logger import TaskLogger

MAX_START_RETRIES = 3
BASE_RETRY_DELAY = 2
MULTILOGIN_SIGNIN_URL = "https://api.multilogin.com/user/signin"
MULTILOGIN_PROFILE_CREATE_URL = "https://api.multilogin.com/profile/create"


class MultiloginApiError(Exception):
    pass


@dataclass(frozen=True)
class MultiloginCreateProfileResult:
    profile_ids: list[str]
    http_code: int
    response: dict[str, Any]


class MultiloginClient:
    def __init__(self, logger: TaskLogger | None = None):
        self.logger = logger

    @staticmethod
    def _password_hash(password: str) -> str:
        return hashlib.md5(password.encode()).hexdigest()

    @classmethod
    def get_token_by_credentials(cls, email: str, password: str, timeout: int = 30) -> str:
        payload = {
            "email": email,
            "password": cls._password_hash(password),
        }

        try:
            response = requests.post(MULTILOGIN_SIGNIN_URL, json=payload, timeout=timeout)
        except RequestException as exc:
            raise MultiloginApiError(
                f"Failed to get Multilogin token for {email}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            response_json = response.json()
        except ValueError as exc:
            raise MultiloginApiError(
                f"Multilogin signin returned non-JSON response. HTTP={response.status_code}"
            ) from exc

        if response.status_code != 200:
            message = (response_json.get("status") or {}).get("message") or "Signin failed"
            raise MultiloginApiError(f"Failed to get Multilogin token for {email}: {message}. HTTP={response.status_code}")

        token = (response_json.get("data") or {}).get("token")
        if not token:
            raise MultiloginApiError(f"Multilogin signin response does not contain token for {email}.")

        return token

    @staticmethod
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

    @staticmethod
    def create_profile_with_token(token: str, payload: dict[str, Any], timeout: int = 60) -> MultiloginCreateProfileResult:
        try:
            response = requests.post(
                MULTILOGIN_PROFILE_CREATE_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                json=payload,
                timeout=timeout,
            )
        except RequestException as exc:
            raise MultiloginApiError(f"Failed to create Multilogin profile: {type(exc).__name__}: {exc}") from exc

        try:
            response_json = response.json()
        except ValueError as exc:
            raise MultiloginApiError(
                f"Multilogin profile/create returned non-JSON response. HTTP={response.status_code}"
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

    async def get_token(self, task) -> str | None:
        for attempt in range(1, MAX_START_RETRIES + 1):
            try:
                return await asyncio.to_thread(
                    self.get_token_by_credentials,
                    task.profile.user.email,
                    task.profile.user.password,
                    30,
                )
            except MultiloginApiError as e:
                if attempt < MAX_START_RETRIES:
                    await asyncio.sleep(BASE_RETRY_DELAY * attempt)
                    continue
                if self.logger:
                    self.logger.console(str(e))
                return None
        return None

    async def start_profile(self, task, token: str | None = None) -> int | None:
        if token is None:
            token = await self.get_token(task)
        if not token:
            return None

        folder_id = task.profile.ml_folder_id
        profile_id = task.profile.ml_profile_id

        url = f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder_id}/p/{profile_id}/start"
        params = {"automation_type": "playwright"}
        headers = {"Authorization": f"Bearer {token}"}

        last_error_msg = None

        for attempt in range(1, MAX_START_RETRIES + 1):
            try:
                res = await self._request_start_profile(url, headers, params)
            except RequestException as e:
                last_error_msg = (
                    f"Request failed while starting browser for user "
                    f"{task.profile.user.email}, attempt {attempt}/{MAX_START_RETRIES}: "
                    f"{type(e).__name__}: {e}"
                )
                if attempt < MAX_START_RETRIES:
                    if self.logger:
                        await self.logger.warning(last_error_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                    continue
                if self.logger:
                    await self.logger.error(last_error_msg, exc=e)
                return None

            raw_text = res.text
            data = None
            try:
                data = res.json()
            except JSONDecodeError:
                data = None

            if res.status_code == 200:
                if data is None:
                    last_error_msg = (
                        f"JSON decoding failed while starting browser on successful response, "
                        f"attempt {attempt}/{MAX_START_RETRIES}. {self._response_debug_message(res)}"
                    )
                    if attempt < MAX_START_RETRIES:
                        if self.logger:
                            await self.logger.warning(last_error_msg)
                        await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                        continue
                    if self.logger:
                        await self.logger.error(last_error_msg)
                    return None

                port = self._extract_port(data)
                if port:
                    return port

                last_error_msg = (
                    f"Browser started response did not contain a port, "
                    f"attempt {attempt}/{MAX_START_RETRIES}. Parsed response: {data}"
                )
                if attempt < MAX_START_RETRIES:
                    if self.logger:
                        await self.logger.warning(last_error_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                    continue
                if self.logger:
                    await self.logger.error(last_error_msg)
                return None

            error_msg = (
                f"API error while starting browser, attempt {attempt}/{MAX_START_RETRIES}: "
                f"{self._response_debug_message(res)}"
            )
            if self.logger:
                self.logger.console(error_msg)

            if self._is_proxy_error(raw_text, data):
                proxy_err_msg = (
                    f"Proxy error on attempt {attempt}/{MAX_START_RETRIES}: {error_msg}. "
                    f"Profile {profile_id}."
                )
                if attempt < MAX_START_RETRIES:
                    if self.logger:
                        await self.logger.warning(proxy_err_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * (2 ** attempt), 20))
                    continue
                if self.logger:
                    await self.logger.error(proxy_err_msg)
                return None

            if self._is_lock_error(raw_text, data):
                if self.logger:
                    await self.logger.warning(error_msg)
                if attempt < MAX_START_RETRIES:
                    await asyncio.sleep(min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), 10))
                    continue
                if self.logger:
                    await self.logger.error("Profile remained locked after retries.")
                return None

            if self.logger:
                await self.logger.error(error_msg)
            return None

        if self.logger:
            await self.logger.error(last_error_msg or "Failed to start browser after retries.")
        return None

    async def stop_profile(self, task) -> bool:
        if os.getenv("STOP_BROWSER", "True").lower() != "true":
            return True

        token = await self.get_token(task)
        if not token:
            if self.logger:
                await self.logger.error("Cannot stop Multilogin profile: token is missing")
            return False

        url = f"https://launcher.mlx.yt:45001/api/v1/profile/stop/p/{task.profile.ml_profile_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            response = await asyncio.to_thread(
                requests.get, url, headers=headers, verify=False, timeout=30
            )

            response_data = None
            try:
                response_data = response.json()
            except JSONDecodeError:
                pass

            if (
                response.status_code == 500
                and response_data
                and "profile already stopped" in str(
                    response_data.get("status", {}).get("message", "")
                ).lower()
            ):
                message = (
                    f"Multilogin profile {task.profile.ml_profile_id} "
                    f"was already stopped. Status: {response.status_code}."
                )
                if self.logger:
                    await self.logger.info(message)
                return True
            elif response.status_code not in (200, 204):
                message = (
                    f"Failed to stop Multilogin profile: "
                    f"status={response.status_code}, body={response.text}"
                )
                if self.logger:
                    await self.logger.error(message)
                return False
            else:
                message = f"Stopped Multilogin profile: {task.profile.ml_profile_id}"
                if self.logger:
                    await self.logger.info(message)
                return True

        except RequestException as e:
            message = (
                f"Network error while stopping Multilogin profile "
                f"{task.profile.ml_profile_id}: {e}"
            )
            if self.logger:
                await self.logger.error(message, exc=e)
            return False
        except Exception as e:
            message = (
                f"An unexpected error occurred while stopping Multilogin profile "
                f"{task.profile.ml_profile_id}: {e}"
            )
            if self.logger:
                await self.logger.error(message, exc=e)
            return False

    def _extract_port(self, data) -> int | None:
        return data.get("value") or data.get("data", {}).get("port")

    def _is_lock_error(self, response_text, data=None) -> bool:
        text = (response_text or "").lower()
        if "lock_profile_error" in text or "can't lock profile" in text:
            return True
        if isinstance(data, dict):
            status = data.get("status") or {}
            message = str(status.get("message", "")).lower()
            error_code = str(status.get("error_code", "")).lower()
            if error_code == "lock_profile_error" or "can't lock profile" in message:
                return True
        return False

    def _is_proxy_error(self, response_text, data=None) -> bool:
        text = (response_text or "").lower()
        proxy_keywords = ["proxy_error", "proxy_connection_error", "proxy_timeout", "proxy_auth_error"]
        if any(k in text for k in proxy_keywords):
            return True
        if isinstance(data, dict):
            status = data.get("status") or {}
            error_code = str(status.get("error_code", "")).lower()
            if "proxy" in error_code:
                return True
        return False

    async def _request_start_profile(self, url, headers, params):
        return await asyncio.to_thread(
            requests.get,
            url,
            headers=headers,
            params=params,
            verify=False,
            timeout=30,
        )

    def _response_debug_message(self, response, max_body_length: int = 1000) -> str:
        content_type = response.headers.get("content-type", "unknown")
        body = response.text.strip()
        if len(body) > max_body_length:
            body = f"{body[:max_body_length]}... [truncated]"
        return f"status={response.status_code}, content_type={content_type}, body={body!r}"
