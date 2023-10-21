"""Base Python Class for pyadtpulse."""

import logging
import asyncio
import datetime
import re
import time
from random import randint
from threading import RLock, Thread
from typing import List, Optional, Tuple, Union
from warnings import warn

import uvloop
from aiohttp import ClientResponse, ClientSession
from bs4 import BeautifulSoup

from .alarm_panel import ADT_ALARM_UNKNOWN
from .const import (
    ADT_DEFAULT_HTTP_HEADERS,
    ADT_DEFAULT_KEEPALIVE_INTERVAL,
    ADT_DEFAULT_RELOGIN_INTERVAL,
    ADT_GATEWAY_STRING,
    ADT_LOGIN_URI,
    ADT_LOGOUT_URI,
    ADT_MAX_KEEPALIVE_INTERVAL,
    ADT_MAX_RELOGIN_BACKOFF,
    ADT_MIN_RELOGIN_INTERVAL,
    ADT_SUMMARY_URI,
    ADT_SYNC_CHECK_URI,
    ADT_TIMEOUT_URI,
    API_HOST_CA,
    DEFAULT_API_HOST,
)
from .pulse_connection import ADTPulseConnection
from .site import ADTPulseSite
from .util import (
    AuthenticationException,
    DebugRLock,
    close_response,
    handle_response,
    make_soup,
)

LOG = logging.getLogger(__name__)

SYNC_CHECK_TASK_NAME = "ADT Pulse Sync Check Task"
KEEPALIVE_TASK_NAME = "ADT Pulse Keepalive Task"


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
        "_last_login_time",
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
    def _check_service_host(service_host: str) -> None:
        if service_host is None or service_host == "":
            raise ValueError("Service host is mandatory")
        if service_host not in (DEFAULT_API_HOST, API_HOST_CA):
            raise ValueError(
                "Service host must be one of {DEFAULT_API_HOST}" f" or {API_HOST_CA}"
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
        websession: Optional[ClientSession] = None,
        do_login: bool = True,
        debug_locks: bool = False,
        keepalive_interval: Optional[int] = ADT_DEFAULT_KEEPALIVE_INTERVAL,
        relogin_interval: Optional[int] = ADT_DEFAULT_RELOGIN_INTERVAL,
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
        self._check_service_host(service_host)
        self._init_login_info(username, password, fingerprint)
        self._pulse_connection = ADTPulseConnection(
            service_host,
            session=websession,
            user_agent=user_agent,
            debug_locks=debug_locks,
        )

        self._sync_task: Optional[asyncio.Task] = None
        self._timeout_task: Optional[asyncio.Task] = None

        # FIXME use thread event/condition, regular condition?
        # defer initialization to make sure we have an event loop
        self._authenticated: Optional[asyncio.locks.Event] = None
        self._login_exception: Optional[BaseException] = None

        self._updates_exist: Optional[asyncio.locks.Event] = None

        self._session_thread: Optional[Thread] = None
        self._attribute_lock: Union[RLock, DebugRLock]
        if not debug_locks:
            self._attribute_lock = RLock()
        else:
            self._attribute_lock = DebugRLock("PyADTPulse._attribute_lock")
        self._last_login_time: int = 0

        self._site: Optional[ADTPulseSite] = None
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
        self._check_service_host(host)
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

    async def _update_sites(self, soup: BeautifulSoup) -> None:
        with self._attribute_lock:
            if self._site is None:
                await self._initialize_sites(soup)
                if self._site is None:
                    raise RuntimeError("pyadtpulse could not retrieve site")
            self._site.alarm_control_panel._update_alarm_from_soup(soup)
            self._site._update_zone_from_soup(soup)

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
                    if not await new_site._fetch_devices(None):
                        LOG.error("Could not fetch zones from ADT site")
                    new_site.alarm_control_panel._update_alarm_from_soup(soup)
                    if new_site.alarm_control_panel.status == ADT_ALARM_UNKNOWN:
                        new_site.gateway.is_online = False
                    new_site._update_zone_from_soup(soup)
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

    def _check_retry_after(
        self, response: Optional[ClientResponse], task_name: str
    ) -> int:
        """
        Check the "Retry-After" header in the response and return the number of seconds
        to wait before retrying the task.

        Parameters:
            response (Optional[ClientResponse]): The response object.
            task_name (str): The name of the task.

        Returns:
            int: The number of seconds to wait before retrying the task.
        """
        if response is None:
            return 0
        header_value = response.headers.get("Retry-After")
        if header_value is None:
            return 0
        if header_value.isnumeric():
            retval = int(header_value)
        else:
            try:
                retval = (
                    datetime.datetime.strptime(header_value, "%a, %d %b %G %T %Z")
                    - datetime.datetime.now()
                ).seconds
            except ValueError:
                return 0
        reason = "Unknown"
        if response.status == 429:
            reason = "Too many requests"
        elif response.status == 503:
            reason = "Service unavailable"
        LOG.warning(
            "Task %s received Retry-After %s due to %s", task_name, retval, reason
        )
        return retval

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
        retry_after: int = 0
        response: ClientResponse | None = None
        task_name: str = self._get_task_name(self._timeout_task, KEEPALIVE_TASK_NAME)
        LOG.debug("creating %s", task_name)

        with self._attribute_lock:
            self._validate_authenticated_event()
        while self._authenticated.is_set():
            relogin_interval = self.relogin_interval
            if self._should_relogin(relogin_interval):
                if not await self._handle_relogin(task_name):
                    return
            try:
                await asyncio.sleep(self._calculate_sleep_time(retry_after))
                LOG.debug("Resetting timeout")
                response = await self._reset_pulse_cloud_timeout()
                if (
                    not handle_response(
                        response,
                        logging.WARNING,
                        "Could not reset ADT Pulse cloud timeout",
                    )
                    or response is None
                ):  # shut up linter
                    continue
                success, retry_after = self._handle_timeout_response(response)
                if not success:
                    continue
                await self._update_gateway_device_if_needed()

            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
                close_response(response)
                return

    def _validate_authenticated_event(self) -> None:
        if self._authenticated is None:
            raise RuntimeError(
                "Keepalive task is running without an authenticated event"
            )

    def _should_relogin(self, relogin_interval: int) -> bool:
        """
        Checks if the user should re-login based on the relogin interval and the time
        since the last login.

        Parameters:
            relogin_interval (int): The interval in seconds between re-logins.

        Returns:
            bool: True if the user should re-login, False otherwise.
        """
        return relogin_interval != 0 and time.time() - self._last_login_time > randint(
            int(0.75 * relogin_interval), relogin_interval
        )

    async def _handle_relogin(self, task_name: str) -> bool:
        """Do a relogin from keepalive task."""
        LOG.info("Login timeout reached, re-logging in")
        with self._attribute_lock:
            try:
                await self._cancel_task(self._sync_task)
            except Exception as e:
                LOG.warning("Unhandled exception %s while cancelling %s", e, task_name)
            return await self._do_logout_and_relogin(0.0)

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

    async def _do_logout_and_relogin(self, relogin_wait_time: float) -> bool:
        """
        Performs a logout and re-login process.

        Args:
            relogin_wait_time (float): The amount of time to wait before re-logging in.

        Returns:
            bool: True if the re-login process is successful, False otherwise.
        """
        current_task = asyncio.current_task()
        await self._do_logout_query()
        await asyncio.sleep(relogin_wait_time)
        if not await self.async_quick_relogin():
            task_name: str | None = None
            if current_task is not None:
                task_name = current_task.get_name()
            LOG.error("%s could not re-login, exiting", task_name or "(Unknown task)")
            return False
        if current_task is not None and current_task == self._sync_task:
            return True
        with self._attribute_lock:
            if self._sync_task is not None:
                coro = self._sync_check_task()
                self._sync_task = asyncio.create_task(
                    coro, name=f"{SYNC_CHECK_TASK_NAME}: Async session"
                )
        return True

    def _calculate_sleep_time(self, retry_after: int) -> int:
        return self.keepalive_interval * 60 + retry_after

    async def _reset_pulse_cloud_timeout(self) -> ClientResponse | None:
        return await self._pulse_connection.async_query(ADT_TIMEOUT_URI, "POST")

    def _handle_timeout_response(self, response: ClientResponse) -> Tuple[bool, int]:
        """
        Handle the timeout response from the client.

        Args:
            response (ClientResponse): The client response object.

        Returns:
            Tuple[bool, int]: A tuple containing a boolean value indicating whether the
            response was handled successfully and an integer indicating the
            retry after value.
        """
        if not handle_response(
            response, logging.INFO, "Failed resetting ADT Pulse cloud timeout"
        ):
            retry_after = self._check_retry_after(response, "Keepalive task")
            close_response(response)
            return False, retry_after
        close_response(response)
        return True, 0

    async def _update_gateway_device_if_needed(self) -> None:
        if self.site.gateway.next_update < time.time():
            await self.site._set_device(ADT_GATEWAY_STRING)

    async def _sync_check_task(self) -> None:
        """Asynchronous function that performs a synchronization check task."""
        task_name = self._get_sync_task_name()
        LOG.debug("creating %s", task_name)

        response = None
        retry_after = 0
        initial_relogin_interval = (
            current_relogin_interval
        ) = self.site.gateway.poll_interval
        last_sync_text = "0-0-0"

        self._validate_updates_exist(task_name)

        last_sync_check_was_different = False
        while True:
            try:
                self.site.gateway.adjust_backoff_poll_interval()
                pi = (
                    self.site.gateway.poll_interval
                    if not last_sync_check_was_different
                    else 0.0
                )
                await asyncio.sleep(max(retry_after, pi))

                response = await self._perform_sync_check_query()

                if response is None or self._check_and_handle_retry(
                    response, task_name
                ):
                    close_response(response)
                    continue

                text = await response.text()
                if not await self._validate_sync_check_response(
                    response, text, current_relogin_interval
                ):
                    current_relogin_interval = min(
                        ADT_MAX_RELOGIN_BACKOFF, current_relogin_interval * 2
                    )
                    close_response(response)
                    continue
                current_relogin_interval = initial_relogin_interval
                close_response(response)
                if self._handle_updates_exist(text, last_sync_text):
                    last_sync_check_was_different = True
                    last_sync_text = text
                    continue
                if await self._handle_no_updates_exist(
                    last_sync_check_was_different, task_name, text
                ):
                    last_sync_check_was_different = False
                    continue
            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
                close_response(response)
                return

    def _validate_updates_exist(self, task_name: str) -> None:
        if self._updates_exist is None:
            raise RuntimeError(f"{task_name} started without update event initialized")

    async def _perform_sync_check_query(self):
        return await self._pulse_connection.async_query(
            ADT_SYNC_CHECK_URI, extra_params={"ts": str(int(time.time() * 1000))}
        )

    def _check_and_handle_retry(self, response, task_name):
        retry_after = self._check_retry_after(response, f"{task_name}")
        if retry_after != 0:
            self._set_update_failed(response)
            return True
        return False

    async def _validate_sync_check_response(
        self,
        response: ClientResponse,
        text: str,
        current_relogin_interval: float,
    ) -> bool:
        """
        Validates the sync check response received from the ADT Pulse site.

        Args:
            response (ClientResponse): The HTTP response object.
            text (str): The response text.
            current_relogin_interval (float): The current relogin interval.

        Returns:
            bool: True if the sync check response is valid, False otherwise.
        """
        if not handle_response(response, logging.ERROR, "Error querying ADT sync"):
            self._set_update_failed(response)
            return False

        pattern = r"\d+[-]\d+[-]\d+"
        if not re.match(pattern, text):
            LOG.warning(
                "Unexpected sync check format (%s), "
                "forcing re-auth after %f seconds",
                pattern,
                current_relogin_interval,
            )
            LOG.debug("Received %s from ADT Pulse site", text)
            await self._do_logout_and_relogin(current_relogin_interval)
            self._set_update_failed(None)
            return False
        return True

    def _handle_updates_exist(self, text: str, last_sync_text: str) -> bool:
        if text != last_sync_text:
            LOG.debug("Updates exist: %s, requerying", text)
            return True
        return False

    async def _handle_no_updates_exist(
        self, have_updates: bool, task_name: str, text: str
    ) -> None:
        if have_updates:
            if await self.async_update() is False:
                LOG.debug("Pulse data update from %s failed", task_name)
                return
            # shouldn't need to call _validate_updates_exist, but just in case
            self._validate_updates_exist(task_name)
            self._updates_exist.set()
        else:
            if self.detailed_debug_logging:
                LOG.debug("Sync token %s indicates no remote updates to process", text)

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
        if self._authenticated is not None:
            while self._authenticated.is_set():
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
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Get event loop.

        Returns:
            Optional[asyncio.AbstractEventLoop]: the event loop object or
                                                 None if no thread is running
        """
        return self._pulse_connection.loop

    async def async_quick_relogin(self) -> bool:
        """Quickly re-login to Pulse.

        Doesn't do device queries or set connected event unless a failure occurs.
        FIXME:  Should probably just re-work login logic."""
        response = await self._do_login_query()
        if not handle_response(response, logging.ERROR, "Could not re-login to Pulse"):
            await self.async_logout()
            return False
        return True

    def quick_relogin(self) -> bool:
        """Perform quick_relogin synchronously."""
        coro = self.async_quick_relogin()
        return asyncio.run_coroutine_threadsafe(
            coro,
            self._pulse_connection.check_sync(
                "Attempting to do call sync quick re-login from async"
            ),
        ).result()

    async def _do_login_query(self, timeout: int = 30) -> ClientResponse | None:
        """
        Performs a login query to the Pulse site.

        Args:
            timeout (int, optional): The timeout value for the query in seconds.
            Defaults to 30.

        Returns:
            ClientResponse | None: The response from the query or None if the login
            was unsuccessful.
        """
        try:
            retval = await self._pulse_connection.async_query(
                ADT_LOGIN_URI,
                method="POST",
                extra_params={
                    "partner": "adt",
                    "e": "ns",
                    "usernameForm": self.username,
                    "passwordForm": self._password,
                    "fingerprint": self._fingerprint,
                    "sun": "yes",
                },
                timeout=timeout,
            )
        except Exception as e:  # pylint: disable=broad-except
            LOG.error("Could not log into Pulse site: %s", e)
            return None
        if retval is None:
            LOG.error("Could not log into Pulse site.")
            return None
        if not handle_response(
            retval,
            logging.ERROR,
            "Error encountered communicating with Pulse site on login",
        ):
            close_response(retval)
            return None
        self._last_login_time = int(time.time())
        return retval

    async def _do_logout_query(self) -> None:
        """Performs a logout query to the ADT Pulse site."""
        params = {}
        network: ADTPulseSite = self.site
        if network is not None:
            params.update({"network": str(network.id)})
        params.update({"partner": "adt"})
        await self._pulse_connection.async_query(
            ADT_LOGOUT_URI, extra_params=params, timeout=10
        )

    async def async_login(self) -> bool:
        """Login asynchronously to ADT.

        Returns: True if login successful
        """
        if self._authenticated is None:
            self._authenticated = asyncio.locks.Event()
        else:
            self._authenticated.clear()

        LOG.debug("Authenticating to ADT Pulse cloud service as %s", self._username)
        await self._pulse_connection.async_fetch_version()

        response = await self._do_login_query()
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
        # need to set authenticated here to prevent login loop
        self._authenticated.set()
        await self._update_sites(soup)
        if self._site is None:
            LOG.error("Could not retrieve any sites, login failed")
            self._authenticated.clear()
            return False

        # since we received fresh data on the status of the alarm, go ahead
        # and update the sites with the alarm status.

        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(
                self._keepalive_task(), name=f"{KEEPALIVE_TASK_NAME}"
            )
        if self._updates_exist is None:
            self._updates_exist = asyncio.locks.Event()
        await asyncio.sleep(0)
        return True

    async def async_logout(self) -> None:
        """Logout of ADT Pulse async."""
        LOG.info("Logging %s out of ADT Pulse", self._username)
        await self._cancel_task(self._timeout_task)
        await self._cancel_task(self._sync_task)
        self._timeout_task = self._sync_task = None
        await self._do_logout_query()
        if self._authenticated is not None:
            self._authenticated.clear()

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

    def _set_update_failed(self, resp: ClientResponse | None) -> None:
        """Sets update failed, sets updates_exist to notify wait_for_update
        and closes response if necessary."""
        with self._attribute_lock:
            self._update_succeded = False
            if resp is not None:
                close_response(resp)
            if self._updates_exist is not None:
                self._updates_exist.set()

    def _check_update_succeeded(self) -> bool:
        """Check if update succeeded, clears the update event and
        resets _update_succeeded.
        """
        with self._attribute_lock:
            old_update_succeded = self._update_succeded
            self._update_succeded = True
            if self._updates_exist is None:
                return False
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
        with self._attribute_lock:
            if self._authenticated is None:
                return False
            return self._authenticated.is_set()

    # FIXME? might have to move this to site for multiple sites

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
    def sites(self) -> List[ADTPulseSite]:
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
