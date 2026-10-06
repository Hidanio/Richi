"""Bounded, read-only access to the official stable GitHub release feed.

The manifest supplies compatibility information and a checksum, never download
URLs or executable install instructions. HTTPS and GitHub repository ownership
are the trust boundary; checksums detect corruption, not a compromised publisher.
"""

import ast
from email.parser import BytesParser
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import stat
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile
import zlib

from .config import ConfigError


REPOSITORY = "Hidanio/Richi"
PROTOCOL = 1
MANIFEST_FILENAME = "richi-release.json"
MAX_WHEEL_SIZE = 50 * 1024 * 1024
MAX_JSON_SIZE = 1024 * 1024
REQUEST_TIMEOUT = 15
TRANSFER_TIMEOUT = 60
_API = "https://api.github.com/repos/" + REPOSITORY + "/releases/"
_WEB = "https://github.com/" + REPOSITORY + "/releases/"
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z")


class TransientReleaseError(ConfigError):
    """Network failure which can be retried without quarantining a release."""


class _ReleaseNotFound(ConfigError):
    pass


def version_tuple(text):
    """Parse a stable X.Y.Z version; reject tags, prereleases and coercions."""
    if not isinstance(text, str) or _VERSION.fullmatch(text) is None:
        raise ConfigError("Release version must be a stable X.Y.Z version")
    return tuple(int(part) for part in text.split("."))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate release JSON key: " + key)
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ConfigError("Invalid JSON constant: " + value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError("Invalid release JSON: " + str(exc)) from exc


def _keys(raw, expected, label):
    if not isinstance(raw, dict) or set(raw) != set(expected):
        raise ConfigError(label + " has missing or unsupported fields")


def validate_manifest(raw):
    """Return an independent manifest with a supported format and safe values."""
    _keys(raw, ("format_version", "version", "updater_protocol", "python_min",
                "database_schemas", "map_schema", "wheel"), "Release manifest")
    if type(raw["format_version"]) is not int or raw["format_version"] != 1:
        raise ConfigError("Unsupported release manifest format")
    if type(raw["updater_protocol"]) is not int or raw["updater_protocol"] != PROTOCOL:
        raise ConfigError("Release requires an unsupported updater protocol")
    version_tuple(raw["version"])
    minimum = raw["python_min"]
    if (not isinstance(minimum, list) or len(minimum) != 2
            or any(type(part) is not int or not 0 <= part <= 999 for part in minimum)
            or tuple(minimum) < (3, 9)):
        raise ConfigError("Release python_min must be a Python version of at least 3.9")
    schemas = raw["database_schemas"]
    if (not isinstance(schemas, list) or not schemas or len(schemas) > 100
            or any(type(item) is not int or not 1 <= item <= 10000 for item in schemas)
            or sorted(set(schemas)) != schemas):
        raise ConfigError("Release database_schemas must be sorted distinct positive integers")
    if type(raw["map_schema"]) is not int or raw["map_schema"] not in schemas:
        raise ConfigError("Release map_schema must be a supported database schema")
    wheel = raw["wheel"]
    _keys(wheel, ("filename", "sha256", "size"), "Release wheel")
    if wheel["filename"] != "richi-" + raw["version"] + "-py3-none-any.whl":
        raise ConfigError("Release wheel filename does not match its version")
    if not isinstance(wheel["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"]) is None:
        raise ConfigError("Release wheel SHA256 must be 64 lowercase hexadecimal digits")
    if type(wheel["size"]) is not int or not 0 < wheel["size"] <= MAX_WHEEL_SIZE:
        raise ConfigError("Release wheel exceeds the allowed size")
    return dict(raw, python_min=list(minimum), database_schemas=list(schemas), wheel=dict(wheel))


def _allowed_url(url, original, asset):
    """Allow only the original URL and GitHub's two release-asset CDN hosts."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            return False
    except (ValueError, TypeError):
        return False
    if url == original:
        return True
    if not asset:
        return False
    return (parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            and re.match(r"^/github-production-release-asset(?:-[a-z0-9]+)?/", parsed.path) is not None)


class _ReleaseRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def __init__(self, original, asset):
        self.original = original
        self.asset = asset

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl, self.original, self.asset):
            raise ConfigError("Release download redirected outside the official GitHub hosts")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _stream_response(url, limit, publish, asset=False):
    request = Request(url, headers={"Accept": "application/octet-stream" if asset else
                                   "application/vnd.github+json",
                                   "User-Agent": "Richi-updater/1",
                                   "X-GitHub-Api-Version": "2022-11-28"})
    try:
        opener = build_opener(_ReleaseRedirects(url, asset))
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if not _allowed_url(response.geturl(), url, asset):
                raise ConfigError("Unexpected release response URL")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if not 0 <= int(length) <= limit:
                        raise ConfigError("Release response exceeds the allowed size")
                except ValueError as exc:
                    raise ConfigError("Invalid release response Content-Length") from exc
            size = 0
            while True:
                # read1 returns after one socket read, so a slow stream cannot
                # keep a large read alive indefinitely by dripping bytes.
                block = response.read1(min(65536, limit - size + 1))
                if not block:
                    return size
                size += len(block)
                if size > limit:
                    raise ConfigError("Release response exceeds the allowed size")
                if not publish(block):
                    return size
    except HTTPError as exc:
        if exc.code == 404:
            raise _ReleaseNotFound("No published stable Richi release was found") from exc
        error_type = TransientReleaseError if exc.code in {408, 429} or 500 <= exc.code <= 599 else ConfigError
        raise error_type("GitHub release request failed (HTTP " + str(exc.code) + ")") from exc
    except (URLError, OSError, HTTPException) as exc:
        raise TransientReleaseError("Cannot download the official Richi release: " + str(exc)) from exc
    except ValueError as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Cannot download the official Richi release: " + str(exc)) from exc


def _transfer(url, limit, write, asset=False):
    # urllib socket timeouts measure inactivity, including during HTTP headers.
    # Keep all caller/file writes in this thread and bound the entire network
    # operation even if a remote peer drips protocol bytes without going idle.
    # A daemon can finish/close its response after cancellation; it cannot write
    # to the destination or keep the CLI alive. Its queue holds at most 128 KiB.
    messages = queue.Queue(maxsize=2)
    stopped = threading.Event()
    deadline = time.monotonic() + TRANSFER_TIMEOUT

    def publish(kind, value):
        while not stopped.is_set():
            try:
                messages.put((kind, value), timeout=0.05)
                return True
            except queue.Full:
                pass
        return False

    def download():
        try:
            size = _stream_response(url, limit, lambda block: publish("data", block), asset)
            publish("done", size)
        except Exception as exc:
            publish("error", exc)

    worker = threading.Thread(target=download, name="richi-release-download", daemon=True)
    worker.start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransientReleaseError("Release download exceeded its time limit")
            try:
                kind, value = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TransientReleaseError("Release download exceeded its time limit") from exc
            if kind == "data":
                write(value)
            elif kind == "done":
                return value
            else:
                if isinstance(value, ConfigError):
                    raise value
                raise ConfigError("Cannot download the official Richi release: " + str(value)) from value
    finally:
        stopped.set()


def _read_json(url, asset=False):
    blocks = []
    _transfer(url, MAX_JSON_SIZE, blocks.append, asset=asset)
    return _json(b"".join(blocks))


def _asset(assets, name, version):
    if not isinstance(assets, list):
        raise ConfigError("Release assets must be a list")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ConfigError("Release must contain exactly one " + name + " asset")
    asset = matches[0]
    url = _WEB + "download/v" + version + "/" + name
    if (asset.get("state") != "uploaded" or asset.get("browser_download_url") != url
            or type(asset.get("size")) is not int or asset["size"] <= 0):
        raise ConfigError("Release asset is incomplete or has an unexpected download URL")
    return asset


def fetch_release(version=None):
    """Fetch a stable release, or None if the latest-release endpoint has none."""
    if version is not None:
        version_tuple(version)
    try:
        raw = _read_json(_API + ("latest" if version is None else "tags/v" + version))
    except _ReleaseNotFound:
        if version is None:
            return None
        raise
    if not isinstance(raw, dict) or raw.get("draft") is not False or raw.get("prerelease") is not False:
        raise ConfigError("Only published stable Richi releases can be installed")
    tag = raw.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ConfigError("Release tag must be vX.Y.Z")
    released = tag[1:]
    version_tuple(released)
    if version is not None and version != released:
        raise ConfigError("GitHub release version does not match the requested version")
    html_url = _WEB + "tag/" + tag
    if raw.get("html_url") != html_url:
        raise ConfigError("Release page is outside the official Richi repository")
    metadata = _asset(raw.get("assets"), MANIFEST_FILENAME, released)
    if metadata["size"] > MAX_JSON_SIZE:
        raise ConfigError("Release manifest exceeds the allowed size")
    manifest = validate_manifest(_read_json(metadata["browser_download_url"], asset=True))
    if manifest["version"] != released:
        raise ConfigError("Release manifest version does not match its tag")
    wheel = _asset(raw.get("assets"), manifest["wheel"]["filename"], released)
    if wheel["size"] != manifest["wheel"]["size"]:
        raise ConfigError("Release wheel size disagrees with the manifest")
    return {"version": released, "tag": tag, "html_url": html_url,
            "manifest": manifest, "wheel_url": wheel["browser_download_url"]}


def _one_header(metadata, key, expected):
    if metadata.get_all(key) != [expected]:
        raise ConfigError("Wheel " + key + " does not match the release manifest")


def _source_version(source):
    try:
        tree = ast.parse(source)
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "__version__"
                          for target in node.targets)]
        if len(values) != 1:
            raise ValueError("expected one literal __version__ assignment")
        version_tuple(values[0])
        return values[0]
    except (SyntaxError, ValueError, TypeError, LookupError, RecursionError) as exc:
        raise ConfigError("Invalid Richi source version: " + str(exc)) from exc


def verify_wheel(path, manifest):
    """Verify bytes, package identity and extraction safety without executing code."""
    manifest = validate_manifest(manifest)
    try:
        path = Path(path)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                size += len(block)
                if size > MAX_WHEEL_SIZE:
                    raise ConfigError("Release wheel exceeds the allowed size")
                digest.update(block)
        if size != manifest["wheel"]["size"] or digest.hexdigest() != manifest["wheel"]["sha256"]:
            raise ConfigError("Release wheel failed SHA256/size verification")
        version = manifest["version"]
        info_dir = "richi-" + version + ".dist-info"
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(item.file_size for item in entries) > 200 * 1024 * 1024:
                raise ConfigError("Release wheel expands beyond the allowed size")
            seen = set()
            for item in entries:
                name = item.filename
                parts = PurePosixPath(name).parts
                mode = stat.S_IFMT(item.external_attr >> 16)
                if (not name or name.startswith("/") or "\\" in name or ":" in name
                        or any(ord(char) < 32 for char in name)
                        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                        or not parts or (parts[0] not in {"richi", "richi_launcher", info_dir}
                                         and name != "richi_bootstrap.py")
                        or name.casefold() in seen or item.flag_bits & 1
                        or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or item.file_size > MAX_WHEEL_SIZE):
                    raise ConfigError("Unsafe or unexpected path in release wheel: " + name)
                seen.add(name.casefold())
            def read_small(name):
                if archive.getinfo(name).file_size > MAX_JSON_SIZE:
                    raise ConfigError("Wheel metadata exceeds the allowed size")
                return archive.read(name)
            metadata = BytesParser().parsebytes(read_small(info_dir + "/METADATA"))
            _one_header(metadata, "Name", "richi")
            _one_header(metadata, "Version", version)
            _one_header(metadata, "Requires-Python", ">=%d.%d" % tuple(manifest["python_min"]))
            if metadata.get_all("Requires-Dist"):
                raise ConfigError("Release wheel must have no runtime dependencies")
            wheel = BytesParser().parsebytes(read_small(info_dir + "/WHEEL"))
            _one_header(wheel, "Wheel-Version", "1.0")
            _one_header(wheel, "Root-Is-Purelib", "true")
            _one_header(wheel, "Tag", "py3-none-any")
            archive.getinfo(info_dir + "/RECORD")
            if _source_version(read_small("richi/__init__.py")) != version:
                raise ConfigError("Wheel source version disagrees with the release manifest")
            archive.getinfo("richi_launcher/cli.py")
            read_small("richi_bootstrap.py")
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Invalid Richi release wheel: " + str(exc)) from exc
    return path


def download_wheel(release, destination):
    """Download into a directory and expose the final wheel only after verification."""
    if not isinstance(release, dict):
        raise ConfigError("Release must be an object")
    manifest = validate_manifest(release.get("manifest"))
    version = manifest["version"]
    expected = _WEB + "download/v" + version + "/" + manifest["wheel"]["filename"]
    if (release.get("version") != version or release.get("tag") != "v" + version
            or release.get("wheel_url") != expected
            or release.get("html_url") != _WEB + "tag/v" + version):
        raise ConfigError("Release download must use the official versioned GitHub asset")
    temporary = None
    try:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = destination / manifest["wheel"]["filename"]
        fd, temporary = tempfile.mkstemp(prefix=".richi-download-", dir=str(destination))
        with os.fdopen(fd, "wb") as stream:
            _transfer(expected, manifest["wheel"]["size"], stream.write, asset=True)
            stream.flush()
            os.fsync(stream.fileno())
        verify_wheel(temporary, manifest)
        os.replace(temporary, target)
        temporary = None
        return target
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Cannot stage release wheel: " + str(exc)) from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
