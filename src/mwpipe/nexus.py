"""Minimal Nexus Mods client. This table shows which API is used for each task:

  collection lookup   GraphQL v2   v3 can only create and edit collections. It has no read endpoint
  mod file downloads  REST v3      GET /games/{game}/mod-file-versions/{id}/download-link
                                   (marked Experimental; if it causes an error, use v1)
  Premium check       REST v1      /users/validate.json, v3 has no user endpoint
  mod lookup          REST v3      GET /games/{game}/mods/{id} and /games/{game}/mod-file-versions/{id}
  publishing          REST v3      /uploads, /uploads/{id}/finalise, /collections[/{id}/revisions]

Nexus gives download links to Premium accounts only. Because of this, mwpipe needs Premium.
The code sends the API key only to api.nexusmods.com. It never sends the key to the CDN hosts that serve the files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from mwpipe import __version__
from mwpipe.util import PipelineError, log, md5sum

API_HOST = "api.nexusmods.com"
API_ROOT = f"https://{API_HOST}"
API_V1 = f"{API_ROOT}/v1"
API_V3 = f"{API_ROOT}/v3"
GRAPHQL_URL = f"{API_ROOT}/v2/graphql"
USER_AGENT = f"mwpipe/{__version__}"
MULTIPART_LIMIT = 100 * 1024 * 1024  # larger files need the multipart upload API


class ApiError(PipelineError):
    """An HTTP error from the Nexus API. The caller uses `status` to decide if it must use a fallback."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CollectionRef:
    game: str
    slug: str
    revision: int | None = None


_COLLECTION_RE = re.compile(
    r"^(?:nxm://|https://(?:www\.|next\.)?nexusmods\.com/(?:games/)?)"
    r"(?P<game>[a-z0-9]+)/collections/(?P<slug>[a-z0-9]+)(?:/revisions/(?P<rev>\d+))?",
    re.IGNORECASE,
)


def parse_collection_ref(text: str, default_game: str = "morrowind") -> CollectionRef:
    """Accept an https URL, an nxm:// URL or a bare `slug[@revision]`."""
    text = text.strip()
    if m := _COLLECTION_RE.match(text):
        rev = int(m["rev"]) if m["rev"] else None
        if rev is None:  # also accept ?revision=N on web URLs
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
            rev = int(query["revision"][0]) if query.get("revision", [""])[0].isdigit() else None
        return CollectionRef(m["game"].lower(), m["slug"].lower(), rev)
    if m := re.fullmatch(r"([a-z0-9]+)(?:@(\d+))?", text, re.I):
        return CollectionRef(default_game, m[1].lower(), int(m[2]) if m[2] else None)
    raise PipelineError(f"This is not a Nexus collection URL, nxm link or slug: {text!r}")


class NexusClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._user: dict | None = None

    # -- low level -----------------------------------------------------------------------------
    def _api(self, url: str, body: dict | None = None, retries: int = 3,
             method: str | None = None) -> dict | list:
        if urllib.parse.urlsplit(url).hostname != API_HOST:
            raise PipelineError(f"The code does not send the API key to {url}")
        headers = {"apikey": self._key, "Accept": "application/json", "User-Agent": USER_AGENT,
                   "Application-Name": "mwpipe", "Application-Version": __version__}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as err:
                detail = err.read().decode(errors="replace")[:1500]
                if err.code == 429 and attempt + 1 < retries:
                    time.sleep(30 * (attempt + 1))
                    continue
                if err.code == 401:
                    raise ApiError("Nexus rejected the API key (401).", 401) from None
                if err.code == 403:
                    raise ApiError("Nexus refused the request (403): not Premium, or the file is "
                                   f"quarantined or under moderation. {detail}", 403) from None
                raise ApiError(f"Nexus API error {err.code} for {url}: {detail}", err.code) from None
        raise ApiError("The Nexus rate limit is exceeded. Try again later.", 429)

    # -- account ---------------------------------------------------------------------------------
    def user(self) -> dict:
        if self._user is None:
            self._user = self._api(f"{API_V1}/users/validate.json")
        return self._user

    def require_premium(self) -> None:
        user = self.user()
        if not user.get("is_premium"):
            raise PipelineError(f"Nexus account {user.get('name')} is not Premium. The API only "
                                "hands out download links to Premium accounts.")

    # -- collections -------------------------------------------------------------------------
    def collection_archive_url(self, ref: CollectionRef) -> tuple[str, int, str]:
        """Return (archive URL, revision number, collection name)."""
        query = ("query Rev($slug: String!, $revision: Int, $adult: Boolean) {"
                 " collectionRevision(slug: $slug, revision: $revision, viewAdultContent: $adult)"
                 " { revisionNumber downloadLink collection { name } } }")
        variables = {"slug": ref.slug, "revision": ref.revision, "adult": True}
        result = self._api(GRAPHQL_URL, {"query": query, "variables": variables})
        if result.get("errors"):
            raise PipelineError(f"The collection lookup failed: {result['errors'][0].get('message')}")
        rev = result["data"]["collectionRevision"]
        links = self._api(urllib.parse.urljoin(API_ROOT, rev["downloadLink"]))
        links = links.get("download_links") or [links.get("download_link")]
        if not links or not links[0]:
            raise PipelineError("Nexus gave no download link for the collection archive.")
        return links[0]["URI"], rev["revisionNumber"], rev["collection"]["name"]

    def collection_name(self, game: str, collection_id: str) -> str | None:
        """Return the name of one of your collections (by numeric ID), or None if the API does not give it.

        The code tries only the v3 collection resource. The v3 specification has no read endpoint,
        so this can return None. Then mwpipe uses the name that it recorded.
        """
        if not collection_id.strip().isdigit():
            return None
        try:
            data = self._api(f"{API_V3}/collections/{collection_id.strip()}")
        except PipelineError:
            return None
        return ((data.get("data") if isinstance(data, dict) else None) or data or {}).get("name")

    # -- mod files ---------------------------------------------------------------------------
    def file_url(self, game: str, mod_id: int, file_id: int) -> str:
        """Return the signed CDN URL for one mod file (the link expires after four hours)."""
        try:
            links = self._api(f"{API_V3}/games/{game}/mod-file-versions/{file_id}/download-link")["data"]["links"]
            if links:
                return links[0]["url"]  # the user's preferred CDN comes first
            raise ApiError(f"no v3 download links for file {file_id}", 404)
        except ApiError as err:
            if err.status not in (404, 410) and err.status < 500:
                raise  # authentication and permission problems: the v1 endpoint fails in the same way
            log.warning("v3 download-link failed for file %s (%s); trying v1", file_id, err)
        mirrors = self._api(f"{API_V1}/games/{game}/mods/{mod_id}/files/{file_id}/download_link.json")
        if not mirrors:
            raise PipelineError(f"There are no download mirrors for mod {mod_id} file {file_id}.")
        return mirrors[0]["URI"]

    # -- mod lookup (v3) -------------------------------------------------------------------------
    def mod(self, game: str, mod_id: int) -> dict:
        """Mod page data. `main_file_version` is the primary main file that passed the scan. If there is no primary file, it is the newest one."""
        return self._api(f"{API_V3}/games/{game}/mods/{mod_id}")["data"]

    def file_version(self, game: str, file_id: int) -> dict:
        return self._api(f"{API_V3}/games/{game}/mod-file-versions/{file_id}")["data"]

    def mod_files(self, game: str, mod_id: int) -> list[dict]:
        """The files of the mod (v1: file_id, name, category_name, ...). Return [] if the API does not give them."""
        try:
            data = self._api(f"{API_V1}/games/{game}/mods/{mod_id}/files.json")
        except PipelineError:
            return []
        files = data.get("files") if isinstance(data, dict) else None
        return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []

    # -- publishing (v3) -------------------------------------------------------------------------
    def upload_file(self, path: Path, filename: str) -> str:
        """Create a single-part upload session, PUT to the presigned URL, then finalise. Return the upload ID."""
        size = path.stat().st_size
        if size >= MULTIPART_LIMIT:
            raise PipelineError(f"{path.name} is {size >> 20} MiB. The code supports only single-part uploads (smaller than 100 MiB).")
        digest = md5sum(path)
        session = self._api(f"{API_V3}/uploads", {"size_bytes": size, "filename": filename, "md5": digest})["data"]
        put_presigned(session["presigned_url"], path, filename, digest)
        state = self._api(f"{API_V3}/uploads/{session['id']}/finalise", {}, method="POST")["data"]["state"]
        for _ in range(30):
            if state == "available":
                return session["id"]
            time.sleep(2)
            state = self._api(f"{API_V3}/uploads/{session['id']}")["data"]["state"]
        raise PipelineError(f"Upload {session['id']} is still {state!r} after 60 s. Try again later.")

    def create_collection(self, upload_id: str, collection_data: dict, collection_id: str | None = None) -> dict:
        """Create a new collection, or a new revision of `collection_id`. Both start as a draft revision."""
        url = f"{API_V3}/collections" + (f"/{collection_id}/revisions" if collection_id else "")
        return self._api(url, {"upload_id": upload_id, "collection_data": collection_data}, method="POST")["data"]


def put_presigned(url: str, path: Path, filename: str, md5_hex: str) -> None:
    """PUT a file to a presigned storage URL. The code does not send the API key here (this is intentional).

    Content-Disposition and Content-MD5 are part of the URL signature. They must be the same as the
    values that you gave when you created the upload session.
    """
    if urllib.parse.urlsplit(url).scheme != "https":
        raise PipelineError("The code does not use an upload URL that is not HTTPS.")
    quoted = filename.replace("\\", "_").replace('"', "_")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/octet-stream",
               "Content-Length": str(path.stat().st_size),
               "Content-Disposition": f'attachment; filename="{quoted}"',
               "Content-MD5": base64.b64encode(bytes.fromhex(md5_hex)).decode()}
    try:
        with path.open("rb") as fh:
            urllib.request.urlopen(urllib.request.Request(url, data=fh, headers=headers, method="PUT"),
                                   timeout=300).read()
    except urllib.error.HTTPError as err:
        raise PipelineError(f"Upload to storage failed ({err.code}): {err.read().decode(errors='replace')[:300]}") from None


def encode_url(url: str) -> str:
    """Percent-encode the characters (for example, spaces) that CDNs leave raw in file names. Parts that are encoded already stay the same."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(
        path=urllib.parse.quote(parts.path, safe="/%:@+,;=~!$&'()*"),
        query=urllib.parse.quote(parts.query, safe="=&%+:/,;@")))


def download(url: str, dest: Path, md5: str | None = None) -> Path:
    """Stream `url` to `dest` atomically. Verify its MD5 if the MD5 is known.

    The code checks only the MD5, because the collection format does not give the unit of its file sizes.
    """
    if urllib.parse.urlsplit(url).scheme != "https":
        raise PipelineError(f"The code does not use a download that is not HTTPS: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.md5()
    request = urllib.request.Request(encode_url(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp, tmp.open("wb") as out:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, OSError) as err:  # HTTPError is a URLError
        tmp.unlink(missing_ok=True)
        raise PipelineError(f"Download of {dest.name} failed: {err}") from None
    if md5 and digest.hexdigest() != md5.lower():
        tmp.unlink()
        raise PipelineError(f"{dest.name}: MD5 mismatch. The download is corrupted or the file changed upstream")
    os.replace(tmp, dest)
    log.info("downloaded %s", dest.name)
    return dest


def filename_from_url(url: str, fallback: str) -> str:
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return name or fallback
