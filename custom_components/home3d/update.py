"""The home's model as an update entity: a newer model package in the repository's releases is offered in
Settings > Updates and installs without a restart."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Home3DConfigEntry, async_install_release, home_lock
from .const import CONF_HOME, DOMAIN
from .github import GitHubAuthError


async def async_setup_entry(
    hass: HomeAssistant, entry: Home3DConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    version = await hass.async_add_executor_job(entry.runtime_data.store.current_version)
    async_add_entities([ModelUpdate(entry, version)])


class ModelUpdate(CoordinatorEntity, UpdateEntity):
    """The installed model package against the newest one in the repository."""

    _attr_has_entity_name = True
    _attr_translation_key = "model"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS | UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, entry: Home3DConfigEntry, installed: str | None) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._entry = entry
        self._installed = installed
        self._progress: int | None = None
        home = entry.data[CONF_HOME]
        self._lock = home_lock(entry.runtime_data.coordinator.hass, home)
        self._attr_unique_id = f"{home}_model"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, home)}, name=entry.title, entry_type=DeviceEntryType.SERVICE
        )

    @property
    def installed_version(self) -> str | None:
        return self._installed

    @property
    def latest_version(self) -> str | None:
        release = self.coordinator.data
        return release.version if release else self._installed

    @property
    def release_url(self) -> str | None:
        release = self.coordinator.data
        return release.url if release else None

    @property
    def in_progress(self) -> bool:
        return self._lock.locked()

    @property
    def update_percentage(self) -> int | None:
        return self._progress

    @property
    def available(self) -> bool:  # the installed model stays known while GitHub can't be reached
        return True

    async def async_release_notes(self) -> str | None:
        release = self.coordinator.data
        return release.notes if release else None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        data = self._entry.runtime_data
        if self._lock.locked():
            raise HomeAssistantError("This home's model is already being installed")
        async with self._lock:
            release = self.coordinator.data
            if release is None:
                raise HomeAssistantError("No model package found in the repository")

            @callback
            def progress(pct: int) -> None:
                if pct != self._progress:
                    self._progress = pct
                    self.async_write_ha_state()

            self._progress = 0
            self.async_write_ha_state()
            try:
                await async_install_release(self.hass, data.source, data.store, release, progress)
            except GitHubAuthError as err:
                self._entry.async_start_reauth(self.hass)
                raise HomeAssistantError(f"GitHub refused the token: {err}") from err
            finally:
                self._progress = None
        # the panel registers again with the new model (no restart)
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
