"""ADT Pulse Async API."""
import logging
import asyncio
import re
import time
from datetime import datetime
from random import randint
from warnings import warn

from aiohttp import ClientSession
from bs4 import BeautifulSoup
from typeguard import typechecked
from yarl import URL

from .alarm_panel import ADT_ALARM_UNKNOWN
from .const import (
    ADT_DEFAULT_HTTP_USER_AGENT,
    ADT_DEFAULT_KEEPALIVE_INTERVAL,
    ADT_DEFAULT_RELOGIN_INTERVAL,
    ADT_GATEWAY_STRING,
    ADT_SYNC_CHECK_URI,
    ADT_TIMEOUT_URI,
    DEFAULT_API_HOST,
)
from .pulse_authentication_properties import PulseAuthenticationProperties
from .pulse_connection import PulseConnection
from .pulse_connection_properties import PulseConnectionProperties
from .pulse_connection_status import PulseConnectionStatus
from .pyadtpulse_properties import PyADTPulseProperties
from .site import ADTPulseSite
from .util import handle_response, set_debug_lock

LOG = logging.getLogger(__name__)
SYNC_CHECK_TASK_NAME = "ADT Pulse Sync Check Task"
KEEPALIVE_TASK_NAME = "ADT Pulse Keepalive Task"
RELOGIN_BACKOFF_WARNING_THRESHOLD = 5.0 * 60.0


class PyADTPulseAsync:
    """ADT Pulse Async API."""

    __slots__ = (
        "_sync_task",
        "_timeout_task",
        "_pa_attribute_lock",
        "_pulse_properties",
        "_authentication_properties",
        "_pulse_connection_properties",
        "_pulse_connection",
        "_pulse_connection_status",
        "_site",
        "_detailed_debug_logging",
    )

    @typechecked
    def __init__(
        self,
        username: str,
        password: str,
        fingerprint: str,
        service_host: str = DEFAULT_API_HOST,
        user_agent=ADT_DEFAULT_HTTP_USER_AGENT["User-Agent"],
        websession: ClientSession | None = None,
        debug_locks: bool = False,
        keepalive_interval: int = ADT_DEFAULT_KEEPALIVE_INTERVAL,
        relogin_interval: int = ADT_DEFAULT_RELOGIN_INTERVAL,
        detailed_debug_logging: bool = False,
    ) -> None:
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
        self._pa_attribute_lock = set_debug_lock(
            debug_locks, "pyadtpulse.pa_attribute_lock"
        )
        self._pulse_connection_properties = PulseConnectionProperties(
            service_host, websession, user_agent, detailed_debug_logging, debug_locks
        )
        self._authentication_properties = PulseAuthenticationProperties(
            username=username,
            password=password,
            fingerprint=fingerprint,
            debug_locks=debug_locks,
        )
        self._pulse_connection_status = PulseConnectionStatus(debug_locks=debug_locks)
        self._pulse_properties = PyADTPulseProperties(
            keepalive_interval=keepalive_interval,
            relogin_interval=relogin_interval,
            detailed_debug_logging=detailed_debug_logging,
            debug_locks=debug_locks,
        )
        self._pulse_connection = PulseConnection(
            self._pulse_connection_status,
            self._pulse_connection_properties,
            self._authentication_properties,
            debug_locks,
        )
        self._sync_task: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None
        self._site: ADTPulseSite | None = None
        self._detailed_debug_logging = detailed_debug_logging

    def __repr__(self) -> str:
        """Object representation."""
        return (
            f"<{self.__class__.__name__}: {self._authentication_properties.username}>"
        )

    async def _update_sites(self, soup: BeautifulSoup) -> None:
        with self._pa_attribute_lock:
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

    def _get_task_name(self, task: asyncio.Task | None, default_name) -> str:
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

        async def reset_pulse_cloud_timeout() -> tuple[int, str | None, URL | None]:
            return await self._pulse_connection.async_query(ADT_TIMEOUT_URI, "POST")

        async def update_gateway_device_if_needed() -> None:
            if self.site.gateway.next_update < time.time():
                await self.site.set_device(ADT_GATEWAY_STRING)

        def should_relogin(relogin_interval: int) -> bool:
            return (
                relogin_interval != 0
                and time.time() - self._authentication_properties.last_login_time
                > randint(int(0.75 * relogin_interval), relogin_interval)
            )

        response: str | None
        task_name: str = self._get_task_name(self._timeout_task, KEEPALIVE_TASK_NAME)
        LOG.debug("creating %s", task_name)

        while True:
            relogin_interval = self._pulse_properties.relogin_interval
            try:
                await asyncio.sleep(self._pulse_properties.keepalive_interval * 60)
                if self._pulse_connection_status.retry_after > time.time():
                    LOG.debug(
                        "%s: Skipping actions because retry_after > now", task_name
                    )
                    continue
                if not self._pulse_connection.is_connected:
                    LOG.debug("%s: Skipping relogin because not connected", task_name)
                    continue
                elif should_relogin(relogin_interval):
                    await self.async_logout()
                    await self._login_looped(task_name)
                    continue
                LOG.debug("Resetting timeout")
                code, response, url = await reset_pulse_cloud_timeout()
                if (
                    not handle_response(
                        code,
                        url,
                        logging.WARNING,
                        "Could not reset ADT Pulse cloud timeout",
                    )
                    or response is None
                ):
                    continue
                await update_gateway_device_if_needed()

            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
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

    async def _login_looped(self, task_name: str) -> None:
        """
        Logs in and loops until successful.

        Args:
            None.
        Returns:
            None
        """
        log_level = logging.DEBUG
        login_successful = False

        while not login_successful:
            LOG.log(log_level, "%s performming loop login", task_name)
            login_successful = await self.async_login()
            if login_successful:
                return
            self._pulse_properties.set_update_status(False)

    async def _sync_check_task(self) -> None:
        """Asynchronous function that performs a synchronization check task."""

        async def perform_sync_check_query():
            return await self._pulse_connection.async_query(
                ADT_SYNC_CHECK_URI,
                extra_headers={"Sec-Fetch-Mode": "iframe"},
                extra_params={"ts": str(int(time.time() * 1000))},
            )

        task_name = self._get_sync_task_name()
        LOG.debug("creating %s", task_name)

        response_text: str | None = None
        code: int = 200
        last_sync_text = "0-0-0"
        last_sync_check_was_different = False
        url: URL | None = None

        async def validate_sync_check_response() -> bool:
            """
            Validates the sync check response received from the ADT Pulse site.
            Returns:
                bool: True if the sync check response is valid, False otherwise.
            """
            if not handle_response(code, url, logging.ERROR, "Error querying ADT sync"):
                self._pulse_properties.set_update_status(False)
                return False
            # this should have already been handled
            if response_text is None:
                LOG.warning("Internal Error: response_text is None")
                return False
            pattern = r"\d+[-]\d+[-]\d+"
            if not re.match(pattern, response_text):
                LOG.warning(
                    "Unexpected sync check format (%s), forcing re-auth",
                    response_text,
                )
                LOG.debug("Received %s from ADT Pulse site", response_text)
                await self.async_logout()
                await self._login_looped(task_name)
                return False
            return True

        async def handle_no_updates_exist() -> bool:
            if last_sync_check_was_different:
                if await self.async_update() is False:
                    LOG.debug("Pulse data update from %s failed", task_name)
                    return False
                self._pulse_properties.updates_exist.set()
                return True
            else:
                if self._detailed_debug_logging:
                    LOG.debug(
                        "Sync token %s indicates no remote updates to process",
                        response_text,
                    )
            return False

        def handle_updates_exist() -> bool:
            if response_text != last_sync_text:
                LOG.debug("Updates exist: %s, requerying", response_text)
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
                retry_after = self._pulse_connection_status.retry_after
                if retry_after > time.time():
                    LOG.debug(
                        "%s: Waiting for retry after %s",
                        task_name,
                        datetime.fromtimestamp(retry_after),
                    )
                    self._pulse_properties.set_update_status(False)
                    await asyncio.sleep(retry_after - time.time())
                    continue
                await asyncio.sleep(pi)

                code, response_text, url = await perform_sync_check_query()
                if not handle_response(
                    code, url, logging.WARNING, "Error querying ADT sync"
                ):
                    continue
                if response_text is None:
                    LOG.warning("Sync check received no response from ADT Pulse site")
                    continue
                if not await validate_sync_check_response():
                    continue
                if handle_updates_exist():
                    last_sync_check_was_different = True
                    last_sync_text = response_text
                    continue
                if await handle_no_updates_exist():
                    last_sync_check_was_different = False
                    continue
            except asyncio.CancelledError:
                LOG.debug("%s cancelled", task_name)
                return

    async def async_login(self) -> bool:
        """Login asynchronously to ADT.

        Returns: True if login successful
        """
        if self._pulse_connection.login_in_progress:
            LOG.debug("Login already in progress, returning")
            return True
        LOG.debug(
            "Authenticating to ADT Pulse cloud service as %s",
            self._authentication_properties.username,
        )
        await self._pulse_connection.async_fetch_version()

        soup = await self._pulse_connection.async_do_login_query(
            self._authentication_properties.username,
            self._authentication_properties.password,
            self._authentication_properties.fingerprint,
        )
        if soup is None:
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
        if self._pulse_connection.login_in_progress:
            LOG.debug("Login in progress, returning")
            return
        LOG.info(
            "Logging %s out of ADT Pulse", self._authentication_properties.username
        )
        if asyncio.current_task() not in (self._sync_task, self._timeout_task):
            await self._cancel_task(self._timeout_task)
            await self._cancel_task(self._sync_task)
            self._timeout_task = self._sync_task = None
        await self._pulse_connection.async_do_logout_query(self.site.id)

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

    async def wait_for_update(self) -> bool:
        """Wait for update.

        Blocks current async task until Pulse system
        signals an update
        FIXME?: This code probably won't work with multiple waiters.
        """
        with self._pa_attribute_lock:
            if self._sync_task is None:
                coro = self._sync_check_task()
                self._sync_task = asyncio.create_task(
                    coro, name=f"{SYNC_CHECK_TASK_NAME}: Async session"
                )
                await asyncio.sleep(0)
        if self._pulse_properties.updates_exist is None:
            raise RuntimeError("Update event does not exist")

        await self._pulse_properties.updates_exist.wait()
        return self._pulse_properties.check_update_succeeded()

    @property
    def sites(self) -> list[ADTPulseSite]:
        """Return all sites for this ADT Pulse account."""
        warn(
            "multiple sites being removed, use pyADTPulse.site instead",
            PendingDeprecationWarning,
            stacklevel=2,
        )
        with self._pa_attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return [self._site]

    @property
    def site(self) -> ADTPulseSite:
        """Return the site associated with the Pulse login."""
        with self._pa_attribute_lock:
            if self._site is None:
                raise RuntimeError(
                    "No sites have been retrieved, have you logged in yet?"
                )
            return self._site

    @property
    def is_connected(self) -> bool:
        """Convenience method to return whether ADT Pulse is connected."""
        return self._pulse_connection.is_connected
