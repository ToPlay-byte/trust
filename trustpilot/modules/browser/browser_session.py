import asyncio
from playwright.async_api import async_playwright

from services.task_logger import TaskLogger
from clients.multilogin_client import MultiloginClient

# Ordered fallback lists for proxy probing. The browser navigates each endpoint in
# turn; first success ends the probe. Multiple endpoints guard against individual
# probe servers being unreachable — a single-endpoint timeout no longer aborts
# startup. ERR_SOCKS* / ERR_CERT* on any endpoint is always immediately fatal.
PROXY_PROBE_HTTP_ENDPOINTS = [
    "http://checkip.amazonaws.com",   # AWS-hosted, extremely stable
    "http://api.ipify.org",           # lightweight, widely available
    "http://ip-api.com/json",         # good uptime, JSON response
]
PROXY_PROBE_HTTPS_ENDPOINTS = [
    "https://checkip.amazonaws.com",
    "https://api.ipify.org",
]
PROXY_PROBE_TIMEOUT = 25_000  # ms — generous to tolerate slow proxy nodes

# Cooldown before a SOCKS reconnect attempt. Longer than the SSL cooldown because
# a collapsing SOCKS tunnel usually indicates proxy pool pressure — reconnecting
# immediately would amplify that pressure rather than relieve it.
SOCKS_RECOVERY_COOLDOWN_SECONDS = 12

# Cooldown before a mid-scenario SSL reconnect. Shorter than SOCKS because SSL
# cert failures are proxy-node-specific (not pool-pressure related), but still
# long enough to let Multilogin release the bad node cleanly before reassigning.
SSL_MID_SCENARIO_COOLDOWN_SECONDS = 10


class BrowserStartError(Exception):
    pass


class BrowserSession:
    def __init__(self, task, multilogin_client: MultiloginClient, logger: TaskLogger):
        self.task = task
        self.multilogin_client = multilogin_client
        self.logger = logger

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._stopped = False  # prevents double stop

    async def start(self):
        """Start the browser with one recovery attempt for SOCKS and SSL cert failures.

        ERR_SOCKS on the proxy probe: stop the profile, wait for the SOCKS cooldown,
        and retry so Multilogin can assign a fresh proxy node. Single attempt only.

        ERR_CERT* on the proxy probe: same pattern with the SSL cooldown.

        All other BrowserStartErrors (token failure, lock error, CDP timeout)
        propagate immediately — no recovery attempted.
        """
        try:
            return await self._attempt_start()
        except BrowserStartError as e:
            err_str = str(e)
            if "ERR_SOCKS" in err_str:
                return await self._startup_socks_recovery()
            if "ERR_CERT" in err_str:
                return await self._startup_ssl_recovery()
            raise

    async def _startup_socks_recovery(self):
        """Single recovery attempt for a SOCKS probe failure at startup.

        Tears down the session and retries _attempt_start() so Multilogin can
        assign a fresh proxy node. If the retry also fails, logs a clear terminal
        message and re-raises. Does NOT set _stopped so task_runner's finally
        block can still invoke stop() cleanly.
        """
        await self._socks_recovery_reconnect()
        await self.logger.info(
            f"Re-running startup after SOCKS recovery for profile "
            f"{self.task.profile.ml_profile_id}."
        )
        try:
            return await self._attempt_start()
        except BrowserStartError as retry_err:
            await self.logger.error(
                f"SOCKS startup failure persisted after recovery for profile "
                f"{self.task.profile.ml_profile_id} — proxy node dead or unreachable. "
                f"Error: {retry_err}"
            )
            raise

    async def _startup_ssl_recovery(self):
        """Single recovery attempt for an SSL cert probe failure at startup.

        Same pattern as _startup_socks_recovery but uses the SSL cooldown.
        Does NOT set _stopped so task_runner's finally block remains functional.
        """
        await self._ssl_recovery_reconnect()
        await self.logger.info(
            f"Re-running proxy probes after SSL reconnect for profile "
            f"{self.task.profile.ml_profile_id}."
        )
        try:
            return await self._attempt_start()
        except BrowserStartError as retry_err:
            await self.logger.error(
                f"SSL issue persisted after reconnect for profile "
                f"{self.task.profile.ml_profile_id} — failing task. "
                f"Error: {retry_err}"
            )
            raise

    async def _attempt_start(self):
        # 1. Get token
        token = await self.multilogin_client.get_token(self.task)
        if not token:
            raise BrowserStartError("Failed to get Multilogin token")

        # 2. Start profile — pass the token so start_profile doesn't fetch it again
        port = await self.multilogin_client.start_profile(self.task, token=token)
        if not port:
            raise BrowserStartError("Failed to start Multilogin profile")

        # 3. Connect Playwright — retry because Chromium needs a few seconds
        #    after Multilogin reports it as started before the CDP port is ready.
        #    ECONNREFUSED means the port isn't open yet (startup race); websocket
        #    retrieval failures are the same race via a different Playwright code path.
        #    Both are retried here with a short delay. Timeout is retried with the
        #    original 3s delay (hung handshake, not a race). All other exceptions
        #    propagate immediately — no retry storms, no new profile launches.
        CDP_MAX_RETRIES = 5
        CDP_RACE_DELAY = 2      # seconds — ECONNREFUSED / websocket race
        CDP_TIMEOUT_DELAY = 3   # seconds — hung handshake timeout

        self.playwright = await async_playwright().start()
        cdp_url = f"http://127.0.0.1:{port}"
        for cdp_attempt in range(CDP_MAX_RETRIES):
            try:
                self.browser = await asyncio.wait_for(
                    self.playwright.chromium.connect_over_cdp(cdp_url),
                    timeout=30.0,
                )
                if cdp_attempt > 0:
                    await self.logger.info(
                        f"CDP attach succeeded after {cdp_attempt + 1} attempts "
                        f"for profile {self.task.profile.ml_profile_id}."
                    )
                break
            except asyncio.TimeoutError:
                if cdp_attempt == CDP_MAX_RETRIES - 1:
                    raise BrowserStartError(
                        f"Timed out connecting to Chromium on port {port} "
                        f"after {CDP_MAX_RETRIES} attempts."
                    )
                await self.logger.warning(
                    f"CDP connect timed out (attempt {cdp_attempt + 1}/{CDP_MAX_RETRIES}), "
                    f"retrying in {CDP_TIMEOUT_DELAY}s..."
                )
                await asyncio.sleep(CDP_TIMEOUT_DELAY)
            except Exception as _cdp_err:
                _cdp_msg = str(_cdp_err)
                _is_race = "ECONNREFUSED" in _cdp_msg or "websocket" in _cdp_msg.lower()
                if not _is_race or cdp_attempt == CDP_MAX_RETRIES - 1:
                    raise BrowserStartError(
                        f"CDP startup failed for profile {self.task.profile.ml_profile_id} "
                        f"on port {port} after {cdp_attempt + 1} attempt(s): {_cdp_err}"
                    ) from _cdp_err
                await self.logger.warning(
                    f"CDP endpoint not ready yet (attempt {cdp_attempt + 1}/{CDP_MAX_RETRIES}) "
                    f"for profile {self.task.profile.ml_profile_id} — "
                    f"retrying connect_over_cdp in {CDP_RACE_DELAY}s. Error: {_cdp_msg}"
                )
                await asyncio.sleep(CDP_RACE_DELAY)
        self.context = self.browser.contexts[0]
        inherited_tab = bool(self.context.pages)
        self.page = (
            self.context.pages[0]
            if inherited_tab
            else await self.context.new_page()
        )

        await self.logger.info(
            f"Browser started for profile {self.task.profile.ml_profile_id} "
            f"on {'inherited' if inherited_tab else 'new'} tab at: {self.page.url}"
        )

        # Probe the SOCKS tunnel before declaring startup complete.
        # connect_over_cdp() only confirms Chromium is alive on localhost;
        # the proxy hasn't carried real traffic yet at that point.
        # A failed probe here raises BrowserStartError so the scheduler
        # treats this the same as any other startup failure (marks FAILED,
        # signals startup_complete, proceeds to next task).
        await self._probe_proxy()

        return self.page

    async def _ssl_recovery_reconnect(self):
        """Tear down the current session so the next _attempt_start() gets a fresh proxy node.

        Does NOT call stop() (which sets _stopped=True) so that task_runner's
        finally block can still invoke stop() cleanly if the retry also fails.
        """
        await self.logger.warning(
            f"SSL proxy failure detected for profile {self.task.profile.ml_profile_id} — "
            f"attempting single profile reconnect."
        )
        # Stop Multilogin profile first so it can upload profile data before Chromium exits.
        await self.multilogin_client.stop_profile(self.task)

        # Then disconnect Playwright (Chromium is already stopped above).
        # Does NOT go through the public stop() so _stopped stays False and the
        # task_runner finally block works correctly.
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        await asyncio.sleep(5)

    async def _socks_recovery_reconnect(self):
        """Tear down the current session after a SOCKS tunnel collapse.

        Same teardown pattern as _ssl_recovery_reconnect but with a longer
        cooldown. Does NOT set _stopped so task_runner's finally block can
        still call stop() cleanly if the retry also fails.
        """
        await self.logger.warning(
            f"SOCKS tunnel failure detected for profile {self.task.profile.ml_profile_id} — "
            f"attempting single SOCKS reconnect."
        )
        await self.logger.info(
            f"Waiting {SOCKS_RECOVERY_COOLDOWN_SECONDS}s cooldown before reconnect "
            f"to avoid proxy pool pressure amplification."
        )
        # Stop Multilogin profile first so it manages the shutdown and profile sync.
        await self.multilogin_client.stop_profile(self.task)

        # Then disconnect Playwright (Chromium is already stopped above).
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        await asyncio.sleep(SOCKS_RECOVERY_COOLDOWN_SECONDS)

    async def reconnect(self):
        """Stop the current session and start a completely fresh one after a SOCKS failure.

        Called by task_runner when a SOCKS_PROXY_FAILURE is detected mid-scenario.
        Returns the new page. Raises BrowserStartError if the new session cannot
        start or if proxy probes fail — caller treats that as a terminal task failure.
        """
        await self._socks_recovery_reconnect()
        await self.logger.info(
            f"Re-running proxy probes after SOCKS reconnect for profile "
            f"{self.task.profile.ml_profile_id}."
        )
        return await self._attempt_start()

    async def _ssl_mid_scenario_reconnect(self):
        """Tear down the current session after a mid-scenario SSL cert failure.

        Same teardown pattern as _socks_recovery_reconnect with a shorter cooldown
        (SSL cert failures are node-specific, not pool-pressure related). Does NOT
        set _stopped so task_runner's finally block can still clean up if the retry fails.
        """
        await self.logger.warning(
            f"SSL proxy failure detected for profile {self.task.profile.ml_profile_id} — "
            f"attempting single SSL reconnect."
        )
        await self.logger.info(
            f"Waiting {SSL_MID_SCENARIO_COOLDOWN_SECONDS}s cooldown before SSL reconnect."
        )
        # Stop Multilogin profile first so it manages the shutdown and profile sync.
        await self.multilogin_client.stop_profile(self.task)

        # Then disconnect Playwright (Chromium is already stopped above).
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        await asyncio.sleep(SSL_MID_SCENARIO_COOLDOWN_SECONDS)

    async def reconnect_ssl(self):
        """Stop the current session and start a completely fresh one after an SSL failure.

        Called by task_runner when an SSL_PROXY_FAILURE is detected mid-scenario.
        The HTTPS probe inside _attempt_start() re-validates TLS, so a bad proxy
        node will be caught at probe time rather than returning a broken session.
        Returns the new page. Raises BrowserStartError on probe or start failure.
        """
        await self._ssl_mid_scenario_reconnect()
        await self.logger.info(
            f"Re-running proxy probes after SSL reconnect for profile "
            f"{self.task.profile.ml_profile_id}."
        )
        return await self._attempt_start()

    async def _probe_proxy(self):
        """Validate the SOCKS tunnel and TLS behaviour before handing the browser to the scenario.

        Step 1 — HTTP: confirms TCP routing through the SOCKS tunnel works. Tries each
        endpoint in PROXY_PROBE_HTTP_ENDPOINTS; first success wins. ERR_SOCKS* or
        ERR_CERT* on any endpoint is immediately fatal. Timeout / endpoint unavailable
        advances to the next. All failing → BrowserStartError.

        Step 2 — HTTPS: detects proxy nodes that intercept TLS with a wrong certificate.
        ERR_CERT* on any endpoint is immediately fatal. Other failures advance to the
        next endpoint. All failing with non-cert errors → warn and continue (probe
        server outage should not block startup).
        """
        pid = self.task.profile.ml_profile_id

        # Step 1: HTTP — prove SOCKS tunnel routes TCP
        http_ok = False
        http_last_err = None
        for endpoint in PROXY_PROBE_HTTP_ENDPOINTS:
            await self.logger.info(
                f"HTTP proxy probe ({endpoint}) for profile {pid} "
                f"(timeout={PROXY_PROBE_TIMEOUT}ms)."
            )
            try:
                response = await self.page.goto(
                    endpoint,
                    wait_until="domcontentloaded",
                    timeout=PROXY_PROBE_TIMEOUT,
                )
                status = response.status if response else None
                await self.logger.info(
                    f"HTTP proxy probe OK for profile {pid} via {endpoint} (status={status})."
                )
                http_ok = True
                break
            except Exception as e:
                err_str = str(e)
                if "ERR_SOCKS" in err_str or "ERR_CERT" in err_str:
                    raise BrowserStartError(
                        f"Proxy probe fatal error for profile {pid} at {endpoint}: "
                        f"{type(e).__name__}: {e}"
                    ) from e
                http_last_err = e
                await self.logger.warning(
                    f"HTTP proxy probe endpoint unavailable for profile {pid} at {endpoint} "
                    f"({type(e).__name__}) — trying next endpoint."
                )

        if not http_ok:
            raise BrowserStartError(
                f"HTTP proxy probe failed on all endpoints for profile {pid}. "
                f"Last error: {type(http_last_err).__name__}: {http_last_err}"
            )

        # Step 2: HTTPS — detect TLS-intercepting proxy nodes
        for endpoint in PROXY_PROBE_HTTPS_ENDPOINTS:
            await self.logger.info(
                f"HTTPS proxy probe ({endpoint}) for profile {pid}."
            )
            try:
                await self.page.goto(
                    endpoint,
                    wait_until="domcontentloaded",
                    timeout=PROXY_PROBE_TIMEOUT,
                )
                await self.logger.info(
                    f"HTTPS proxy probe OK for profile {pid} via {endpoint}."
                )
                return
            except Exception as e:
                err_str = str(e)
                if "ERR_CERT" in err_str:
                    raise BrowserStartError(
                        f"HTTPS probe SSL certificate error for profile {pid} at {endpoint} — "
                        f"proxy node likely intercepts TLS: {type(e).__name__}: {e}"
                    ) from e
                await self.logger.warning(
                    f"HTTPS proxy probe non-cert failure for profile {pid} at {endpoint} "
                    f"({type(e).__name__}) — trying next endpoint."
                )

        await self.logger.warning(
            f"HTTPS proxy probe failed on all endpoints for profile {pid} "
            f"(non-cert errors only — probe servers may be unavailable). Continuing startup."
        )

    async def stop(self):
        # Guard against double stop
        if self._stopped:
            return
        self._stopped = True

        # 1. Stop Multilogin profile via API first.
        # This lets Multilogin manage the Chromium shutdown and upload the profile
        # data (cookies, localStorage) to cloud storage before Chromium exits.
        # Calling playwright.stop() first would send Browser.close via CDP, killing
        # Chromium before Multilogin gets to sync the session — losing the cookies.
        await self.multilogin_client.stop_profile(self.task)

        # 2. Disconnect Playwright (browser is already stopped above)
        if self.playwright is not None:
            try:
                await self.playwright.stop()
                await self.logger.info("Playwright stopped")
            except Exception as e:
                await self.logger.error(f"Failed to stop Playwright: {e}", exc=e)
