"""Home 3D: a home's 3D model as a sidebar panel of Home Assistant, shown only to its signed-in users.

The integration holds no home. Each config entry is one home whose model package (docs/model-package.md) it downloads
from a GitHub repository's releases with a token the user gives, keeps in Home Assistant's cache folder (backups leave
it out; it can always be downloaded again) and serves from there. Its update entity offers newer model packages;
installing one needs no restart.

Setup never waits for the network: it serves the model already on disk. Only when there is none (a restored backup, a
cleared cache) does it fetch the newest in the background, retried at every check until it succeeds; meanwhile a
repair issue says why the home has no sidebar entry.

Access: the model's files are served only to Home Assistant's signed-in users. The panel element is inlined into the
panel's registration (a data: URL, nothing to fetch). Before it loads the model it POSTs /api/home3d/session through
hass (the user's own bearer token); that sets a random, HttpOnly, SameSite=Strict cookie scoped to /api/home3d/, valid
for an hour and only while the user's refresh token exists and the user may sign in from where the request comes
(local-only users only from the home network). The files under /api/home3d/<home>/ are answered only with that cookie
(or a bearer token or signed path); without it 401.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
from pathlib import Path
import secrets
import time
from typing import Any

from aiohttp import web

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, HomeAssistantError
from homeassistant.helpers import area_registry as ar, config_validation as cv, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import KEY_AUTHENTICATED, KEY_HASS
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_HOME, CONF_REPOSITORY, COOKIE, COOKIE_PATH, DOMAIN, SESSION_TTL, UPDATE_INTERVAL
from .github import GitHubAuthError, GitHubError, GitHubSource, Release
from .package import ModelStore, PackageError

_LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).parent
PLATFORMS = [Platform.UPDATE]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SESSIONS_PER_USER = 5
CHECK_FAILING_AFTER = 24 * 3600  # s without a successful check before a repair issue says so

type Home3DConfigEntry = ConfigEntry[Home3DData]


@dataclass
class Home3DData:
    """What a loaded entry holds."""

    source: GitHubSource
    store: ModelStore
    coordinator: DataUpdateCoordinator[Release | None]
    last_ok: float


def model_store(hass: HomeAssistant, home: str) -> ModelStore:
    return ModelStore(Path(hass.config.cache_path(DOMAIN)), home)


def home_lock(hass: HomeAssistant, home: str) -> asyncio.Lock:
    """One lock per home that outlives reloads of its entry: never two installs of one home at once."""
    return _data(hass)["locks"].setdefault(home, asyncio.Lock())


def _data(hass: HomeAssistant) -> dict[str, Any]:
    """The integration's shared state; a config flow may need it before the integration is set up."""
    data = hass.data.setdefault(DOMAIN, {})
    for key in ("sites", "sessions", "locks"):
        data.setdefault(key, {})
    return data


async def async_install_release(
    hass: HomeAssistant, source: GitHubSource, store: ModelStore, release: Release, progress=None
) -> dict[str, Any]:
    """Download a model package and make it the current model. Call with the home's lock held.

    Raises GitHubAuthError when the token is refused, HomeAssistantError with the reason for anything else.
    """
    dest: Path | None = None
    try:
        dest = await hass.async_add_executor_job(store.new_download, release.size)
        await source.download(release, dest, hass.async_add_executor_job, progress)
        return await hass.async_add_executor_job(store.install, dest, release.version)
    except GitHubAuthError:
        raise
    except (GitHubError, PackageError, OSError) as err:
        raise HomeAssistantError(f"Model {release.version} not installed: {err}") from err
    except asyncio.CancelledError:
        raise
    except Exception as err:
        _LOGGER.exception("Home 3D %s: installing %s failed", release.home, release.version)
        raise HomeAssistantError(f"Model {release.version} not installed: {err}") from err
    finally:
        if dest is not None:  # also when cancelled (HA stopping, the dialog closed); install() removes it itself
            await asyncio.shield(hass.async_add_executor_job(lambda: dest.unlink(missing_ok=True)))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the two views once; they serve only homes with a loaded entry."""
    _data(hass)
    hass.http.register_view(SessionView())
    hass.http.register_view(FileView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Home3DConfigEntry) -> bool:
    home = entry.data[CONF_HOME]
    source = GitHubSource(async_get_clientsession(hass), entry.data[CONF_REPOSITORY], entry.data[CONF_TOKEN])
    store = model_store(hass, home)

    async def check() -> Release | None:
        try:
            release = await source.latest(home)
        except GitHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GitHubError as err:
            raise UpdateFailed(str(err)) from err
        entry.runtime_data.last_ok = time.monotonic()
        return release

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {home}", update_method=check, update_interval=UPDATE_INTERVAL
    )
    entry.runtime_data = Home3DData(source, store, coordinator, time.monotonic())

    lock = home_lock(hass, home)
    if not lock.locked():
        await hass.async_add_executor_job(store.sweep)
    meta = await hass.async_add_executor_job(store.home_json)
    if meta is None:
        entry.async_create_background_task(hass, _fetch_missing(hass, entry), f"{DOMAIN} {home} first model")
    else:
        ir.async_delete_issue(hass, DOMAIN, f"model_missing_{home}")
        await _register_panel(hass, entry, meta, await hass.async_add_executor_job(store.site))

    @callback
    def _after_check() -> None:
        _check_issue(hass, entry)
        if meta is None and not lock.locked() and coordinator.last_update_success:
            entry.async_create_background_task(hass, _fetch_missing(hass, entry), f"{DOMAIN} {home} first model")

    entry.async_on_unload(coordinator.async_add_listener(_after_check))
    # the first check runs in the background: an offline start serves the model on disk all the same
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} {home} check")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


@callback
def _check_issue(hass: HomeAssistant, entry: Home3DConfigEntry) -> None:
    """A warning when no check has reached the repository for a day; gone with the next one that does."""
    data, issue = entry.runtime_data, f"check_failing_{entry.data[CONF_HOME]}"
    if data.coordinator.last_update_success:
        ir.async_delete_issue(hass, DOMAIN, issue)
    elif time.monotonic() - data.last_ok > CHECK_FAILING_AFTER:
        ir.async_create_issue(
            hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="check_failing",
            translation_placeholders={"home": entry.title, "repository": data.source.repository,
                                      "error": str(data.coordinator.last_exception)},
        )


async def _fetch_missing(hass: HomeAssistant, entry: Home3DConfigEntry) -> None:
    """No model on disk: fetch the newest, then load the entry again. A repair issue says why if that fails."""
    data, home = entry.runtime_data, entry.data[CONF_HOME]
    issue = f"model_missing_{home}"
    lock = home_lock(hass, home)
    if lock.locked():
        return
    async with lock:
        try:
            release = await data.source.latest(home)
            if release is None:
                raise HomeAssistantError(f"no model package for '{home}' in {data.source.repository}")
            await async_install_release(hass, data.source, data.store, release)
        except GitHubAuthError:
            entry.async_start_reauth(hass)
            return
        except (GitHubError, HomeAssistantError) as err:
            _LOGGER.warning("Home 3D %s: no model yet: %s", home, err)
            ir.async_create_issue(
                hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key="model_missing", translation_placeholders={"home": entry.title, "error": str(err)},
            )
            return
    ir.async_delete_issue(hass, DOMAIN, issue)
    if hass.config_entries.async_get_entry(entry.entry_id) is not None:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def _register_panel(hass: HomeAssistant, entry: Home3DConfigEntry, meta: dict[str, Any], site: Path) -> None:
    home = entry.data[CONF_HOME]
    panel_js = await hass.async_add_executor_job((HERE / "panel.js").read_bytes)
    options = entry.options
    config: dict[str, Any] = {
        "home": home,
        "title": entry.title,
        "app_url": f"/api/home3d/{home}/index.html",
        "session_url": "home3d/session",
        "version": meta.get("version"),
    }
    if "lights" in options:
        config["lights"] = options["lights"]
    if "areas" in options:
        areas = ar.async_get(hass)
        config["areas"] = [
            {"room": room, "area_id": area_id, "name": a.name if (a := areas.async_get_area(area_id)) else area_id}
            for room, ids in options["areas"].items()
            for area_id in ids
        ]
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=home,
            webcomponent_name="home3d-panel",
            sidebar_title=entry.title,
            sidebar_icon=options.get("icon") or meta.get("icon") or "mdi:home",
            module_url="data:text/javascript;base64," + base64.b64encode(panel_js).decode(),
            # the module is inline (data:), not from another host: nothing for HA to ask the user about
            trust_external=True,
            config=config,
        )
    except ValueError as err:  # a panel at that path already (another integration's, or YAML's)
        raise ConfigEntryError(f"Sidebar path /{home} is taken: {err}") from err
    hass.data[DOMAIN]["sites"][home] = site
    entry.async_on_unload(lambda: _remove_panel(hass, home))


def _remove_panel(hass: HomeAssistant, home: str) -> None:
    frontend.async_remove_panel(hass, home, warn_if_unknown=False)
    hass.data[DOMAIN]["sites"].pop(home, None)


async def _reload(hass: HomeAssistant, entry: Home3DConfigEntry) -> None:
    """Options, the token or the entry's name changed: set it up anew."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Home3DConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: Home3DConfigEntry) -> None:
    """The entry is deleted: its downloaded model and its repair issues go with it (after any running install)."""
    home = entry.data[CONF_HOME]
    for issue in (f"model_missing_{home}", f"check_failing_{home}"):
        ir.async_delete_issue(hass, DOMAIN, issue)
    async with home_lock(hass, home):
        await hass.async_add_executor_job(model_store(hass, home).remove)


def _session_ok(request: web.Request) -> bool:
    """A bearer token or signed path, or a live session cookie of a user who may still sign in from here."""
    if request.get(KEY_AUTHENTICATED):
        return True
    hass = request.app[KEY_HASS]
    session = hass.data[DOMAIN]["sessions"].get(request.cookies.get(COOKIE, ""))
    if session is None or session[1] < time.monotonic():
        return False
    token = hass.auth.async_get_refresh_token(session[0])
    return token is not None and async_user_not_allowed_do_auth(hass, token.user, request) is None


class SessionView(HomeAssistantView):
    """POST with the user's token: sets the cookie the model's files are served with."""

    url = "/api/home3d/session"
    name = "api:home3d:session"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        refresh_token_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        if refresh_token_id is None:  # e.g. a signed path: no user session to tie it to
            return web.Response(status=403)
        sessions: dict[str, tuple[str, float]] = hass.data[DOMAIN]["sessions"]
        now = time.monotonic()
        for key in [k for k, (_, exp) in sessions.items() if exp < now]:
            del sessions[key]
        mine = sorted((exp, k) for k, (rt, exp) in sessions.items() if rt == refresh_token_id)
        for _, key in mine[: max(0, len(mine) - SESSIONS_PER_USER + 1)]:
            del sessions[key]
        token = secrets.token_urlsafe(32)
        sessions[token] = (refresh_token_id, now + SESSION_TTL)
        response = self.json({"expires_in": SESSION_TTL})
        response.set_cookie(
            COOKIE, token, max_age=SESSION_TTL, path=COOKIE_PATH, httponly=True, samesite="Strict", secure=request.secure
        )
        return response


class FileView(HomeAssistantView):
    """The model of a home with a loaded entry, to signed-in users only."""

    url = "/api/home3d/{home}/{path:.*}"
    name = "api:home3d:file"
    requires_auth = False  # checked here: the cookie, as the iframe's requests carry no token

    async def get(self, request: web.Request, home: str, path: str) -> web.StreamResponse:
        if not _session_ok(request):
            return web.Response(status=401, headers={"Cache-Control": "no-store"})
        hass = request.app[KEY_HASS]
        site: Path | None = hass.data[DOMAIN]["sites"].get(home)
        file = await hass.async_add_executor_job(_resolve, site, path or "index.html") if site else None
        if file is None:
            return web.Response(status=404, headers={"Cache-Control": "no-store"})
        # private: never kept by a shared cache; revalidated each time (ETag), so nothing stays usable after sign-out
        return web.FileResponse(file, headers={"Cache-Control": "private, no-cache"})

    head = get


def _resolve(site: Path, path: str) -> Path | None:
    """The file under site/ for a request path, or None (missing, a directory, outside site/, or not a valid path)."""
    try:
        file = (site / path).resolve()
        if not file.is_relative_to(site) or not file.is_file():
            return None
    except (OSError, ValueError):
        return None
    return file
