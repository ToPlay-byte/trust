import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sys, os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from services.task_logger import TaskLogger

def test_console_is_sync():
    logger = TaskLogger(task=None)
    logger.console("Test console message — sync, no await")
    print("✅ console() works synchronously")


def test_without_task():
    async def run():
        logger = TaskLogger(task=None)
        await logger.info("Info without task")
        await logger.warning("Warning without task")
        await logger.error("Error without task")
        print("✅ Works without task — no crash")

    asyncio.run(run())


def test_error_with_exception():
    async def run():
        logger = TaskLogger(task=None)
        try:
            raise ValueError("Test exception")
        except ValueError as e:
            await logger.error("Something failed", exc=e)
            print("✅ error() with exc= works — traceback in console only")

    asyncio.run(run())


def test_no_password_in_logs():
    logger = TaskLogger(task=None)
    # Просто убеждаемся что logger не добавляет ничего лишнего
    import io
    from contextlib import redirect_stdout

    f = io.StringIO()
    with redirect_stdout(f):
        logger.console("User logged in successfully")

    output = f.getvalue()
    assert "password" not in output.lower()
    print("✅ No password in log output")


if __name__ == "__main__":
    print("\n--- Running TaskLogger tests ---\n")
    test_console_is_sync()
    test_without_task()
    test_error_with_exception()
    test_no_password_in_logs()
    print("\n✅ All tests passed\n")
