import asyncio
import time
import threading
from datetime import timedelta
from asgiref.sync import sync_to_async
from django.utils import timezone
from user.models import UserTaskManager, ProfileDayProgress, ProfileSessionEvent
from services.task_logger import TaskLogger
from clients.multilogin_client import MultiloginClient
from browser.browser_session import BrowserSession, BrowserStartError
from pages.site_page import SitePage
from actions import SessionExpiredError
from playwright._impl._errors import TargetClosedError


class TaskRunner:
    def __init__(self, task, scenario, params):
        self.task = task
        self.scenario = scenario
        self.params = params
        self.logger = TaskLogger(task)
        self.multilogin_client = MultiloginClient(self.logger)
        self.browser_session = BrowserSession(
            task=task,
            multilogin_client=self.multilogin_client,
            logger=self.logger,
        )

    async def run(self, startup_complete: threading.Event | None = None, scenario_start_delay: int = 0):
        perf_start = time.perf_counter()
        started_at = timezone.now()

        progress = await self._start_day_progress(started_at)
        result = None
        error = None
        _startup_signalled = False

        def _signal_startup():
            nonlocal _startup_signalled
            if startup_complete and not _startup_signalled:
                startup_complete.set()
                _startup_signalled = True

        try:
            page = await self.browser_session.start()
            # Browser is live — release the scheduler to start the next profile.
            # threading.Event.set() is thread-safe and safe to call from a coroutine.
            _signal_startup()

            if scenario_start_delay > 0:
                # Stagger scenario execution so concurrent tasks don't all hit
                # the proxy pool at the same moment. The browser is idle during
                # this wait — the proxy tunnel is already established (probe passed).
                await self.logger.info(
                    f"Waiting {scenario_start_delay}s before starting scenario "
                    f"(proxy traffic stagger)."
                )
                await asyncio.sleep(scenario_start_delay)

            site_page = SitePage(page=page, logger=self.logger)

            try:
                result = await self.scenario.run(site_page, self.task)
            except TargetClosedError as _tce:
                await self.logger.warning(
                    f"TargetClosedError mid-scenario — restarting browser context. Error: {_tce}"
                )
                _reconnect_page = await self.browser_session.reconnect()
                try:
                    site_page = SitePage(page=_reconnect_page, logger=self.logger)
                    result = await self.scenario.run(site_page, self.task)
                except Exception as _retry_err:
                    await self.logger.error(
                        f"Browser restart failed to recover from TargetClosedError — failing task. "
                        f"Error: {_retry_err}"
                    )
                    raise
            except RuntimeError as _proxy_err:
                _err_str = str(_proxy_err)
                # Only SOCKS_PROXY_FAILURE and SSL_PROXY_FAILURE enter the recovery
                # path. All other RuntimeErrors (DOM errors, search failures, etc.)
                # are re-raised immediately to the outer handler — no recovery attempted.
                if "SOCKS_PROXY_FAILURE" in _err_str:
                    await self.logger.warning(
                        "SOCKS tunnel failure mid-scenario — triggering single reconnect."
                    )
                    _reconnect_page = await self.browser_session.reconnect()
                elif "SSL_PROXY_FAILURE" in _err_str:
                    await self.logger.warning(
                        "SSL proxy failure mid-scenario — triggering single SSL reconnect."
                    )
                    _reconnect_page = await self.browser_session.reconnect_ssl()
                else:
                    raise
                # Shared retry path for both SOCKS and SSL recovery.
                # startup_complete is already signalled — this reconnect is
                # invisible to the scheduler. Single attempt only: any failure
                # here propagates to the outer handler and the task is FAILED.
                try:
                    site_page = SitePage(page=_reconnect_page, logger=self.logger)
                    result = await self.scenario.run(site_page, self.task)
                except Exception as _retry_err:
                    _failure_type = "SOCKS" if "SOCKS_PROXY_FAILURE" in _err_str else "SSL"
                    await self.logger.error(
                        f"{_failure_type} issue persisted after reconnect — failing task. "
                        f"Error: {_retry_err}"
                    )
                    raise

            if result.success:
                self.task.task_status = UserTaskManager.Status.SUCCESS
                await self._record_session_event(ProfileSessionEvent.SessionStatus.ALIVE)
            else:
                self.task.task_status = UserTaskManager.Status.FAILED
                error = result.error

        except Exception as e:
            # If startup failed, unblock the scheduler so it can fire the next task.
            _signal_startup()
            self.task.task_status = UserTaskManager.Status.FAILED
            error = e
            if isinstance(e, SessionExpiredError):
                await self.logger.error(
                    "Session expired — profile will be paused. Relogin required before retry."
                )
                await self._record_session_event(ProfileSessionEvent.SessionStatus.RELOGIN_REQUIRED, error=e)
            elif isinstance(e, BrowserStartError):
                _err_s = str(e)
                if "ERR_SOCKS" in _err_s:
                    await self.logger.error(
                        "SOCKS startup failure — proxy node dead or unreachable."
                    )
                elif "Proxy probe" in _err_s:
                    await self.logger.error(
                        "Proxy startup failure — proxy unreachable at startup."
                    )
            await self.logger.error(f"Task failed: {type(e).__name__}: {e}", exc=e)

        finally:
            finished_at = timezone.now()
            await self.browser_session.stop()

            self.task.finished_at = finished_at
            await sync_to_async(self.task.save)(
                update_fields=["task_status", "finished_at", "updated_at"]
            )

            if self.task.task_status == UserTaskManager.Status.SUCCESS and result:
                await self._advance_profile_day(result, progress, started_at, finished_at)
            else:
                await self._mark_profile_day_failed(error, progress, finished_at)

            await self._log_runtime(perf_start)

    async def _start_day_progress(self, started_at):
        profile = self.task.profile
        return await sync_to_async(ProfileDayProgress.objects.create)(
            profile=profile,
            task=self.task,
            day_number=self._parse_day_number(),
            scenario_name=self.task.action,
            status=ProfileDayProgress.Status.IN_PROGRESS,
            started_at=started_at,
        )

    async def _advance_profile_day(self, result, progress, started_at, finished_at):
        profile = self.task.profile
        duration_seconds = int((finished_at - started_at).total_seconds())

        progress.status = ProfileDayProgress.Status.SUCCESS
        progress.finished_at = finished_at
        progress.duration_seconds = duration_seconds
        progress.interactions_count = result.interactions_count or 0
        progress.reviews_count = result.reviews_count or 0
        await sync_to_async(progress.save)()

        profile.last_completed_day = profile.current_day
        profile.total_successful_days = (profile.total_successful_days or 0) + 1
        profile.interactions_count = (profile.interactions_count or 0) + (result.interactions_count or 0)
        profile.reviews_count = (profile.reviews_count or 0) + (result.reviews_count or 0)
        profile.failure_reason = None
        profile.failed_at = None

        if (profile.current_day or 1) >= 21:
            profile.stage_status = profile.StageStatus.COMPLETED
            profile.completed_at = timezone.now()
        else:
            profile.current_day = (profile.current_day or 1) + 1
            profile.current_stage = f"day_{profile.current_day}"
            profile.stage_status = profile.StageStatus.PENDING
            profile.next_action_at = timezone.now() + timedelta(days=1)

        await sync_to_async(profile.save)()

    async def _mark_profile_day_failed(self, error, progress, finished_at):
        profile = self.task.profile
        error_msg = str(error) if error else "Task failed"
        is_session_expired = isinstance(error, SessionExpiredError)

        progress.status = ProfileDayProgress.Status.FAILED
        progress.finished_at = finished_at
        progress.error_message = error_msg
        if is_session_expired:
            progress.metadata = {"failure_category": "session_expired"}
        await sync_to_async(progress.save)()

        if is_session_expired:
            profile.stage_status = profile.StageStatus.PAUSED
            profile.failure_reason = (
                "Session expired — relogin required before this day can be retried. "
                f"Detail: {error_msg}"
            )
        else:
            profile.stage_status = profile.StageStatus.RETRY_REQUIRED
            profile.failure_reason = error_msg
        profile.failed_at = timezone.now()
        profile.total_failed_days = (profile.total_failed_days or 0) + 1
        await sync_to_async(profile.save)()

    async def _record_session_event(self, status: str, error: Exception | None = None):
        """Persist a session health event for per-account, per-day statistics."""
        try:
            profile = self.task.profile
            await sync_to_async(ProfileSessionEvent.objects.create)(
                profile=profile,
                warming_day=self._parse_day_number(),
                date=timezone.now().date(),
                session_status=status,
                session_dropped_at=timezone.now() if status != ProfileSessionEvent.SessionStatus.ALIVE else None,
                error_reason=str(error)[:500] if error else None,
                notes=self.task.action,
            )
        except Exception as rec_err:
            await self.logger.warning(f"Failed to record session event ({status}): {rec_err}")

    def _parse_day_number(self):
        """Extract integer day number from task.action, e.g. 'day_4' -> 4."""
        try:
            return int(self.task.action.split("_")[1])
        except (IndexError, ValueError):
            return self.task.profile.current_day or 0

    async def _log_runtime(self, perf_start):
        elapsed = time.perf_counter() - perf_start
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        await self.logger.info(f"Task runtime: {minutes}m {seconds}s ({int(elapsed)}s)")
