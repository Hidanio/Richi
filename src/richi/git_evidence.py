"""Bounded, local Git evidence; never switches or writes a user's checkout.

Git records bytes and ancestry, not the truth of a knowledge claim or deployment.
Working-tree observations keep immutable bytes alongside the memory database.
"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import stat
import subprocess
import time


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_GIT_OUTPUT = MAX_FILE_BYTES + 65536
MAX_DIFF_BYTES = 1024 * 1024
TIMEOUT = 15
_OPERATION_DEADLINE = ContextVar("git_operation_deadline", default=None)
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
NOTICE = ("Content and ancestry describe local code evidence only. Changed code does not "
          "invalidate a knowledge claim; unchanged bytes do not establish deployment freshness.")


class GitEvidenceError(ValueError):
    pass


@contextmanager
def operation_deadline(deadline):
    """Bound nested Git subprocesses without changing normal callers or threads."""
    prior = _OPERATION_DEADLINE.get()
    token = _OPERATION_DEADLINE.set(min(prior, deadline) if prior is not None else deadline)
    try:
        yield
    finally:
        _OPERATION_DEADLINE.reset(token)


def _text(value, name, maximum=4096):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise GitEvidenceError("Invalid " + name)
    return value


def _path(value):
    _text(value, "repository-relative path")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or value != path.as_posix()
            or any(part in (".", "..") or part.casefold() == ".git" for part in path.parts)
            or not path.parts):
        raise GitEvidenceError("Use a normalized relative file path within the repository")
    return value


def validate_git(anchor):
    """Pure validation of the versioned nested source.git contract."""
    required = {"version", "repo_id", "path", "commit", "blob", "mode", "dirty"}
    if not isinstance(anchor, dict) or not required <= set(anchor) or set(anchor) - required - {"snapshot"}:
        raise GitEvidenceError("Invalid Git anchor fields")
    if type(anchor["version"]) is not int or anchor["version"] != 1:
        raise GitEvidenceError("Unsupported Git anchor version")
    _text(anchor["repo_id"], "repository ID", 256)
    _path(anchor["path"])
    if not isinstance(anchor["commit"], str) or not OID.fullmatch(anchor["commit"]):
        raise GitEvidenceError("Git anchor requires a full lowercase commit OID")
    if anchor["blob"] is not None and (not isinstance(anchor["blob"], str)
            or not OID.fullmatch(anchor["blob"]) or len(anchor["blob"]) != len(anchor["commit"])):
        raise GitEvidenceError("Invalid Git blob OID")
    if anchor["mode"] not in ("commit", "worktree") or type(anchor["dirty"]) is not bool:
        raise GitEvidenceError("Invalid Git observation mode or dirty flag")
    if anchor["mode"] == "commit":
        if anchor["dirty"] or anchor["blob"] is None or "snapshot" in anchor:
            raise GitEvidenceError("Commit evidence must have a clean blob and no working-tree snapshot")
    else:
        snapshot = anchor.get("snapshot")
        if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}\.blob", snapshot):
            raise GitEvidenceError("Working-tree evidence requires a content-addressed snapshot")
        if anchor["blob"] is None and not anchor["dirty"]:
            raise GitEvidenceError("An untracked working-tree observation cannot be clean")
    return anchor


def validate_anchor(source):
    if not isinstance(source, dict):
        raise GitEvidenceError("Source must be an object")
    anchor = validate_git(source.get("git"))
    if source.get("type") != "code" or source.get("revision") != "git:" + anchor["commit"]:
        raise GitEvidenceError("Git source type/revision must agree with its anchor")
    if not isinstance(source.get("sha256"), str) or not SHA256.fullmatch(source["sha256"]):
        raise GitEvidenceError("Git source requires a SHA-256 content hash")
    if anchor["mode"] == "worktree" and anchor["snapshot"] != source["sha256"] + ".blob":
        raise GitEvidenceError("Snapshot ID must agree with the observed content hash")
    _text(source.get("reference"), "source reference")
    _text(source.get("observed_at"), "source observation time", 128)
    return source


def _git(cwd, args, *, allowed=(0,), maximum=MAX_GIT_OUTPUT):
    deadline = time.monotonic() + TIMEOUT
    outer = _OPERATION_DEADLINE.get()
    if outer is not None:
        deadline = min(deadline, outer)
    if time.monotonic() >= deadline:
        raise GitEvidenceError("Local Git operation exceeded the time limit")
    # Inherited GIT_DIR/config/alternate object settings must not redirect reads.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_PROTOCOL_FROM_USER": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    command = ["git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never",
               "-c", "submodule.recurse=false", "-c", "diff.external=",
               "-c", "diff.renamelimit=1000"] + list(args)
    try:
        process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except (OSError, ValueError) as exc:
        raise GitEvidenceError("Cannot run local Git: " + str(exc)) from exc
    chunks = {"stdout": [], "stderr": []}
    total = 0
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitEvidenceError("Local Git operation exceeded the time limit")
            for key, _ in selector.select(min(remaining, 0.2)):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                total += len(data)
                if total > maximum:
                    raise GitEvidenceError("Local Git output exceeded the size limit")
                chunks[key.data].append(data)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitEvidenceError("Local Git operation exceeded the time limit")
        process.wait(timeout=remaining)
    except BaseException as exc:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise GitEvidenceError("Local Git operation exceeded the time limit") from exc
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    output, error = b"".join(chunks["stdout"]), b"".join(chunks["stderr"])
    if process.returncode not in allowed:
        reason = error.decode("utf-8", "replace").strip()[:500]
        raise GitEvidenceError("Local Git read failed" + (": " + reason if reason else ""))
    return output, process.returncode


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitEvidenceError("Git path/metadata is not valid UTF-8") from exc


def _resolve(root, revision):
    _text(revision, "Git revision", 1024)
    if revision.startswith("-"):
        raise GitEvidenceError("Git revision cannot start with '-'")
    output, _ = _git(root, ["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], maximum=8192)
    commit = _decode(output).strip()
    if not OID.fullmatch(commit):
        raise GitEvidenceError("Git did not resolve the revision to a full commit OID")
    return commit


def identify(repo):
    """Resolve a non-bare checkout and its shared identity without remote access."""
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise GitEvidenceError("Repository directory is unavailable")
    root = Path(_decode(_git(path, ["rev-parse", "--show-toplevel"], maximum=16384)[0]).strip()).resolve()
    git_dir = Path(_decode(_git(root, ["rev-parse", "--absolute-git-dir"], maximum=16384)[0]).strip()).resolve()
    common = Path(_decode(_git(root, ["rev-parse", "--git-common-dir"], maximum=16384)[0]).strip())
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    partial, _ = _git(root, ["config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$"],
                      allowed=(0, 1), maximum=65536)
    if partial or any((common / "objects" / "pack").glob("*.promisor")):
        raise GitEvidenceError("Partial/promisor repositories are not supported: local reads must never fetch")
    shallow = _git(root, ["rev-parse", "--is-shallow-repository"], maximum=8192)[0].strip() == b"true"
    try:
        head = _resolve(root, "HEAD")
    except GitEvidenceError:
        head = None
    fmt = _decode(_git(root, ["rev-parse", "--show-object-format"], maximum=8192)[0]).strip()
    if fmt not in ("sha1", "sha256"):
        fmt = "sha256" if head and len(head) == 64 else "sha1"
    return {"root": str(root), "git_dir": str(git_dir), "common_dir": str(common),
            "head": head, "shallow": shallow, "object_format": fmt}


def _blob(root, commit, path):
    parents = [parent.as_posix() for parent in PurePosixPath(path).parents if parent.as_posix() != "."]
    if parents:
        ancestors, _ = _git(root, ["ls-tree", "-z", commit, "--"] + parents, maximum=65536)
        for record in ancestors.split(b"\0"):
            if record and record.split(b" ", 1)[0] in (b"120000", b"160000"):
                raise GitEvidenceError("Paths inside symlinks or submodules are excluded")
    output, _ = _git(root, ["ls-tree", "-z", commit, "--", path], maximum=16384)
    records = [record for record in output.split(b"\0") if record]
    if not records:
        return None
    if len(records) != 1:
        raise GitEvidenceError("Git path did not select exactly one file")
    metadata, filename = records[0].split(b"\t", 1)
    mode, kind, oid = _decode(metadata).split(" ")
    if _decode(filename) != path or mode not in ("100644", "100755") or kind != "blob":
        raise GitEvidenceError("Only regular files are supported; directories, symlinks and submodules are excluded")
    if not OID.fullmatch(oid):
        raise GitEvidenceError("Invalid Git blob OID")
    return oid


def _blob_bytes(root, oid):
    size = _git(root, ["cat-file", "-s", oid], maximum=8192)[0].strip()
    if not size.isdigit() or int(size) > MAX_FILE_BYTES:
        raise GitEvidenceError("Git file exceeds the observation size limit")
    data = _git(root, ["cat-file", "blob", oid], maximum=MAX_FILE_BYTES + 1024)[0]
    if len(data) != int(size):
        raise GitEvidenceError("Git blob size changed while reading")
    return data


def _file_bytes(root, path):
    """Open each component relative to its parent with O_NOFOLLOW, avoiding escapes."""
    parts = PurePosixPath(_path(path)).parts
    descriptors = []
    try:
        current = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise GitEvidenceError("Source must be a regular file within the size limit")
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(file_fd, min(65536, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(file_fd)
        if len(data) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise GitEvidenceError("Source changed while reading or exceeds the size limit")
        return bytes(data)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GitEvidenceError("Cannot safely read the regular source file: " + str(exc)) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _artifact_root(artifact_dir, *, create=False):
    if artifact_dir is None:
        raise GitEvidenceError("An artifact directory is required for working-tree evidence")
    path = Path(artifact_dir).expanduser().absolute()
    # Resolve is deliberately compared with lexical form so a redirected artifact
    # directory cannot cause a snapshot read/write outside the selected store.
    if path.resolve() != path:
        raise GitEvidenceError("Artifact directory must not traverse symlinks")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise GitEvidenceError("Artifact directory is unavailable")
    return path


def _save_artifact(data, artifact_dir):
    root = _artifact_root(artifact_dir, create=True)
    name = hashlib.sha256(data).hexdigest() + ".blob"
    path = root / name
    pending = root / (".pending-" + secrets.token_hex(16))
    descriptor = os.open(str(pending), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(pending), str(path), follow_symlinks=False)
        except FileExistsError:
            existing = _file_bytes(root, name)
            if existing != data:
                raise GitEvidenceError("Existing immutable snapshot is corrupt")
    finally:
        pending.unlink(missing_ok=True)
    return name


def _snapshot(source, artifact_dir):
    root = _artifact_root(artifact_dir)
    data = _file_bytes(root, source["git"]["snapshot"])
    if data is None:
        raise GitEvidenceError("Observed working-tree snapshot is unavailable")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise GitEvidenceError("Observed working-tree snapshot hash does not match its source")
    return data


def _observed(source, identity, artifact_dir):
    anchor = source["git"]
    commit = _resolve(identity["root"], anchor["commit"])
    oid = _blob(identity["root"], commit, anchor["path"])
    if oid != anchor["blob"]:
        raise GitEvidenceError("Recorded commit/path/blob do not agree")
    baseline = _blob_bytes(identity["root"], oid) if oid else None
    if anchor["mode"] == "worktree":
        data = _snapshot(source, artifact_dir)
        if anchor["dirty"] != (data != baseline):
            raise GitEvidenceError("Recorded dirty flag does not agree with the snapshot and base commit")
        return data
    data = baseline
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise GitEvidenceError("Recorded content hash does not agree with Git evidence")
    return data


def capture(repo, repo_id, path, revision="HEAD", worktree=False, artifact_dir=None):
    path = _path(path)
    _text(repo_id, "repository ID", 256)
    if type(worktree) is not bool:
        raise GitEvidenceError("worktree must be a boolean")
    identity = identify(repo)
    root = identity["root"]
    commit = _resolve(root, revision)
    if worktree and commit != _resolve(root, "HEAD"):
        raise GitEvidenceError("Working-tree capture must use the current HEAD as its base")
    oid = _blob(root, commit, path)
    baseline = _blob_bytes(root, oid) if oid else None
    data = _file_bytes(root, path) if worktree else baseline
    if data is None:
        raise GitEvidenceError("Selected file is unavailable in the chosen observation mode")
    if worktree and _resolve(root, "HEAD") != commit:
        raise GitEvidenceError("HEAD changed during the working-tree observation; retry capture")
    anchor = {"version": 1, "repo_id": repo_id, "path": path, "commit": commit,
              "blob": oid, "mode": "worktree" if worktree else "commit",
              "dirty": data != baseline if worktree else False}
    if worktree:
        anchor["snapshot"] = _save_artifact(data, artifact_dir)
    source = {"reference": str(Path(root) / path), "type": "code", "revision": "git:" + commit,
              "sha256": hashlib.sha256(data).hexdigest(),
              "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "git": anchor}
    return validate_anchor(source)


def _ancestry(identity, captured, target):
    root = identity["root"]
    if captured == target:
        return "same"
    try:
        _resolve(root, captured)
        if _git(root, ["merge-base", "--is-ancestor", captured, target], allowed=(0, 1), maximum=8192)[1] == 0:
            return "ahead"
        if _git(root, ["merge-base", "--is-ancestor", target, captured], allowed=(0, 1), maximum=8192)[1] == 0:
            return "behind"
        return "unknown" if identity["shallow"] else "diverged"
    except GitEvidenceError:
        return "unknown"


def _renames(root, source_commit, target_commit, path):
    if source_commit == target_commit:
        return []
    try:
        raw, _ = _git(root, ["diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z",
                              "-M50%", "-l1000", source_commit, target_commit, "--"], maximum=512 * 1024)
        fields, candidates, index = raw.split(b"\0"), [], 0
        while index < len(fields) and fields[index]:
            kind = fields[index].decode("ascii")
            if kind.startswith(("R", "C")):
                old, new = _decode(fields[index + 1]), _decode(fields[index + 2])
                if kind.startswith("R") and old == path:
                    candidates.append({"path": new, "similarity": int(kind[1:])})
                index += 3
            else:
                index += 2
        return candidates
    except (GitEvidenceError, UnicodeDecodeError, IndexError, ValueError):
        return []


def _target(identity, anchor, target, worktree):
    root = identity["root"]
    commit = _resolve(root, target)
    if worktree and commit != _resolve(root, "HEAD"):
        raise GitEvidenceError("Working-tree comparison must use the current HEAD as its base")
    oid = _blob(root, commit, anchor["path"])
    data = _file_bytes(root, anchor["path"]) if worktree else (_blob_bytes(root, oid) if oid else None)
    metadata = {"commit": commit, "mode": "worktree" if worktree else "commit", "path": anchor["path"],
                "blob": oid, "sha256": hashlib.sha256(data).hexdigest() if data is not None else None}
    if worktree:
        committed = _blob_bytes(root, oid) if oid else None
        metadata["dirty"] = data != committed
    return metadata, data


def check(source, repo, target="HEAD", worktree=False, artifact_dir=None):
    validate_anchor(source)
    anchor = source["git"]
    result = {"status": "unavailable", "ancestry": "unknown", "source": dict(anchor), "notice": NOTICE}
    try:
        identity = identify(repo)
        observed = _observed(source, identity, artifact_dir)
        target_meta, data = _target(identity, anchor, target, worktree)
        result.update({"target": target_meta, "ancestry": _ancestry(identity, anchor["commit"], target_meta["commit"]),
                       "status": "deleted" if data is None else "unchanged" if data == observed else "changed",
                       "observation_available": True, "shallow": identity["shallow"]})
        if data is None and not worktree:
            result["rename_candidates"] = _renames(identity["root"], anchor["commit"], target_meta["commit"], anchor["path"])
            result["rename_detection"] = "best_effort; candidates are not automatically followed"
    except (GitEvidenceError, OSError) as exc:
        result["reason"] = str(exc)
    return result


def show(source, repo, artifact_dir=None, max_bytes=65536):
    validate_anchor(source)
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FILE_BYTES:
        raise GitEvidenceError("max_bytes must be between 1 and the file size limit")
    data = _observed(source, identify(repo), artifact_dir)
    sample = data[:max_bytes]
    try:
        content, encoding = sample.decode("utf-8"), "utf-8"
        if b"\0" in sample:
            raise UnicodeDecodeError("utf-8", sample, 0, 1, "binary null")
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(sample).decode("ascii"), "base64"
    return {"source": dict(source["git"]), "sha256": source["sha256"], "bytes": len(data),
            "content": content, "encoding": encoding, "truncated": len(sample) < len(data), "notice": NOTICE}


def history(source, repo, limit=20, target=None):
    validate_anchor(source)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise GitEvidenceError("History limit must be between 1 and 100")
    identity = identify(repo)
    commit = _resolve(identity["root"], target or source["git"]["commit"])
    raw, _ = _git(identity["root"], ["log", "--no-show-signature", "--follow", "--format=%H%x00%P%x00%aI%x00%s%x00",
                                        "--max-count=" + str(limit + 1), commit, "--", source["git"]["path"]],
                  maximum=1024 * 1024)
    fields = raw.split(b"\0")
    commits = []
    for index in range(0, len(fields) - 1, 4):
        if index + 3 >= len(fields):
            raise GitEvidenceError("Unexpected Git history format")
        oid = _decode(fields[index]).strip()
        if not OID.fullmatch(oid):
            raise GitEvidenceError("Unexpected Git history commit")
        commits.append({"commit": oid, "parents": _decode(fields[index + 1]).split(),
                        "authored_at": _decode(fields[index + 2]), "subject": _decode(fields[index + 3])[:2000]})
    return {"source": dict(source["git"]), "target": commit, "commits": commits[:limit],
            "truncated": len(commits) > limit, "shallow": identity["shallow"],
            "follow_renames": "backward Git --follow heuristic", "notice": NOTICE}


def diff(source, repo, target="HEAD", worktree=False, artifact_dir=None, context=3, max_chars=32000):
    validate_anchor(source)
    if type(context) is not int or not 0 <= context <= 20:
        raise GitEvidenceError("Diff context must be between 0 and 20")
    if type(max_chars) is not int or not 100 <= max_chars <= 1000000:
        raise GitEvidenceError("Diff max_chars must be between 100 and 1000000")
    identity = identify(repo)
    original = _observed(source, identity, artifact_dir)
    target_meta, current = _target(identity, source["git"], target, worktree)
    status = "deleted" if current is None else "unchanged" if current == original else "changed"
    current = b"" if current is None else current
    result = {"source": dict(source["git"]), "target": target_meta, "status": status,
              "ancestry": _ancestry(identity, source["git"]["commit"], target_meta["commit"]), "notice": NOTICE}
    if len(original) > MAX_DIFF_BYTES or len(current) > MAX_DIFF_BYTES:
        raise GitEvidenceError("Selected bytes exceed the bounded text-diff limit; use show/check")
    try:
        if b"\0" in original or b"\0" in current:
            raise UnicodeDecodeError("utf-8", b"\0", 0, 1, "binary null")
        before, after = original.decode("utf-8").splitlines(keepends=True), current.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        result.update({"binary": True, "patch": None, "truncated": False})
        return result
    if len(before) > 20000 or len(after) > 20000:
        raise GitEvidenceError("Selected text exceeds the bounded diff line limit")
    patch, length, truncated = [], 0, False
    for line in difflib.unified_diff(before, after, fromfile="observed/" + source["git"]["path"],
                                     tofile="target/" + source["git"]["path"], n=context):
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        remaining = max_chars - length
        if len(line) > remaining:
            patch.append(line[:remaining])
            truncated = True
            break
        patch.append(line)
        length += len(line)
    result.update({"binary": False, "patch": "".join(patch), "truncated": truncated})
    return result
