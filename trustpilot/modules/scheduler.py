import os
import traceback
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from django import db
from django.utils import timezone

from load_django import *
from user.models import UserTaskManager
from services.task_logger import TaskLogger
from services.task_params import TaskParamsFactory
from services.task_registry import TaskRegistry
from services.task_runner import TaskRunner

MAX_CONCURRENT_TASKS = 10

# Maximum seconds to wait for one browser to finish starting before launching the next.
# Prevents the scheduler from hanging forever if a startup silently hangs.
STARTUP_WAIT_TIMEOUT = 120

# Cooldown after a CONFIRMED startup before the next launch begins.
# Gives the Multilogin launcher time to finish internal cleanup.
LAUNCH_COOLDOWN_SECONDS = 3

# Extra cooldown applied when a startup times out (startup_complete never fired
# within STARTUP_WAIT_TIMEOUT). A timeout means the system is under stress —
# adding more load immediately would amplify the problem.
TIMEOUT_BACKPRESSURE_SECONDS = 30

# Delay between a task's browser becoming live and its scenario starting to execute.
# Staggers scenario execution so that 5 profiles don't all begin generating
# heavy proxy traffic at exactly the same time.
SCENARIO_START_DELAY_SECONDS = 10

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)


def _execute_task_worker(task_id, startup_complete: threading.Event, scenario_start_delay: int = 0):
    """Worker function executed in a thread pool.

    Calls startup_complete.set() once the browser is live (or has failed),
    so the scheduler can serialise browser launches without blocking scenario execution.

    scenario_start_delay: seconds to sleep after the browser is live and before
    the scenario begins. Staggers proxy traffic across concurrent tasks so that
    all profiles don't hit the proxy pool simultaneously.
    """
    logger = TaskLogger()
    try:
        task = UserTaskManager.objects.select_related('profile__user').get(pk=task_id)
        logger = TaskLogger(task)

        try:
            scenario_class = TaskRegistry.get_scenario_class(task.action)
        except ValueError:
            task.task_status = UserTaskManager.Status.FAILED
            task.finished_at = timezone.now()
            task.save(update_fields=["task_status", "finished_at", "updated_at"])
            logger.console(f"Task {task.id}: unknown action '{task.action}' — marking FAILED")
            startup_complete.set()
            return

        params = TaskParamsFactory.from_task(task)
        scenario = scenario_class(params=params, logger=None)
        runner = TaskRunner(task=task, scenario=scenario, params=params)
        asyncio.run(runner.run(startup_complete=startup_complete, scenario_start_delay=scenario_start_delay))

    except Exception as e:
        logger.console(f"Critical error in worker for task {task_id}: {type(e).__name__}: {e}")
        traceback.print_exc()
        startup_complete.set()
    finally:
        db.connections.close_all()


def process_pending_tasks():
    logger = TaskLogger()

    current_running = UserTaskManager.objects.filter(
        task_status=UserTaskManager.Status.IN_PROGRESS
    ).count()

    available_slots = MAX_CONCURRENT_TASKS - current_running

    if available_slots <= 0:
        logger.console(f"Maximum concurrent tasks ({MAX_CONCURRENT_TASKS}) already running. Waiting...")
        return

    tasks = UserTaskManager.objects.select_related('profile__user').filter(
        task_status=UserTaskManager.Status.PENDING,
        execute_at__lte=timezone.now(),
    ).order_by('execute_at')[:available_slots]

    if not tasks.exists():
        logger.console("No pending tasks at this time.")
        return

    tasks_list = list(tasks)
    logger.console(
        f"Launching {len(tasks_list)} task(s). "
        f"Running: {current_running}/{MAX_CONCURRENT_TASKS}. "
        f"Scenario start delay: {SCENARIO_START_DELAY_SECONDS}s per task."
    )

    for i, task in enumerate(tasks_list):
        user_email = task.profile.user.email if task.profile and task.profile.user else "Unknown User"
        # Each task's scenario is delayed by its position in the launch sequence
        # so that proxy traffic ramps up gradually rather than spiking all at once.
        scenario_delay = i * SCENARIO_START_DELAY_SECONDS
        logger.console(
            f"Firing task {task.id}: {task.action} for {user_email} "
            f"(scenario_start_delay={scenario_delay}s)"
        )

        task.task_status = UserTaskManager.Status.IN_PROGRESS
        task.started_at = timezone.now()
        task.save(update_fields=["task_status", "started_at"])

        startup_complete = threading.Event()
        executor.submit(_execute_task_worker, task.id, startup_complete, scenario_delay)

        if i < len(tasks_list) - 1:
            # Block until this browser is live (or has failed / timed out) before
            # starting the next one. Serialises Multilogin launcher calls and
            # prevents proxy-connection contention.
            signalled = startup_complete.wait(timeout=STARTUP_WAIT_TIMEOUT)
            if not signalled:
                # Startup timed out — the system is already under stress.
                # Apply extra back-pressure before launching the next task to
                # avoid amplifying an overloaded proxy pool.
                logger.console(
                    f"Task {task.id}: startup did not complete within "
                    f"{STARTUP_WAIT_TIMEOUT}s — applying {TIMEOUT_BACKPRESSURE_SECONDS}s "
                    f"back-pressure before next launch."
                )
                time.sleep(TIMEOUT_BACKPRESSURE_SECONDS)
            else:
                time.sleep(LAUNCH_COOLDOWN_SECONDS)


if __name__ == "__main__":
    print("Starting Trustpilot Warming Strategy Scheduler...")
    print(f"Debug = {os.getenv('DEBUG')}")
    while True:
        try:
            process_pending_tasks()
            time.sleep(30)
        except Exception as e:
            TaskLogger().console(f"Error in scheduler loop: {type(e).__name__}: {e}")
            time.sleep(30)
