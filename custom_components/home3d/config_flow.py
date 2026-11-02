"""Adding a home: the repository its model packages are released in, a token that may read it, which home. The first
model downloads inside the flow, so an entry never starts without one. Reauth for a new token; options for the sidebar
icon and which light is which lamp, which area is which room."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any

try:  # Home Assistant 2026.10 validates with probatio, earlier releases with voluptuous (the same API)
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    IconSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_install_release, home_lock, model_store
from .const import CONF_HOME, CONF_REPOSITORY, DOMAIN
from .github import GitHubAuthError, GitHubError, GitHubSource, Release, newest

_LOGGER = logging.getLogger(__name__)

REPOSITORY = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class Home3DConfigFlow(ConfigFlow, domain=DOMAIN):
    """Repository and token, then the home, then the first download."""

    VERSION = 1

    def __init__(self) -> None:
        self._repository = ""
        self._token = ""
        self._latest: dict[str, Release] = {}
        self._home = ""
        self._title = ""
        self._icon = "mdi:home"
        self._install: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._error = ""
        self._created = False

    async def _releases(self, repository: str, token: str) -> dict[str, Release]:
        source = GitHubSource(async_get_clientsession(self.hass), repository, token)
        found = await source.releases()
        return {h: r for h in dict.fromkeys(r.home for r in found) if (r := newest([x for x in found if x.home == h]))}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            m = REPOSITORY.match(user_input[CONF_REPOSITORY].strip())
            if not m:
                errors[CONF_REPOSITORY] = "invalid_repository"
            else:
                try:
                    latest = await self._releases(m[1], user_input[CONF_TOKEN].strip())
                except GitHubAuthError:
                    errors["base"] = "invalid_auth"
                except GitHubError:
                    errors["base"] = "cannot_connect"
                else:
                    taken = {e.data[CONF_HOME] for e in self._async_current_entries()}
                    self._latest = {h: r for h, r in latest.items() if h not in taken}
                    if not latest:
                        errors["base"] = "no_models"
                    elif not self._latest:
                        return self.async_abort(reason="all_added")
                    else:
                        self._repository, self._token = m[1], user_input[CONF_TOKEN].strip()
                        return await self.async_step_home()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REPOSITORY, default=(user_input or {}).get(CONF_REPOSITORY, "")): str,
                    vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
            errors=errors,
        )

    async def async_step_home(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and user_input[CONF_HOME] in self.hass.data.get(frontend.DATA_PANELS, {}):
            errors[CONF_HOME] = "path_taken"  # the sidebar path /<home> belongs to another panel (e.g. /map)
        elif user_input is not None:
            self._home = user_input[CONF_HOME]
            await self.async_set_unique_id(self._home)
            self._abort_if_unique_id_configured()
            self._title = (user_input.get("title") or "").strip() or self._home
            self._icon = user_input.get("icon") or "mdi:home"
            return await self.async_step_download()
        first = next(iter(self._latest))
        return self.async_show_form(
            step_id="home",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOME, default=first): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=h, label=f"{h} ({r.version})") for h, r in self._latest.items()]
                        )
                    ),
                    vol.Optional("title"): str,
                    vol.Required("icon", default="mdi:home"): IconSelector(),
                }
            ),
            description_placeholders={"repository": self._repository},
            errors=errors,
        )

    async def async_step_download(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._task is None:
            # the install runs on its own: closing the dialog (which cancels the flow's task) never cuts it off
            # half-way; async_remove() discards the model if no entry came of it
            self._install = self.hass.async_create_background_task(self._first_install(), f"{DOMAIN} {self._home} first model")
            self._task = self.hass.async_create_task(self._wait())
        if not self._task.done():
            return self.async_show_progress(
                step_id="download", progress_action="download", progress_task=self._task,
                description_placeholders={"version": self._latest[self._home].version},
            )
        try:
            meta = self._task.result()
        except GitHubAuthError:
            self._error = "GitHub refused the token for the download"
            return self.async_show_progress_done(next_step_id="download_failed")
        except HomeAssistantError as err:
            self._error = str(err)
            return self.async_show_progress_done(next_step_id="download_failed")
        except Exception as err:  # noqa: BLE001 - anything unexpected ends the flow with its reason, never a 500
            _LOGGER.exception("Home 3D %s: first download failed", self._home)
            self._error = str(err) or type(err).__name__
            return self.async_show_progress_done(next_step_id="download_failed")
        if self._title == self._home:
            self._title = meta.get("title") or self._home
        return self.async_show_progress_done(next_step_id="finish")

    async def _first_install(self) -> dict[str, Any]:
        release = self._latest[self._home]
        source = GitHubSource(async_get_clientsession(self.hass), self._repository, self._token)

        def progress(pct: int) -> None:
            self.async_update_progress(pct / 100)

        async with home_lock(self.hass, self._home):
            return await async_install_release(self.hass, source, model_store(self.hass, self._home), release, progress)

    async def _wait(self) -> dict[str, Any]:
        assert self._install is not None
        return await asyncio.shield(self._install)

    @callback
    def async_remove(self) -> None:
        """The flow ends: a model downloaded for an entry that was never created is removed again."""
        if self._install is None or self._created:
            return
        home = self._home

        async def discard() -> None:
            if any(e.unique_id == home for e in self._async_current_entries(include_ignore=False)):
                return
            async with home_lock(self.hass, home):
                await self.hass.async_add_executor_job(model_store(self.hass, home).remove)

        self._install.add_done_callback(lambda _: self.hass.async_create_background_task(discard(), f"{DOMAIN} {home} discard"))

    async def async_step_download_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="download_failed", description_placeholders={"error": self._error})

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._created = True
        return self.async_create_entry(
            title=self._title,
            data={CONF_REPOSITORY: self._repository, CONF_TOKEN: self._token, CONF_HOME: self._home},
            options={"icon": self._icon},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._releases(entry.data[CONF_REPOSITORY], user_input[CONF_TOKEN].strip())
            except GitHubAuthError:
                errors["base"] = "invalid_auth"
            except GitHubError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_and_abort(entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN].strip()})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}),
            description_placeholders={"repository": entry.data[CONF_REPOSITORY]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return Home3DOptionsFlow()


def _labels(items: list[tuple[str, str]]) -> dict[str, str]:
    """id -> a form label that is unique (the id added where two labels are the same)."""
    seen: dict[str, int] = {}
    for _, label in items:
        seen[label] = seen.get(label, 0) + 1
    return {i: (label if seen[label] == 1 else f"{label} ({i})") for i, label in items}


class Home3DOptionsFlow(OptionsFlow):
    """The sidebar icon, and the model's own mapping overridden on site: a light per lamp, areas per room.

    Stored only where it differs from the model's home.json, so a later model's mapping still applies to an untouched
    install.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        meta = await self.hass.async_add_executor_job(model_store(self.hass, self.config_entry.data[CONF_HOME]).home_json)
        if meta is None:
            return self.async_abort(reason="no_model")
        rooms = {r["id"]: r["name"] for r in meta.get("rooms", [])}
        lamp_label = _labels([(l["id"], f"{rooms.get(l['room'], l['room'])} · {l['label']}") for l in meta.get("lamps", [])])
        room_label = _labels(list(rooms.items()))

        # the model's areas are matched by name: the ones this Home Assistant has, by id
        registry = ar.async_get(self.hass)
        default_areas: dict[str, list[str]] = {}
        for a in meta.get("areas", []):
            if (found := registry.async_get_area_by_name(a["name"])) is not None:
                default_areas.setdefault(a["room"], []).append(found.id)
        default_areas = {k: sorted(v) for k, v in default_areas.items()}
        default_lights = meta.get("lights", {})

        options = self.config_entry.options
        if user_input is not None:
            lamps_in, rooms_in = user_input.get("lamps", {}), user_input.get("rooms", {})
            lights = {i: e for i, k in lamp_label.items() if (e := lamps_in.get(k))}
            areas = {i: sorted(ids) for i, k in room_label.items() if (ids := rooms_in.get(k))}
            new: dict[str, Any] = {"icon": user_input.get("icon") or options.get("icon")}
            if lights != default_lights:
                new["lights"] = lights
            if areas != default_areas:
                new["areas"] = areas
            return self.async_create_entry(data=new)

        lights = options.get("lights", default_lights)
        areas = options.get("areas", default_areas)
        light = EntitySelector(EntitySelectorConfig(domain="light"))
        area = AreaSelector(AreaSelectorConfig(multiple=True))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("icon", default=options.get("icon") or meta.get("icon") or "mdi:home"): IconSelector(),
                    vol.Required("lamps"): section(
                        vol.Schema(
                            {vol.Optional(k, description={"suggested_value": lights.get(i)}): light for i, k in lamp_label.items()}
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required("rooms"): section(
                        vol.Schema(
                            {vol.Optional(k, description={"suggested_value": areas.get(i, [])}): area for i, k in room_label.items()}
                        ),
                        {"collapsed": True},
                    ),
                }
            ),
        )
