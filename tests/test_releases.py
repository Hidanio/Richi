"""Synthetic release metadata and HTTP responses; no live feed or user state."""

import hashlib
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request
import zipfile

from richi_launcher.config import ConfigError
from richi_launcher import releases


VERSION = "1.2.3"
BASE = "https://github.com/Hidanio/Richi/releases/"


def wheel_bytes(version=VERSION, extra=None, metadata=None):
    output = io.BytesIO()
    info = "richi-" + VERSION + ".dist-info/"
    files = {
        "richi/__init__.py": '__version__ = "' + version + '"\n',
        "richi_launcher/cli.py": "def main(): pass\n",
        "richi_bootstrap.py": "def main(): pass\n",
        info + "METADATA": metadata or ("Metadata-Version: 2.4\nName: richi\nVersion: " + VERSION
                                         + "\nRequires-Python: >=3.9\n\n"),
        info + "WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        info + "RECORD": "",
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if extra:
            for name, content in extra:
                archive.writestr(name, content)
    return output.getvalue()


def manifest(data=None):
    data = wheel_bytes() if data is None else data
    return {"format_version": 1, "version": VERSION, "updater_protocol": 1,
            "python_min": [3, 9], "database_schemas": [1, 2], "map_schema": 2,
            "wheel": {"filename": "richi-1.2.3-py3-none-any.whl",
                      "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}


def release_record(data=None):
    record = manifest(data)
    return {"version": VERSION, "tag": "v" + VERSION, "html_url": BASE + "tag/v" + VERSION,
            "manifest": record, "wheel_url": BASE + "download/v" + VERSION + "/" + record["wheel"]["filename"]}


def feed(record=None):
    record = release_record() if record is None else record
    return {"draft": False, "prerelease": False, "tag_name": record["tag"],
            "html_url": record["html_url"], "assets": [
                {"name": releases.MANIFEST_FILENAME, "state": "uploaded", "size": 500,
                 "browser_download_url": BASE + "download/v" + VERSION + "/" + releases.MANIFEST_FILENAME},
                {"name": record["manifest"]["wheel"]["filename"], "state": "uploaded",
                 "size": record["manifest"]["wheel"]["size"], "browser_download_url": record["wheel_url"]}]}


class Response(io.BytesIO):
    def __init__(self, data, url, length=True):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data))} if length else {}

    def geturl(self):
        return self.url


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="richi-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def open_responses(self, mapping):
        def open_request(request, timeout):
            self.assertEqual(timeout, releases.REQUEST_TIMEOUT)
            self.assertTrue(request.full_url.startswith("https://"))
            data = mapping[request.full_url]
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
            return Response(data, request.full_url)
        opener = mock.Mock()
        opener.open.side_effect = open_request
        return mock.patch.object(releases, "build_opener", return_value=opener)

    def test_versions_are_strict_stable_and_order_numerically(self):
        self.assertGreater(releases.version_tuple("1.10.0"), releases.version_tuple("1.9.9"))
        for value in (None, 123, "v1.2.3", "01.2.3", "1.2", "1.2.3rc1", "1.2.3+build", "1.2.3\n"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                releases.version_tuple(value)

    def test_manifest_is_copied_and_validated(self):
        raw = manifest()
        result = releases.validate_manifest(raw)
        result["wheel"]["size"] = 1
        result["database_schemas"].append(3)
        self.assertEqual(raw, manifest())
        for changes in ({"format_version": True}, {"updater_protocol": 2}, {"python_min": [3, 8]},
                        {"python_min": [3, True]}, {"database_schemas": [2, 1]},
                        {"database_schemas": [1, 1]}, {"map_schema": 3}, {"url": "https://evil.invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                releases.validate_manifest(dict(raw, **changes))
        for changes in ({"filename": "../../richi.whl"}, {"sha256": "a"},
                        {"size": True}, {"size": releases.MAX_WHEEL_SIZE + 1}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                releases.validate_manifest(dict(raw, wheel=dict(raw["wheel"], **changes)))

    def test_fetch_latest_and_explicit_official_release(self):
        for suffix, version in (("latest", None), ("tags/v1.2.3", VERSION)):
            record = release_record()
            mapping = {releases._API + suffix: feed(record),
                       BASE + "download/v1.2.3/" + releases.MANIFEST_FILENAME: record["manifest"]}
            with self.subTest(version=version), self.open_responses(mapping):
                self.assertEqual(releases.fetch_release(version), record)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_draft_prerelease_wrong_tag_and_wrong_repository_are_rejected(self):
        for changes in ({"draft": True}, {"prerelease": True}, {"draft": 0},
                        {"tag_name": "v1.2.3rc1"}, {"tag_name": "v1.2.4"},
                        {"html_url": "https://github.com/elsewhere/Richi/releases/tag/v1.2.3"}):
            with self.subTest(changes=changes), self.open_responses({releases._API + "tags/v1.2.3": dict(feed(), **changes)}):
                with self.assertRaises(ConfigError):
                    releases.fetch_release(VERSION)

    def test_missing_duplicate_external_and_incomplete_assets_are_rejected(self):
        good = feed()
        cases = [[], [good["assets"][0], good["assets"][0]],
                 [dict(good["assets"][0], browser_download_url="https://example.com/release.json")],
                 [dict(good["assets"][0], state="new")]]
        for assets in cases:
            with self.subTest(assets=assets), self.open_responses({releases._API + "latest": dict(good, assets=assets)}):
                with self.assertRaises(ConfigError):
                    releases.fetch_release()

    def test_manifest_and_asset_version_size_must_agree(self):
        for bad_manifest, bad_feed in ((dict(manifest(), version="1.2.4"), feed()),
                                       (manifest(), dict(feed(), assets=[feed()["assets"][0],
                                        dict(feed()["assets"][1], size=1)]))):
            mapping = {releases._API + "latest": bad_feed,
                       BASE + "download/v1.2.3/" + releases.MANIFEST_FILENAME: bad_manifest}
            with self.open_responses(mapping), self.assertRaises(ConfigError):
                releases.fetch_release()

    def test_network_errors_become_config_errors(self):
        for error in (URLError("offline"), TimeoutError("timed out"), IncompleteRead(b"part"),
                      HTTPError(releases._API + "latest", 403, "rate limit", {}, None)):
            opener = mock.Mock()
            opener.open.side_effect = error
            with mock.patch.object(releases, "build_opener", return_value=opener), self.assertRaises(ConfigError):
                releases.fetch_release()

    def test_no_latest_release_is_normal_but_unknown_explicit_version_is_an_error(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(releases._API + "latest", 404, "not found", {}, None)
        with mock.patch.object(releases, "build_opener", return_value=opener):
            self.assertIsNone(releases.fetch_release())
            with self.assertRaises(ConfigError):
                releases.fetch_release(VERSION)

    def test_missing_manifest_does_not_look_like_no_release(self):
        opener = mock.Mock()
        opener.open.side_effect = [Response(json.dumps(feed()).encode(), releases._API + "latest"),
                                  HTTPError("manifest", 404, "not found", {}, None)]
        with mock.patch.object(releases, "build_opener", return_value=opener), self.assertRaises(ConfigError):
            releases.fetch_release()

    def test_json_bounds_duplicate_keys_and_invalid_payloads(self):
        for payload in (b'{"draft":false,"draft":true}', b'{"number":NaN}', b'not JSON', b'\xff',
                        b'[' * 2000 + b']' * 2000):
            with self.open_responses({releases._API + "latest": payload}), self.assertRaises(ConfigError):
                releases.fetch_release()
        for length in (True, False):
            opener = mock.Mock()
            opener.open.return_value = Response(b"x" * 10, releases._API + "latest", length=length)
            with mock.patch.object(releases, "build_opener", return_value=opener), self.assertRaises(ConfigError):
                releases._transfer(releases._API + "latest", 5, lambda block: None)

    def test_transfer_deadline_and_final_url_are_enforced(self):
        url = releases._API + "latest"
        with self.open_responses({url: b"data"}), mock.patch.object(
                releases.time, "monotonic", side_effect=[0, releases.TRANSFER_TIMEOUT + 1]):
            with self.assertRaisesRegex(ConfigError, "time limit"):
                releases._transfer(url, 10, lambda block: None)
        opener = mock.Mock()
        opener.open.return_value = Response(b"data", "https://example.com/")
        with mock.patch.object(releases, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ConfigError, "response URL"):
                releases._transfer(url, 10, lambda block: None)

    def test_blocked_connection_and_body_are_bounded_without_late_file_writes(self):
        url = releases._API + "latest"
        for blocked_stage in ("open", "read"):
            unblock = threading.Event()
            finished = threading.Event()
            writes = []

            class SlowResponse(Response):
                def read1(self, size):
                    unblock.wait(timeout=2)
                    finished.set()
                    return super().read1(size)

            def open_request(request, timeout):
                if blocked_stage == "open":
                    unblock.wait(timeout=2)
                    finished.set()
                    return Response(b"data", url)
                return SlowResponse(b"data", url)

            opener = mock.Mock()
            opener.open.side_effect = open_request
            try:
                with self.subTest(stage=blocked_stage), mock.patch.object(
                        releases, "build_opener", return_value=opener), mock.patch.object(
                        releases, "TRANSFER_TIMEOUT", 0.03):
                    started = time.monotonic()
                    with self.assertRaisesRegex(ConfigError, "time limit"):
                        releases._transfer(url, 10, writes.append)
                    self.assertLess(time.monotonic() - started, 0.5)
                    unblock.set()
                    self.assertTrue(finished.wait(timeout=1))
                    self.assertEqual(writes, [])
            finally:
                unblock.set()

    def test_redirects_allow_only_constrained_github_asset_hosts(self):
        original = release_record()["wheel_url"]
        allowed = "https://release-assets.githubusercontent.com/github-production-release-asset/123/file?sig=x"
        handler = releases._ReleaseRedirects(original, True)
        self.assertEqual(handler.redirect_request(Request(original), None, 302, "Found", {}, allowed).full_url, allowed)
        for url in ("http://release-assets.githubusercontent.com/github-production-release-asset/123/file",
                    "https://release-assets.githubusercontent.com.evil.invalid/github-production-release-asset/123/file",
                    "https://user:password@release-assets.githubusercontent.com/github-production-release-asset/123/file",
                    "https://release-assets.githubusercontent.com:444/github-production-release-asset/123/file",
                    "https://github.com/someone/else/releases/download/v1.2.3/a.whl",
                    "https://release-assets.githubusercontent.com/anything", "file:///tmp/wheel"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                handler.redirect_request(Request(original), None, 302, "Found", {}, url)
        with self.assertRaises(ConfigError):
            releases._ReleaseRedirects(releases._API + "latest", False).redirect_request(
                Request(releases._API + "latest"), None, 302, "Found", {}, allowed)

    def test_download_verifies_and_exposes_only_finished_wheel(self):
        data = wheel_bytes()
        record = release_record(data)
        with self.open_responses({record["wheel_url"]: data}):
            path = releases.download_wheel(record, self.root)
        self.assertEqual(path.name, record["manifest"]["wheel"]["filename"])
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_corrupt_or_truncated_download_cleans_temporary_file(self):
        record = release_record()
        for data in (b"broken", wheel_bytes() + b"extra"):
            with self.open_responses({record["wheel_url"]: data}), self.assertRaises(ConfigError):
                releases.download_wheel(record, self.root)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_download_rejects_untrusted_url_before_network_or_writes(self):
        record = dict(release_record(), wheel_url="https://example.com/richi.whl")
        with mock.patch.object(releases, "build_opener") as opener, self.assertRaises(ConfigError):
            releases.download_wheel(record, self.root / "absent")
        opener.assert_not_called()
        self.assertFalse((self.root / "absent").exists())

    def assert_invalid_wheel(self, data):
        path = self.root / "wheel.whl"
        path.write_bytes(data)
        with self.assertRaises(ConfigError):
            releases.verify_wheel(path, manifest(data))

    def test_wheel_rejects_wrong_name_version_source_dependency_and_python(self):
        for metadata in ("Name: other\nVersion: 1.2.3\nRequires-Python: >=3.9\n",
                         "Name: richi\nVersion: 1.2.4\nRequires-Python: >=3.9\n",
                         "Name: richi\nVersion: 1.2.3\nRequires-Python: >=3.10\n",
                         "Name: richi\nVersion: 1.2.3\nRequires-Python: >=3.9\nRequires-Dist: dependency\n",
                         "Name: richi\nVersion: 1.2.3\nVersion: 1.2.4\nRequires-Python: >=3.9\n"):
            with self.subTest(metadata=metadata):
                self.assert_invalid_wheel(wheel_bytes(metadata=metadata))
        self.assert_invalid_wheel(wheel_bytes(version="1.2.4"))
        self.assert_invalid_wheel(b"not a wheel")

    def test_wheel_rejects_traversal_symlinks_duplicate_case_and_extra_distribution(self):
        link = zipfile.ZipInfo("richi/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entry in (("richi/../../outside", "bad"), ("/richi/absolute", "bad"),
                      ("richi\\escape", "bad"), ("richi/./alias", "bad"),
                      ("richi//alias", "bad"), ("another-1.0.dist-info/METADATA", "bad"),
                      ("richi/__INIT__.py", "bad"), (link, "/outside")):
            with self.subTest(entry=str(entry[0])):
                self.assert_invalid_wheel(wheel_bytes(extra=[entry]))

    def test_manifest_builder_agrees_with_source_metadata_and_tag(self):
        script = Path(__file__).resolve().parents[1] / "scripts/release_manifest.py"
        spec = importlib.util.spec_from_file_location("richi_release_manifest_test", script)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        source = self.root / "source"
        (source / "src/richi").mkdir(parents=True)
        (source / "pyproject.toml").write_text('[project]\nname = "richi"\nversion = "1.2.3"\n[other]\n')
        (source / "src/richi/__init__.py").write_text('__version__ = "1.2.3"\n')
        path = self.root / "richi-1.2.3-py3-none-any.whl"
        data = wheel_bytes()
        path.write_bytes(data)
        expected = manifest(data)
        self.assertEqual(builder.build_manifest(path, source, "v1.2.3"), expected)
        self.assertEqual(builder.build_manifest(path, source), expected)
        with self.assertRaises(ConfigError):
            builder.build_manifest(path, source, "v1.2.4")
        (source / "src/richi/__init__.py").write_text('__version__ = "1.2.4"\n')
        with self.assertRaises(ConfigError):
            builder.build_manifest(path, source)


if __name__ == "__main__":
    unittest.main()
