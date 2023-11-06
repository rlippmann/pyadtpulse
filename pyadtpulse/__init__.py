"""Base Python Class for pyadtpulse."""

import logging
import asyncio
import re
import time
from datetime import datetime
from random import randint
from threading import RLock, Thread
from typing import Union
from warnings import warn

import uvloop
from aiohttp import ClientResponse, ClientSession
from bs4 import BeautifulSoup

from pyadtpulse.alarm_panel import ADT_ALARM_UNKNOWN
from pyadtpulse.const import (
    ADT_DEFAULT_HTTP_HEADERS,
    ADT_DEFAULT_KEEPALIVE_INTERVAL,
    ADT_DEFAULT_RELOGIN_INTERVAL,
    ADT_GATEWAY_STRING,
    ADT_MAX_KEEPALIVE_INTERVAL,
    ADT_MAX_RELOGIN_BACKOFF,
    ADT_MIN_RELOGIN_INTERVAL,
    ADT_SUMMARY_URI,
    ADT_SYNC_CHECK_URI,
    ADT_TIMEOUT_URI,
    DEFAULT_API_HOST,
)
from pyadtpulse.pulse_connection import ADTPulseConnection
from pyadtpulse.site import ADTPulseSite
from pyadtpulse.util import (
    AuthenticationException,
    DebugRLock,
    close_response,
    handle_response,
    make_soup,
)

LOG = logging.getLogger(__name__)

SYNC_CHECK_TASK_NAME = "ADT Pulse Sync Check Task"
KEEPALIVE_TASK_NAME = "ADT Pulse Keepalive Task"
RELOGIN_BACKOFF_WARNING_THRESHOLD = 5.0 * 60.0


class PyADTPulse:
    """Base object for ADT Pulse service."""

    __slots__ = (
        "_pulse_connection",
        "_sync_task",
        "_timeout_task",
        "_authenticated",
        "_updates_exist",
        "_session_thread",
        "_attribute_lock",
        "_site",
        "_username",
        "_password",
        "_fingerprint",
        "_login_exception",
        "_relogin_interval",
        "_keepalive_interval",
        "_update_succeded",
        "_detailed_debug_logging",
    )

    @staticmethod
    def _check_keepalive_interval(keepalive_interval: int) -> None:
        if keepalive_interval > ADT_MAX_KEEPALIVE_INTERVAL or keepalive_interval <= 0:
            raise ValueError(
                f"keepalive interval ({keepalive_interval}) must be "
                f"greater than 0 and less than {ADT_MAX_KEEPALIVE_INTERVAL}"
            )

    @staticmethod
    def _check_relogin_interval(relogin_interval: int) -> None:
        if relogin_interval < ADT_MIN_RELOGIN_INTERVAL:
            raise ValueError(
                f"relogin interval ({relogin_interval}) must be "
                f"greater than {ADT_MIN_RELOGIN_INTERVAL}"
            )

    def __init__(
        self,
        username: str,
        password: str,
        fingerprint: str,
        service_host: str = DEFAULT_API_HOST,
        user_agent=ADT_DEFAULT_HTTP_HEADERS["User-Agent"],
        websession: ClientSession | None = None,
        do_login: bool = True,
        debug_locks: bool = False,
        keepalive_interval: int = ADT_DEFAULT_KEEPALIVE_INTERVAL,
        relogin_interval: int = ADT_DEFAULT_RELOGIN_INTERVAL,
        detailed_debug_logging: bool = False,
    ):
        """Create a PyADTPulse object.

        Args:
            username (str): Username.
            password (str): Password.
            fingerprint (str): 2FA fingerprint.
            service_host (str, optional): host prefix to use
                         i.e. https://portal.adtpulse.com or
                              https://portal-ca.adtpulse.com
            user_agent (str, optional): User Agent.
                         Defaults to ADT_DEFAULT_HTTP_HEADERS["User-Agent"].
            websession (ClientSession, optional): an initialized
                        aiohttp.ClientSession to use, defaults to None
            do_login (bool, optional): login synchronously when creating object
                            Should be set to False for asynchronous usage
                            and async_login() should be called instead
                            Setting websession will override this
                            and not login
                        Defaults to True
            debug_locks: (bool, optional): use debugging locks
                        Defaults to False
            keepalive_interval (int, optional): number of minutes between
                        keepalive checks, defaults to ADT_DEFAULT_KEEPALIVE_INTERVAL,
                        maxiumum is ADT_MAX_KEEPALIVE_INTERVAL
            relogin_interval (int, optional): number of minutes between relogin checks
                        defaults to ADT_DEFAULT_RELOGIN_INTERVAL,
                        minimum is ADT_MIN_RELOGIN_INTERVAL
            detailed_debug_logging (bool, optional): enable detailed debug logging
        """
        self._init_login_info(username, password, fingerprint)
        self._pulse_connection = ADTPulseConnection(
            service_host,
            session=websession,
            user_agent=user_agent,
            debug_locks=debug_locks,
            detailed_debug_logging=detailed_debug_logging,
        )

        self._sync_task: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None

        # FIXME use thread event/condition, regular condition?
        # defer initialization to make sure we have an event loop
        self._login_exception: BaseException | None = None

        self._updates_exist = asyncio.locks.Event()

        self._session_thread: Thread | None = None
        self._attribute_lock: RLock | DebugRLock
        if not debug_locks:
            self._attribute_lock = RLock()
        else:
            self._attribute_lock = DebugRLock("PyADTPulse._attribute_lock")

        self._site: ADTPulseSite | None = None
        self.keepalive_interval = keepalive_interval
        self.relogin_interval = relogin_interval
        self._detailed_debug_logging = detailed_debug_logging
        self._update_succeded = True

        # authenticate the user
        if do_login and websession is None:
            self.login()

    def _init_login_info(self, username: str, password: str, fingerprint: str) -> None:
        if username is None or username == "":
            raise ValueError("Username is mandatory")

        pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        if not re.match(pattern, username):
            raise ValueError("Username must be an email address")
        self._username = username

        if password is None or password == "":
            raise ValueError("Password is mandatory")
        self._password = password

        if fingerprint is None or fingerprint == "":
            raise ValueError("Fingerprint is required")
        self._fingerprint = fingerprint

    def __repr__(self) -> str:
        """Object representation."""
        return f"<{self.__class__.__name__}: {self._username}>"

    # ADTPulse API endpoint is configurable (besides default US ADT Pulse endpoint) to
    # support testing as well as alternative ADT Pulse endpoints such as
    # portal-ca.adtpulse.com

    @property
    def service_host(self) -> str:
        """Get the Pulse host.

        Returns: (str): the ADT Pulse endpoint host
        """
        return self._pulse_connection.service_host

    @service_host.setter
    def service_host(self, host: str) -> None:
        """Override the Pulse host (i.e. to use portal-ca.adpulse.com).

        Args:
            host (str): name of Pulse endpoint host
        """
        self._pulse_connection.check_service_host(host)
        with self._attribute_lock:
            self._pulse_connection.service_host = host

    def set_service_host(self, host: str) -> None:
        """Backward compatibility for service host property setter."""
        self.service_host = host

    @property
    def username(self) -> str:
        """Get username.

        Returns:
            str: the username
        """
        with self._attribute_lock:
            return self._username

    @property
    def version(self) -> str:
        """Get the ADT Pulse site version.

        Returns:
            str: a string containing the version
        """
        return self._pulse_connection.api_version

    @property
    def relogin_interval(self) -> int:
        """Get re-login interval.

        Returns:
            int: number of minutes to re-login to Pulse
                 0 means disabled
        """
        with self._attribute_lock:
            return self._relogin_interval

    @relogin_interval.setter
    def relogin_interval(self, interval: int | None) -> None:
        """Set re-login interval.

        Args:
            interval (int): The number of minutes between logins.
                            If set to None, resets to ADT_DEFAULT_RELOGIN_INTERVAL

        Raises:
            ValueError: if a relogin interval of less than 10 minutes
                        is specified
        """
        if interval is None:
            interval = ADT_DEFAULT_RELOGIN_INTERVAL
        else:
            self._check_relogin_interval(interval)
        with self._attribute_lock:
            self._relogin_interval = interval
            LOG.debug("relogin interval set to %d", self._relogin_interval)

    @property
    def keepalive_interval(self) -> int:
        """Get the keepalive interval in minutes.

        Returns:
            int: the keepalive interval
        """
        with self._attribute_lock:
            return self._keepalive_interval

    @keepalive_interval.setter
    def keepalive_interval(self, interval: int | None) -> None:
        """Set the keepalive interval in minutes.

        If set to None, resets to ADT_DEFAULT_KEEPALIVE_INTERVAL
        """
        if interval is None:
            interval = ADT_DEFAULT_KEEPALIVE_INTERVAL
        else:
            self._check_keepalive_interval(interval)
        with self._attribute_lock:
            self._keepalive_interval = interval
            LOG.debug("keepalive interval set to %d", self._keepalive_interval)

    @property
    def detailed_debug_logging(self) -> bool:
        """Get the detailed debug logging flag."""
        with self._attribute_lock:
            return self._detailed_debug_logging

    @detailed_debug_logging.setter
    def detailed_debug_logging(self, value: bool) -> None:
        """Set detailed debug logging flag."""
        with self._attribute_lock:
            self._detailed_debug_logging = value
        self._pulse_connection.detailed_debug_logging = value

    async def _update_sites(self, soup: BeautifulSoup) -> None:
        with self._attribute_lock:
            if self._site is None:
                await self._initialize_sites(soup)
                if self._site is None:
                    raise RuntimeError("pyadtpulse could not retrieve site")
            self._site.alarm_control_panel.update_alarm_from_soup(soup)
            self._site.update_zone_from_soup(soup)

    async def _initialize_sites(self, soup: BeautifulSoup) -> None:
        """
        Initializes the sites in the ADT Pulse account.

        Args:
            soup (BeautifulSoup): The parsed HTML soup object.
        """
        # typically, ADT Pulse accounts have only a single site (premise/location)
        single_premise = soup.find("span", {"id": "p_singlePremise"})
        if single_premise:
            site_name = single_premise.text

            # FIXME: this code works, but it doesn't pass the linter
            signout_link = str(
                soup.find("a", {"class": "p_signoutlink"}).get("href")  # type: ignore
            )
            if signout_link:
                m = re.search("networkid=(.+)&", signout_link)
                if m and m.group(1) and m.group(1):
                    site_id = m.group(1)
                    LOG.debug("Discovered site id %s: %s", site_id, site_name)
                    new_site = ADTPulseSite(self._pulse_connection, site_id, site_name)

                    # fetch zones first, so that we can have the status
                    # updated with _update_alarm_status
                    if not await new_site.fetch_devices(None):
                        LOG.error("Could not fetch zones from ADT site")
                    new_site.alarm_control_panel.update_alarm_from_soup(soup)
                    if new_site.alarm_control_panel.status == ADT_ALARM_UNKNOWN:
                        new_site.gateway.is_online = False
                    new_site.update_zone_from_soup(soup)
                    with self._attribute_lock:
                        self._site = new_site
                    return
            else:
                LOG.warning(
                    "Couldn't find site id for %s in %s", site_name, signout_link
                )
        else:
            LOG.error("ADT Pulse accounts with MULTIPLE sites not supported!!!")

    # ...and current network id from:
    # <a id="p_signout1" class="p_signoutlink"
    # href="/myhome/16.0.0-131/access/signout.jsp?networkid=150616za043597&partner=adt"
    # onclick="return flagSignOutInProcess();">
    #
    # ... or perhaps better, just extract all from /system/settings.jsp

    def _get_task_name(self, task, default_name) -> str:
        """
        Get the name of a task.

        Parameters:
            task (Task): The task object.
            default_name (str): The default name to use if the task is None.

        Returns:
            str: The name of the task if it is not None, otherwise the default name
            with a suffix indicating a possible internal error.
        """
        if task is not None:
            return task.get_name()
        return f"{default_name} - possible internal error"

    def _get_sync_task_name(self) -> str:
        return self._get_task_name(self._sync_task, SYNC_CHECK_TASK_NAME)

    def _get_timeout_task_name(self) -> str:
        return self._get_task_name(self._timeout_task, KEEPALIVE_TASK_NAME)

    async def _keepalive_task(self) -> None:
        """
        Asynchronous function that runs a keepalive task to maintain the connection
        with the ADT Pulse cloud.
        """

        async def reset_pulse_cloud_timeout() -> ClientResponse | None:
            return await self._pulse_connection.async_query(ADT_TIMEOUT_URI, "POST")

        async def update_gateway_device_if_needed() -> None:
            if self.site.gateway.next_update < time.time():
                await self.site.set_device(ADT_GATEWAY_STRING)

        def should_relogin(relogin_interval: int) -> bool:
            return (
                relogin_interval != 0
                and time.time() - self._pulse_connection.last_login_time
                > randint(int(0.75 * relogin_interval), relogin_interval)
            )

        response: ClientResponse | None = None
        task_name: str = self._get_task_name(self._timeout_task, KEEPALIVE_TASK_NAME)
        LOG.debug("creating %s", task_name)

        while True:
            relogin_interval = self.relogin_interval
            try:
                await asyncio.sleep(self.keepalive_interval * 60)
                if self._pulse_connection.retry_after > time.time():
                    LOG.debug(
                        "%s: Skipping actions because retry_after > now", task_name
                    )
                    continue
                if not self.is_connected:
                    LOG.debug("%s: Skipping relogin because not connected", task_name)
                    continue
                elif should_relogin(relogin_interval):
                    await self.async_logout()
                    await self._do_login_with_backoff(task_name)
                    continue
                LOG.debug("Resetting timeout")
                response = await reset_pulse_cloud_timeout()
                if (
                    not handle_response(
                        response,
                        logging.WARNING,
                        "Could not reset ADT Pulse cloud timeout",
                    )
                    or response is None
                ):  # shut up linter
                    continue
                await update_gateway_device_if_needed()

            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
                close_response(response)
                return

    async def _cancel_task(self, task: asyncio.Task | None) -> None:
        """
        Cancel a given asyncio task.

        Args:
            task (asyncio.Task | None): The task to be cancelled.
        """
        if task is None:
            return
        task_name = task.get_name()
        LOG.debug("cancelling %s", task_name)
        try:
            task.cancel()
        except asyncio.CancelledError:
            LOG.debug("%s successfully cancelled", task_name)
            await task

    def _set_update_status(self, value: bool) -> None:
        """Sets update failed, sets updates_exist to notify wait_for_update."""
        with self._attribute_lock:
            self._update_succeded = value
            if self._updates_exist is not None and not self._updates_exist.is_set():
                self._updates_exist.set()

    async def _do_login_with_backoff(self, task_name: str) -> None:
        """
        Performs a logout and re-login process.

        Args:
            None.
        Returns:
            None
        """
        log_level = logging.DEBUG
        login_backoff = 0.0
        login_successful = False

        def compute_login_backoff() -> float:
            if login_backoff == 0.0:
                return self.site.gateway.poll_interval
            return min(ADT_MAX_RELOGIN_BACKOFF, login_backoff * 2.0)

        while not login_successful:
            LOG.log(
                log_level, "%s logging in with backoff %f", task_name, login_backoff
            )
            await asyncio.sleep(login_backoff)
            login_successful = await self.async_login()
            if login_successful:
                if login_backoff != 0.0:
                    self._set_update_status(True)
                return
            # only set flag on first failure
            if login_backoff == 0.0:
                self._set_update_status(False)
            login_backoff = compute_login_backoff()
            if login_backoff > RELOGIN_BACKOFF_WARNING_THRESHOLD:
                log_level = logging.WARNING

    async def _sync_check_task(self) -> None:
        """Asynchronous function that performs a synchronization check task."""

        async def perform_sync_check_query():
            return await self._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI, extra_params={"ts": str(int(time.time() * 1000))}
            )

        task_name = self._get_sync_task_name()
        LOG.debug("creating %s", task_name)

        response = None
        last_sync_text = "0-0-0"
        last_sync_check_was_different = False

        async def validate_sync_check_response() -> bool:
            """
            Validates the sync check response received from the ADT Pulse site.
            Returns:
                bool: True if the sync check response is valid, False otherwise.
            """
            if not handle_response(response, logging.ERROR, "Error querying ADT sync"):
                self._set_update_status(False)
                close_response(response)
                return False
            close_response(response)
            pattern = r"\d+[-]\d+[-]\d+"
            if not re.match(pattern, text):
                LOG.warning(
                    "Unexpected sync check format (%s), forcing re-auth",
                    text,
                )
                LOG.debug("Received %s from ADT Pulse site", text)
                await self.async_logout()
                await self._do_login_with_backoff(task_name)
                return False
            return True

        async def handle_no_updates_exist() -> bool:
            if last_sync_check_was_different:
                if await self.async_update() is False:
                    LOG.debug("Pulse data update from %s failed", task_name)
                    return False
                self._updates_exist.set()
                return True
            else:
                if self.detailed_debug_logging:
                    LOG.debug(
                        "Sync token %s indicates no remote updates to process", text
                    )
            return False

        def handle_updates_exist() -> bool:
            if text != last_sync_text:
                LOG.debug("Updates exist: %s, requerying", text)
                return True
            return False

        while True:
            try:
                self.site.gateway.adjust_backoff_poll_interval()
                pi = (
                    self.site.gateway.poll_interval
                    if not last_sync_check_was_different
                    else 0.0
                )
                retry_after = self._pulse_connection.retry_after
                if retry_after > time.time():
                    LOG.debug(
                        "%s: Waiting for retry after %s",
                        task_name,
                        datetime.fromtimestamp(retry_after),
                    )
                    self._set_update_status(False)
                    await asyncio.sleep(retry_after - time.time())
                    continue
                await asyncio.sleep(pi)

                response = await perform_sync_check_query()
                if not handle_response(
                    response, logging.WARNING, "Error querying ADT sync"
                ):
                    close_response(response)
                    continue
                text = await response.text()
                if not await validate_sync_check_response():
                    continue
                if handle_updates_exist():
                    last_sync_check_was_different = True
                    last_sync_text = text
                    continue
                if await handle_no_updates_exist():
                    last_sync_check_was_different = False
                    continue
            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
                close_response(response)
                return

    def _pulse_session_thread(self) -> None:
        """
        Pulse the session thread.

        Acquires the attribute lock and creates a background thread for the ADT
        Pulse API. The thread runs the synchronous loop `_sync_loop()` until completion.
        Once the loop finishes, the thread is closed, the pulse connection's event loop
        is set to `None`, and the session thread is set to `None`.
        """
        # lock is released in sync_loop()
        self._attribute_lock.acquire()

        LOG.debug("Creating ADT Pulse background thread")
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        loop = asyncio.new_event_loop()
        self._pulse_connection.loop = loop
        loop.run_until_complete(self._sync_loop())

        loop.close()
        self._pulse_connection.loop = None
        self._session_thread = None

    async def _sync_loop(self) -> None:
        """
        Asynchronous function that represents the main loop of the synchronization
        process.

        This function is responsible for executing the synchronization logic. It starts
        by calling the `async_login` method to perform the login operation. After that,
        it releases the `_attribute_lock` to allow other tasks to access the attributes.
        If the login operation was successful, it waits for the `_timeout_task` to
        complete using the `asyncio.wait` function.  If the `_timeout_task` is not set,
        it raises a `RuntimeError` to indicate that background tasks were not created.

        After the waiting process, it enters a while loop that continues as long as the
        `_authenticated` event is set. Inside the loop, it waits for 0.5 seconds using
        the `asyncio.sleep` function. This wait allows the logout process to complete
        before continuing with the synchronization logic.
        """
        result = await self.async_login()
        self._attribute_lock.release()
        if result:
            if self._timeout_task is not None:
                task_list = (self._timeout_task,)
                try:
                    await asyncio.wait(task_list)
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # pylint: disable=broad-except
                    LOG.exception(
                        "Received exception while waiting for ADT Pulse service %s", e
                    )
            else:
                # we should never get here
                raise RuntimeError("Background pyadtpulse tasks not created")
        while self._pulse_connection.authenticated_flag.is_set():
            # busy wait until logout is done
            await asyncio.sleep(0.5)

    def login(self) -> None:
        """Login to ADT Pulse and generate access token.

        Raises:
            AuthenticationException if could not login
        """
        self._attribute_lock.acquire()
        # probably shouldn't be a daemon thread
        self._session_thread = thread = Thread(
            target=self._pulse_session_thread,
            name="PyADTPulse Session",
            daemon=True,
        )
        self._attribute_lock.release()

        self._session_thread.start()
        time.sleep(1)

        # thread will unlock after async_login, so attempt to obtain
        # lock to block current thread until then
        # if it's still alive, no exception
        self._attribute_lock.acquire()
        self._attribute_lock.release()
        if not thread.is_alive():
            raise AuthenticationException(self._username)

    @property
    def attribute_lock(self) -> Union[RLock, DebugRLock]:
        """Get attribute lock for PyADTPulse object.

        Returns:
            RLock: thread Rlock
        """
        return self._attribute_lock

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Get event loop.

        Returns:
            Optional[asyncio.AbstractEventLoop]: the event loop object or
                                                 None if no thread is running
        """
        return self._pulse_connection.loop

    async def async_login(self) -> bool:
        """Login asynchronously to ADT.

        Returns: True if login successful
        """
        LOG.debug("Authenticating to ADT Pulse cloud service as %s", self._username)
        await self._pulse_connection.async_fetch_version()

        response = await self._pulse_connection.async_do_login_query(
            self.username, self._password, self._fingerprint
        )
        if response is None:
            return False
        if self._pulse_connection.make_url(ADT_SUMMARY_URI) != str(response.url):
            # more specifically:
            # redirect to signin.jsp = username/password error
            # redirect to mfaSignin.jsp = fingerprint error
            LOG.error("Authentication error encountered logging into ADT Pulse")
            close_response(response)
            return False

        soup = await make_soup(
            response, logging.ERROR, "Could not log into ADT Pulse site"
        )
        if soup is None:
            return False

        # FIXME: should probably raise exceptions
        error = soup.find("div", {"id": "warnMsgContents"})
        if error:
            LOG.error("Invalid ADT Pulse username/password: %s", error)
            return False
        error = soup.find("div", "responsiveContainer")
        if error:
            LOG.error(
                "2FA authentiation required for ADT pulse username %s: %s",
                self.username,
                error,
            )
            return False
        # if tasks are started, we've already logged in before
        if self._sync_task is not None or self._timeout_task is not None:
            return True
        await self._update_sites(soup)
        if self._site is None:
            LOG.error("Could not retrieve any sites, login failed")
            await self.async_logout()
            return False

        # since we received fresh data on the status of the alarm, go ahead
        # and update the sites with the alarm status.
        self._timeout_task = asyncio.create_task(
            self._keepalive_task(), name=f"{KEEPALIVE_TASK_NAME}"
        )
        await asyncio.sleep(0)
        return True

    async def async_logout(self) -> None:
        """Logout of ADT Pulse async."""
        LOG.info("Logging %s out of ADT Pulse", self._username)
        if asyncio.current_task() not in (self._sync_task, self._timeout_task):
            await self._cancel_task(self._timeout_task)
            await self._cancel_task(self._sync_task)
            self._timeout_task = self._sync_task = None
        await self._pulse_connection.async_do_logout_query(self.site.id)

    def logout(self) -> None:
        """Log out of ADT Pulse."""
        loop = self._pulse_connection.check_sync(
            "Attempting to call sync logout without sync login"
        )
        sync_thread = self._session_thread

        coro = self.async_logout()
        asyncio.run_coroutine_threadsafe(coro, loop)
        if sync_thread is not None:
            sync_thread.join()

    def _check_update_succeeded(self) -> bool:
        """Check if update succeeded, clears the update event and
        resets _update_succeeded.
        """
        with self._attribute_lock:
            old_update_succeded = self._update_succeded
            self._update_succeded = True
            if self._updates_exist.is_set():
                self._updates_exist.clear()
            return old_update_succeded

    @property
    def updates_exist(self) -> bool:
        """Check if updated data exists.

        Returns:
            bool: True if updated data exists
        """
        with self._attribute_lock:
            if self._sync_task is None:
                loop = self._pulse_connection.loop
                if loop is None:
                    raise RuntimeError(
                        "ADT pulse sync function updates_exist() "
                        "called from async session"
                    )
                coro = self._sync_check_task()
                self._sync_task = loop.create_task(
                    coro, name=f"{SYNC_CHECK_TASK_NAME}: Sync session"
                )
            return self._check_update_succeeded()

    async def wait_for_update(self) -> bool:
        """Wait for update.

        Blocks current async task until Pulse system
        signals an update
        FIXME?: This code probably won't work with multiple waiters.
        """
        with self._attribute_lock:
            if self._sync_task is None:
                coro = self._sync_check_task()
                self._sync_task = asyncio.create_task(
                    coro, name=f"{SYNC_CHECK_TASK_NAME}: Async session"
                )
        if self._updates_exist is None:
            raise RuntimeError("Update event does not exist")

        await self._updates_exist.wait()
        return self._check_update_succeeded()

    @property
    def is_connected(self) -> bool:
        """Check if connected to ADT Pulse.

        Returns:
            bool: True if connected
        """
        return (
            self._pulse_connection.authenticated_flag.is_set()
            and self._pulse_connection.retry_after < time.time()
        )

    async def async_update(self) -> bool:
        """Update ADT Pulse data.

        Returns:
            bool: True if update succeeded.
        """
        LOG.debug("Checking ADT Pulse cloud service for updates")

        # FIXME will have to query other URIs for camera/zwave/etc
        soup = await self._pulse_connection.query_orb(
            logging.INFO, "Error returned from ADT Pulse service check"
        )
        if soup is not None:
            await self._update_sites(soup)
            return True

        return False

    def update(self) -> bool:
        """Update ADT Pulse data.

        Returns:
            bool: True on success
        """
        coro = self.async_update()
        return asyncio.run_coroutine_threadsafe(
            coro,
            self._pulse_connection.check_sync(
                "Attempting to run sync update from async login"
            ),
        ).result()

    @property
    def sites(self) -> list[ADTPulseSite]:
        """Return all sites for this ADT Pulse account."""
        warn(
            "multiple sites being removed, use pyADTPulse.site instead",
            PendingDeprecationWarning,
            stacklevel=2,
        )
        with self._attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return [self._site]

    @property
    def site(self) -> ADTPulseSite:
        """Return the site associated with the Pulse login."""
        with self._attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return self._site
