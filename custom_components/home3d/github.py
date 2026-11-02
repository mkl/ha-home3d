"""Model packages as release assets of a (private) GitHub repository, read with a token the user gives."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
from typing import Any

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException
from yarl import URL

from .package import parse_asset

_LOGGER = logging.getLogger(__name__)

API = "https://api.github.com"
CHUNK = 1024 * 1024
MIN_RATE = 100 * 1024  # bytes/s: a download slower than this on average is given up


class GitHubError(Exception):
    """GitHub could not be reached, limited the rate, or answered with an error."""


class GitHubAuthError(GitHubError):
    """The token is refused, or it can't see the repository (GitHub answers 404 for a private repository then)."""


@dataclass(frozen=True)
class Release:
    """One model package: a release asset named home3d-model-HOME-VERSION.zip."""

    home: str
    version: str
    asset_url: str
    size: int
    sha256: str | None
    notes: str | None
    url: str | None


def _rate_limited(resp: aiohttp.ClientResponse) -> bool:
    return resp.status == 429 or (
        resp.status == 403 and (resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers)
    )


def _raise_for(resp: aiohttp.ClientResponse, what: str) -> None:
    if _rate_limited(resp):
        raise GitHubError(f"GitHub's rate limit reached ({what})")
    if resp.status in (401, 403):
        raise GitHubAuthError(f"GitHub refused the token ({what}: {resp.status})")
    if resp.status == 404:
        raise GitHubAuthError(f"{what}: not found, or the token can no longer read the repository")
    if resp.status >= 400:
        raise GitHubError(f"GitHub answered {resp.status} ({what})")


def newest(releases: list[Release]) -> Release | None:
    """The highest version; GitHub's own order (newest first) where versions can't be compared."""
    try:
        return max(releases, key=lambda r: AwesomeVersion(r.version), default=None)
    except AwesomeVersionException:
        return releases[0] if releases else None


class GitHubSource:
    """The model packages in one repository's releases."""

    def __init__(self, session: aiohttp.ClientSession, repository: str, token: str) -> None:
        self._session = session
        self.repository = repository
        self._token = token

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}

    async def releases(self) -> list[Release]:
        """Every model package in the 30 most recent published releases, newest release first."""
        url = f"{API}/repos/{self.repository}/releases?per_page=30"
        try:
            async with self._session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as resp:
                _raise_for(resp, self.repository)
                data: list[dict[str, Any]] = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GitHubError(f"GitHub not reachable: {err}") from err
        found = []
        for rel in data:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for asset in rel.get("assets", []):
                parsed = parse_asset(asset.get("name", ""))
                url = asset.get("url", "")
                if parsed is None or not url.startswith(f"{API}/"):  # the token is sent there: GitHub's API only
                    continue
                digest = asset.get("digest") or ""
                found.append(
                    Release(
                        home=parsed[0],
                        version=parsed[1],
                        asset_url=url,
                        size=int(asset.get("size") or 0),
                        sha256=digest.removeprefix("sha256:").lower() if digest.startswith("sha256:") else None,
                        notes=rel.get("body") or None,
                        url=rel.get("html_url"),
                    )
                )
        return found

    async def latest(self, home: str) -> Release | None:
        """The newest model package of one home."""
        return newest([r for r in await self.releases() if r.home == home])

    async def download(
        self,
        release: Release,
        dest: Path,
        run: Callable[..., Awaitable[Any]],
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Stream the asset to dest, checking its size and SHA-256. `run(fn, *args)` runs blocking work in the executor.

        GitHub answers an asset with a redirect to a pre-signed storage URL that refuses an Authorization header, so
        the redirect is followed by hand, without the token, and only to https.
        """
        total = max(900, release.size // MIN_RATE) if release.size else 3600
        timeout = aiohttp.ClientTimeout(total=total, sock_connect=30, sock_read=60)
        try:
            async with self._session.get(
                release.asset_url, headers=self._headers("application/octet-stream"), allow_redirects=False, timeout=timeout
            ) as first:
                if first.status in (301, 302, 303, 307, 308):
                    location = first.headers.get("Location", "")
                    if URL(location).scheme != "https":
                        raise GitHubError("GitHub redirected the download to a non-https address")
                    async with self._session.get(location, timeout=timeout) as resp:
                        _raise_for(resp, "download")
                        await self._save(resp, release, dest, run, progress)
                else:
                    _raise_for(first, "download")
                    await self._save(first, release, dest, run, progress)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GitHubError(f"download failed: {err}") from err

    async def _save(self, resp, release: Release, dest: Path, run, progress) -> None:
        sha = hashlib.sha256()
        done = 0
        handle = await run(open, dest, "wb")

        def write(chunk: bytes) -> None:  # hashing a megabyte takes milliseconds on a Pi: not in the event loop
            sha.update(chunk)
            handle.write(chunk)

        try:
            async for chunk in resp.content.iter_chunked(CHUNK):
                done += len(chunk)
                if release.size and done > release.size:
                    raise GitHubError(f"the download is larger than the release says ({release.size} bytes)")
                await run(write, chunk)
                if progress and release.size:
                    progress(min(99, done * 100 // release.size))
        finally:
            await run(handle.close)
        if release.size and done != release.size:
            raise GitHubError(f"the download ended after {done} of {release.size} bytes")
        if release.sha256 is None:
            _LOGGER.warning("%s %s: the release gives no SHA-256; only its size was checked", release.home, release.version)
        elif sha.hexdigest() != release.sha256:
            raise GitHubError("the download does not match the release's SHA-256")
