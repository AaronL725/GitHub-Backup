"""Bounded GitHub API access. Source clients cannot issue mutations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .common import BackupError, atomic_json, read_json, sha256


def check_write_cooldown(path):
    if path and read_json(Path(path), {}).get("until", 0) > time.time():
        raise APIError(403, secondary=True)


class APIError(BackupError):
    def __init__(self, status, *, secondary=False):
        self.status = status
        self.secondary = secondary
        message = (
            "GitHub destination write cooldown is active; Drive collection continues"
            if secondary
            else f"GitHub API failed (HTTP {status})"
        )
        super().__init__(message)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or not (
            parsed.hostname == "github.com"
            or parsed.hostname == "api.github.com"
            or (parsed.hostname or "").endswith(".githubusercontent.com")
        ):
            raise BackupError("Refused an untrusted download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and parsed.netloc != urlsplit(req.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(
        self,
        token,
        *,
        writable=False,
        timeout=60,
        retries=5,
        sleep=time.sleep,
        protected_repository_ids=(),
        cooldown_file=None,
        cooldown_seconds=3600,
    ):
        self.token, self.writable = token, writable
        self.timeout, self.retries, self.sleep = timeout, retries, sleep
        self.opener = build_opener(SafeRedirect())
        self.protected_repository_ids = frozenset(protected_repository_ids)
        self.cooldown_file = Path(cooldown_file) if cooldown_file else None
        self.cooldown_seconds = cooldown_seconds

    def _request(self, method, path, body=None, accept="application/vnd.github+json"):
        if not path.startswith("/") or path.startswith("//"):
            raise BackupError("Expected a GitHub API path")
        if method != "GET":
            if not self.writable:
                raise BackupError("Source API is read only")
            allowed = (
                (
                    method == "POST"
                    and path == "/user/repos"
                    and body.get("private") is True
                )
                or (
                    method == "PATCH"
                    and re.fullmatch(r"/repos/[\w.-]+/[\w.-]+", path)
                    and bool(body)
                    and set(body) <= {"private", "name", "default_branch"}
                    and ("private" not in body or body["private"] is True)
                )
                or (
                    method == "PUT"
                    and re.fullmatch(
                        r"/repos/[\w.-]+/[\w.-]+/actions/permissions", path
                    )
                    and body == {"enabled": False}
                )
            )
            if not allowed:
                raise BackupError("Mutation refused by backup API allowlist")
            check_write_cooldown(self.cooldown_file)
            if self.protected_repository_ids and path.startswith("/repos/"):
                parts = path.split("/")
                target = self.get("/".join(parts[:4]))
                if target["id"] in self.protected_repository_ids:
                    raise BackupError(
                        "Existing destination repository is protected; mutation refused"
                    )
        request = Request(
            "https://api.github.com" + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": accept,
                "User-Agent": "ghbackup/1.0",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        for attempt in range(self.retries + 1):
            try:
                return self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                secondary = False
                if exc.code in (403, 429):
                    try:
                        message = json.loads(exc.read(65536)).get("message", "").lower()
                        secondary = (
                            "secondary rate limit" in message
                            or "temporarily blocked from content creation" in message
                        )
                    except (ValueError, AttributeError):
                        pass
                if (
                    secondary
                    and self.writable
                    and method != "GET"
                    and self.cooldown_file
                ):
                    delay = max(
                        self.cooldown_seconds,
                        float(exc.headers.get("Retry-After", "0")),
                    )
                    atomic_json(
                        self.cooldown_file,
                        {
                            "until": time.time() + delay,
                            "reason": "secondary_content_creation_rate_limit",
                            "seconds": delay,
                        },
                    )
                    raise APIError(exc.code, secondary=True) from None
                rate_limited = exc.code in (403, 429) and (
                    exc.headers.get("Retry-After") is not None
                    or exc.headers.get("X-RateLimit-Remaining") == "0"
                    or exc.code == 429
                    or secondary
                )
                if attempt == self.retries or not (rate_limited or exc.code >= 500):
                    raise APIError(exc.code) from None
                delay = 2**attempt
                if rate_limited:
                    delay = max(
                        60,
                        delay,
                        float(exc.headers.get("Retry-After", "0")),
                        float(exc.headers.get("X-RateLimit-Reset", "0"))
                        - time.time()
                        + 1,
                    )
                # Leave long rate-limit windows for the next scheduled run.
                if delay > 300:
                    raise APIError(exc.code) from None
                self.sleep(delay)
            except (URLError, TimeoutError, OSError):
                if attempt == self.retries:
                    raise BackupError("GitHub network request failed") from None
                self.sleep(2**attempt)

    def call(self, method, path, body=None):
        with self._request(method, path, body) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.call("GET", path)

    def pages(self, path, params=None):
        params = dict(params or {}, per_page=100)
        for page in range(1, 100001):
            data = self.get(path + "?" + urlencode(dict(params, page=page)))
            if not isinstance(data, list):
                raise BackupError("Unexpected GitHub pagination response")
            yield from data
            if len(data) < 100:
                return
        raise BackupError("Pagination did not terminate")

    def optional(self, path):
        try:
            return self.get(path)
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def asset(self, path, destination: Path, expected_size: int, digest=None):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = destination.with_name(destination.name + ".partial")
        try:
            with self._request(
                "GET", path, accept="application/octet-stream"
            ) as response:
                h, size = hashlib.sha256(), 0
                with partial.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                        size += len(chunk)
                        h.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if size != expected_size:
                raise BackupError("Release asset size mismatch")
            actual = h.hexdigest()
            if digest and digest.startswith("sha256:") and digest[7:] != actual:
                raise BackupError("Release asset digest mismatch")
            os.replace(partial, destination)
            return actual
        finally:
            partial.unlink(missing_ok=True)


def backup_releases(api: GitHub, repo: dict, root: Path):
    """ID-based paths avoid tag/filename collisions and detect replaced assets."""
    path = f"/repos/{repo['full_name']}"
    releases = list(api.pages(path + "/releases"))
    live_assets = []
    for release in releases:
        rid = int(release["id"])
        folder = root / str(rid)
        atomic_json(folder / "release.json", release)
        # Fetch separately: do not assume an embedded assets array is complete.
        assets = list(api.pages(path + f"/releases/{rid}/assets"))
        for asset in assets:
            aid = int(asset["id"])
            file = folder / f"{aid}.bin"
            info_path = folder / f"{aid}.json"
            previous = read_json(info_path, {})
            identity = {
                k: asset.get(k) for k in ("id", "name", "size", "updated_at", "digest")
            }
            if not (
                previous.get("identity") == identity
                and file.is_file()
                and file.stat().st_size == asset["size"]
                and sha256(file) == previous.get("sha256")
            ):
                checksum = api.asset(
                    path + f"/releases/assets/{aid}",
                    file,
                    asset["size"],
                    asset.get("digest"),
                )
                atomic_json(info_path, {"identity": identity, "sha256": checksum})
            live_assets.append(
                {
                    "release_id": rid,
                    "asset_id": aid,
                    "original_name": asset["name"],
                    "path": str(file.relative_to(root)),
                }
            )
    atomic_json(
        root / "inventory.json",
        {"release_ids": [r["id"] for r in releases], "assets": live_assets},
    )
