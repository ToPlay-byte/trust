import asyncio
import hashlib
import os
import requests
from requests.exceptions import JSONDecodeError, RequestException

from services.task_logger import TaskLogger

MAX_START_RETRIES = 3
BASE_RETRY_DELAY = 2


class MultiloginClient:
    def __init__(self, logger: TaskLogger):
        self.logger = logger

    async def get_token(self, task) -> str | None:
        url = "https://api.multilogin.com/user/signin"
        payload = {
            "email": task.profile.user.email,
            "password": hashlib.md5(task.profile.user.password.encode()).hexdigest()
        }
        for attempt in range(1, MAX_START_RETRIES + 1):
            try:
                r = await asyncio.to_thread(requests.post, url, json=payload, timeout=30)
            except RequestException as e:
                if attempt < MAX_START_RETRIES:
                    await asyncio.sleep(BASE_RETRY_DELAY * attempt)
                    continue
                self.logger.console(
                    f"Failed to get token for user {task.profile.user.email}: {type(e).__name__}: {e}"
                )
                return None
            if r.status_code == 200:
                return r.json().get("data", {}).get("token")
            if r.status_code >= 500 and attempt < MAX_START_RETRIES:
                self.logger.console(
                    f"get_token:: Status {r.status_code} on attempt {attempt}/{MAX_START_RETRIES}, retrying..."
                )
                await asyncio.sleep(BASE_RETRY_DELAY * attempt)
                continue
            self.logger.console(
                f"Failed to get token for user {task.profile.user.email}. Status: {r.status_code}"
            )
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
                    await self.logger.warning(last_error_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                    continue
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
                        await self.logger.warning(last_error_msg)
                        await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                        continue
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
                    await self.logger.warning(last_error_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * attempt, 10))
                    continue
                await self.logger.error(last_error_msg)
                return None

            error_msg = (
                f"API error while starting browser, attempt {attempt}/{MAX_START_RETRIES}: "
                f"{self._response_debug_message(res)}"
            )
            self.logger.console(error_msg)

            if self._is_proxy_error(raw_text, data):
                proxy_err_msg = (
                    f"Proxy error on attempt {attempt}/{MAX_START_RETRIES}: {error_msg}. "
                    f"Profile {profile_id}."
                )
                if attempt < MAX_START_RETRIES:
                    await self.logger.warning(proxy_err_msg)
                    await asyncio.sleep(min(BASE_RETRY_DELAY * (2 ** attempt), 20))
                    continue
                await self.logger.error(proxy_err_msg)
                return None

            if self._is_lock_error(raw_text, data):
                await self.logger.warning(error_msg)
                if attempt < MAX_START_RETRIES:
                    await asyncio.sleep(min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), 10))
                    continue
                await self.logger.error("Profile remained locked after retries.")
                return None

            await self.logger.error(error_msg)
            return None

        await self.logger.error(last_error_msg or "Failed to start browser after retries.")
        return None

    async def stop_profile(self, task) -> bool:
        if os.getenv("STOP_BROWSER", "True").lower() != "true":
            return True

        token = await self.get_token(task)
        if not token:
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
                await self.logger.info(message)
                return True
            elif response.status_code not in (200, 204):
                message = (
                    f"Failed to stop Multilogin profile: "
                    f"status={response.status_code}, body={response.text}"
                )
                await self.logger.error(message)
                return False
            else:
                message = f"Stopped Multilogin profile: {task.profile.ml_profile_id}"
                await self.logger.info(message)
                return True

        except RequestException as e:
            message = (
                f"Network error while stopping Multilogin profile "
                f"{task.profile.ml_profile_id}: {e}"
            )
            await self.logger.error(message, exc=e)
            return False
        except Exception as e:
            message = (
                f"An unexpected error occurred while stopping Multilogin profile "
                f"{task.profile.ml_profile_id}: {e}"
            )
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
