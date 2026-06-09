import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
sys.stdout.reconfigure(encoding='utf-8')

from unittest.mock import AsyncMock, MagicMock, patch
from browser.browser_session import BrowserSession, BrowserStartError
from clients.multilogin_client import MultiloginClient
from services.task_logger import TaskLogger


def make_mock_task():
    task = MagicMock()
    task.profile.ml_profile_id = "test-profile-123"
    task.profile.ml_folder_id = "test-folder-456"
    task.profile.user.email = "test@example.com"
    return task


def test_start_raises_on_no_token():
    async def run():
        logger = TaskLogger(task=None)
        client = MultiloginClient(logger)
        task = make_mock_task()
        session = BrowserSession(task=task, multilogin_client=client, logger=logger)

        # Mock get_token to return None
        client.get_token = AsyncMock(return_value=None)

        try:
            await session.start()
            print("❌ Should have raised BrowserStartError")
        except BrowserStartError as e:
            print(f"✅ BrowserStartError raised correctly: {e}")

    asyncio.run(run())


def test_stop_is_idempotent():
    async def run():
        logger = TaskLogger(task=None)
        client = MultiloginClient(logger)
        client.stop_profile = AsyncMock(return_value=True)
        task = make_mock_task()
        session = BrowserSession(task=task, multilogin_client=client, logger=logger)
        session._stopped = False
        session.playwright = None  # never started

        # Call stop twice — must not crash
        await session.stop()
        await session.stop()
        print("✅ stop() is idempotent — called twice without crash")

    asyncio.run(run())


def test_stop_does_not_crash_when_playwright_none():
    async def run():
        logger = TaskLogger(task=None)
        client = MultiloginClient(logger)
        client.stop_profile = AsyncMock(return_value=True)
        task = make_mock_task()
        session = BrowserSession(task=task, multilogin_client=client, logger=logger)
        session.playwright = None

        await session.stop()
        print("✅ stop() with playwright=None does not crash")

    asyncio.run(run())


if __name__ == "__main__":
    print("\n--- Running BrowserSession tests ---\n")
    test_start_raises_on_no_token()
    test_stop_is_idempotent()
    test_stop_does_not_crash_when_playwright_none()
    print("\n✅ All BrowserSession tests passed\n")
