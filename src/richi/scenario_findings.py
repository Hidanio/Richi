"""Grounded review checks (no_dirty_git, findings_grounded) and read-side finding dedup/consensus.

All I/O helpers here are strictly read-only, bounded by file size, total bytes, item count and wall-clock
deadline, and designed to run completely outside SQLite write transactions.
"""
from collections import Counter
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time

from . import git_evidence


SEVERITIES = ("P0", "P1", "P2", "nit")
SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "nit": 3}
MAX_GROUNDED_FINDINGS = 100
MAX_TITLE_CHARS = 300
MIN_TITLE_CHARS = 3
MIN_EVIDENCE_CHARS = 10
MAX_EVIDENCE_CHARS = 4000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_IO_SECONDS = 5.0
MAX_FINGERPRINT_FILES = 200
CLUSTER_LINE_DIAMETER = 12
_CHUNK_BYTES = 64 * 1024

_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_CAMEL_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+")
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{2,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "when", "into", "not", "are", "was", "were",
    "has", "have", "can", "may", "will", "should", "must", "use", "used", "line", "file", "code",
    "check", "return", "true", "false", "none", "null", "def", "class", "function", "const", "let",
    "var", "import", "else", "elif", "try", "except", "catch", "self", "what", "where", "which",
    "или", "для", "при", "как", "что", "это", "все", "без", "под", "над", "если", "нет", "так",
})


def _safe_rel_repo_path(rel_path):
    """Validate a relative POSIX file path inside the repo, rejecting traversal and .git paths."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        return None, "file must be nonempty relative POSIX text"
    clean = rel_path.strip()
    if len(clean) > 500 or "\x00" in clean:
        return None, "invalid file path length or NUL byte"
    if clean.startswith("/") or "\\" in clean or re.match(r"^[A-Za-z]:", clean):
        return None, "file must be a relative POSIX path: " + clean[:80]
    parts = clean.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        return None, "file must not contain empty, '.' or '..' segments: " + clean[:80]
    if any(seg.casefold() == ".git" for seg in parts):
        return None, "file must not target .git internals: " + clean[:80]
    return clean, None


def _open_regular_inside_root(root, rel_path):
    """Safely open a regular file inside root using anchored component-wise dir_fd + O_NOFOLLOW + O_NONBLOCK."""
    resolved_root = Path(root).resolve()
    parts = rel_path.split("/")
    if not parts or any(seg in ("", ".", "..") or seg.casefold() == ".git" for seg in parts):
        return None, "invalid path: " + rel_path
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    cur_fd = None
    try:
        cur_fd = os.open(str(resolved_root), dir_flags)
        for seg in parts[:-1]:
            nxt_fd = os.open(seg, dir_flags | nofollow, dir_fd=cur_fd)
            os.close(cur_fd)
            cur_fd = nxt_fd
        fd = os.open(parts[-1], os.O_RDONLY | nofollow | nonblock, dir_fd=cur_fd)
    except OSError:
        # Distinguish symlink escape vs missing file for clear diagnostics
        try:
            (resolved_root / rel_path).resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            return None, "path escapes project root: " + rel_path
        return None, "file not found or unreadable regular file: " + rel_path
    finally:
        if cur_fd is not None:
            try:
                os.close(cur_fd)
            except OSError:
                pass
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None, "unable to stat file: " + rel_path
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None, "path is not a regular file: " + rel_path
    return (fd, st), None


def _stream_file_hash(fd, hasher, remaining_bytes, deadline):
    """Stream regular file fd into hasher under byte and time caps. Returns bytes_read or None if capped."""
    total = 0
    while True:
        if time.monotonic() > deadline:
            return None
        chunk = os.read(fd, _CHUNK_BYTES)
        if not chunk:
            return total
        total += len(chunk)
        if total > MAX_FILE_BYTES or total > remaining_bytes:
            return None
        hasher.update(chunk)


def compute_worktree_fingerprint(project_path):
    """Compute a 64-byte SHA-256 fingerprint of HEAD, index, status, and dirty/untracked regular files.

    Runs read-only git commands and bounded O_NOFOLLOW file reads with a 5s deadline.
    """
    if project_path is None:
        return {"fingerprint": None, "is_clean": None, "head": None, "error": "requires --project-path"}
    root = Path(project_path).resolve()
    deadline = time.monotonic() + MAX_IO_SECONDS
    h = hashlib.sha256()
    try:
        with git_evidence.operation_deadline(deadline):
            top_out, top_rc = git_evidence._git(root, ["rev-parse", "--show-toplevel"], allowed=(0, 1))
            if top_rc == 0 and top_out.strip():
                top_str = top_out.strip().decode("utf-8", errors="surrogateescape") if isinstance(top_out, bytes) else top_out.strip()
                git_root = Path(top_str).resolve()
            else:
                git_root = root
            head_out, head_rc = git_evidence._git(root, ["rev-parse", "--verify", "--quiet", "HEAD"], allowed=(0, 1))
            head_b = head_out.strip() if head_rc == 0 and head_out.strip() else b"UNBORN"
            status_out, _ = git_evidence._git(
                root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], allowed=(0,)
            )
            cached_out, _ = git_evidence._git(
                root, ["diff", "--raw", "--no-abbrev", "--no-renames", "-z", "--cached"], allowed=(0,)
            )
    except (git_evidence.GitEvidenceError, OSError) as exc:
        return {"fingerprint": None, "is_clean": None, "head": None, "error": str(exc)}

    if isinstance(head_b, str):
        head_b = head_b.encode("utf-8", errors="replace")
    if isinstance(status_out, str):
        status_out = status_out.encode("utf-8", errors="surrogateescape")
    if isinstance(cached_out, str):
        cached_out = cached_out.encode("utf-8", errors="surrogateescape")

    head = head_b.decode("ascii", errors="replace")
    h.update(b"HEAD\x00" + head_b + b"\x00")
    h.update(b"STATUS\x00" + status_out + b"\x00")
    h.update(b"CACHED\x00" + cached_out + b"\x00")

    is_clean = not bool(status_out)
    # Parse porcelain v1 -z entries to hash dirty/untracked file contents
    dirty_paths = []
    if status_out:
        tokens = status_out.split(b"\x00")
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if len(tok) < 4:
                continue
            xy = tok[:2]
            rel = os.fsdecode(tok[3:])
            # Rename/copy in porcelain v1 -z consumes an extra NUL-terminated source path
            if xy[0:1] in (b"R", b"C") or xy[1:2] in (b"R", b"C"):
                i += 1
            if rel and rel not in dirty_paths:
                dirty_paths.append(rel)

    remaining_bytes = MAX_TOTAL_BYTES
    for idx, rel in enumerate(sorted(dirty_paths)):
        h.update(b"FILE\x00" + rel.encode("utf-8", errors="replace") + b"\x00")
        if idx >= MAX_FINGERPRINT_FILES or time.monotonic() > deadline:
            # Bounded metadata fallback so huge dirty trees never OOM or hang
            try:
                lst = os.lstat(str(git_root / rel))
                h.update(f"CAP:{lst.st_size}:{lst.st_mtime_ns}\x00".encode("ascii"))
            except OSError:
                h.update(b"MISSING\x00")
            continue
        clean, path_err = _safe_rel_repo_path(rel)
        if path_err:
            h.update(b"SKIP\x00")
            continue
        opened, open_err = _open_regular_inside_root(git_root, clean)
        if open_err:
            # Deleted file, directory, or symlink: include lstat signature if present
            try:
                lst = os.lstat(str(git_root / clean))
                h.update(f"NONREG:{lst.st_mode}:{lst.st_size}:{lst.st_mtime_ns}\x00".encode("ascii"))
            except OSError:
                h.update(b"GONE\x00")
            continue
        fd, st = opened
        try:
            if st.st_size > MAX_FILE_BYTES or st.st_size > remaining_bytes:
                h.update(f"BIG:{st.st_size}:{st.st_mtime_ns}\x00".encode("ascii"))
            else:
                file_hash = hashlib.sha256()
                nbytes = _stream_file_hash(fd, file_hash, remaining_bytes, deadline)
                if nbytes is None:
                    h.update(f"CAP:{st.st_size}:{st.st_mtime_ns}\x00".encode("ascii"))
                else:
                    remaining_bytes -= nbytes
                    h.update(b"SHA:" + file_hash.hexdigest().encode("ascii") + b"\x00")
        finally:
            os.close(fd)

    return {"fingerprint": h.hexdigest(), "is_clean": is_clean, "head": head, "error": None}


def _count_file_lines_bounded(root, rel_path, cache, state, deadline):
    """Stream-count lines of rel_path inside root with O_NOFOLLOW, size/time bounds, and per-call cache."""
    if rel_path in cache:
        return cache[rel_path]
    opened, err = _open_regular_inside_root(root, rel_path)
    if err:
        res = (None, err)
        cache[rel_path] = res
        return res
    fd, st = opened
    try:
        if st.st_size > MAX_FILE_BYTES:
            res = (None, f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MiB check limit: {rel_path}")
            cache[rel_path] = res
            return res
        if state["total_bytes"] + st.st_size > MAX_TOTAL_BYTES:
            res = (None, f"findings_grounded total read limit ({MAX_TOTAL_BYTES // (1024 * 1024)} MiB) exceeded")
            cache[rel_path] = res
            return res
        newlines = 0
        read_bytes = 0
        last_byte = b""
        while True:
            if time.monotonic() > deadline:
                res = (None, "findings_grounded check timed out")
                cache[rel_path] = res
                return res
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            read_bytes += len(chunk)
            state["total_bytes"] += len(chunk)
            if read_bytes > MAX_FILE_BYTES or state["total_bytes"] > MAX_TOTAL_BYTES:
                res = (None, "findings_grounded byte cap exceeded while reading " + rel_path)
                cache[rel_path] = res
                return res
            newlines += chunk.count(b"\n")
            last_byte = chunk[-1:]
        # Empty file has 0 lines; non-empty file without trailing newline has newlines + 1 lines
        if read_bytes == 0:
            line_count = 0
        else:
            line_count = newlines if last_byte == b"\n" else newlines + 1
        res = (line_count, None)
        cache[rel_path] = res
        return res
    finally:
        os.close(fd)


def verify_findings_grounded_io(findings, project_path, path_label="findings"):
    """Validate that every finding in data.<path_label> is grounded in an existing repo file and valid line.

    Empty list `[]` is valid (clean PRs with zero bugs).
    """
    if not isinstance(findings, list):
        return [f"check findings_grounded failed: data.{path_label} must be a list"]
    if project_path is None:
        return [f"check findings_grounded requires --project-path to verify data.{path_label}"]
    if len(findings) == 0:
        return []
    if len(findings) > MAX_GROUNDED_FINDINGS:
        return [
            f"check findings_grounded failed: data.{path_label} has {len(findings)} items (max {MAX_GROUNDED_FINDINGS})"
        ]

    root = Path(project_path).resolve()
    deadline = time.monotonic() + MAX_IO_SECONDS
    cache = {}
    state = {"total_bytes": 0}
    reasons = []

    for idx, item in enumerate(findings):
        prefix = f"check findings_grounded failed at data.{path_label}[{idx}]"
        if not isinstance(item, dict):
            reasons.append(f"{prefix}: finding must be a JSON object")
            continue
        raw_file = item.get("file")
        clean_file, file_err = _safe_rel_repo_path(raw_file)
        if file_err:
            reasons.append(f"{prefix}: {file_err}")
            continue
        raw_line = item.get("line")
        if isinstance(raw_line, bool) or not isinstance(raw_line, int) or raw_line < 1:
            reasons.append(f"{prefix}: line must be a positive integer")
            continue
        raw_end = item.get("end_line")
        if raw_end is not None and (isinstance(raw_end, bool) or not isinstance(raw_end, int) or raw_end < raw_line):
            reasons.append(f"{prefix}: end_line must be an integer >= line")
            continue
        sev = item.get("severity")
        if not isinstance(sev, str) or sev not in SEVERITIES:
            reasons.append(f"{prefix}: severity must be one of {', '.join(SEVERITIES)}")
        title = item.get("title")
        if (not isinstance(title, str) or len(title.strip()) < MIN_TITLE_CHARS
                or len(title) > MAX_TITLE_CHARS or "\x00" in title):
            reasons.append(f"{prefix}: title must be {MIN_TITLE_CHARS}-{MAX_TITLE_CHARS} chars of text")
        ev = item.get("evidence")
        if (not isinstance(ev, str) or len(ev.strip()) < MIN_EVIDENCE_CHARS
                or len(ev) > MAX_EVIDENCE_CHARS or "\x00" in ev):
            reasons.append(f"{prefix}: evidence must be {MIN_EVIDENCE_CHARS}-{MAX_EVIDENCE_CHARS} chars of text")
        st_val = item.get("status")
        if st_val is not None and st_val not in ("candidate", "confirmed", "disproved"):
            reasons.append(f"{prefix}: status must be candidate, confirmed, or disproved")

        line_count, io_err = _count_file_lines_bounded(root, clean_file, cache, state, deadline)
        if io_err:
            reasons.append(f"{prefix}: {io_err}")
            continue
        if line_count == 0 or raw_line > line_count:
            reasons.append(f"{prefix}: line {raw_line} is out of range for {clean_file} ({line_count} lines)")
        elif raw_end is not None and raw_end > line_count:
            reasons.append(f"{prefix}: end_line {raw_end} is out of range for {clean_file} ({line_count} lines)")
        if len(reasons) >= 15:
            reasons.append(f"check findings_grounded failed: too many errors in data.{path_label} (truncated)")
            break
    return reasons


# ---------------------------------------------------------------- read-side dedup & consensus


def _normalize_file(path):
    if not isinstance(path, str):
        return ""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _extract_identifiers(text):
    """Extract code identifiers (plus camelCase/snake_case sub-tokens) for semantic overlap."""
    if not isinstance(text, str) or not text:
        return set()
    ids = set()
    for m in _IDENT_RE.findall(text):
        low = m.lower()
        if low in _STOPWORDS:
            continue
        if "_" in m or any(c.isupper() for c in m[1:]):
            ids.add(low)
            for part in m.split("_"):
                for sub in _CAMEL_SPLIT.findall(part):
                    sl = sub.lower()
                    if len(sl) >= 3 and sl not in _STOPWORDS:
                        ids.add(sl)
        elif len(low) >= 4:
            ids.add(low)
    return ids


def _token_counts(text):
    if not isinstance(text, str) or not text:
        return Counter()
    return Counter(t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS)


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(len(a | b))


def _cosine(c1, c2):
    if not c1 or not c2:
        return 0.0
    dot = sum(v * c2[k] for k, v in c1.items() if k in c2)
    if dot == 0:
        return 0.0
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    return dot / (n1 * n2) if (n1 and n2) else 0.0


def canonical_finding_id(file_path, line, title, evidence=""):
    """Deterministic short ID (`f:<12-hex>`) for referencing a finding in feedback and across runs."""
    nf = _normalize_file(file_path)
    bucket = int(line) // 5 if isinstance(line, int) and not isinstance(line, bool) and line > 0 else 0
    ids = sorted(_extract_identifiers((title or "") + " " + (evidence or "")))[:6]
    sig = f"{nf}|{bucket}|{' '.join(ids) if ids else (title or '').strip().lower()[:80]}"
    return "f:" + hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12]


def findings_similar(a, b):
    """Status-agnostic similarity check for cross-step provenance propagation and cross-run matching."""
    fa = _normalize_file(a.get("file") or "")
    fb = _normalize_file(b.get("file") or "")
    if not fa or fa != fb:
        return False
    la = a.get("line") if (isinstance(a.get("line"), int) and not isinstance(a.get("line"), bool) and a.get("line") >= 1) else None
    lb = b.get("line") if (isinstance(b.get("line"), int) and not isinstance(b.get("line"), bool) and b.get("line") >= 1) else None
    ta = (str(a.get("title") or "") + " " + str(a.get("evidence") or "")).strip()
    tb = (str(b.get("title") or "") + " " + str(b.get("evidence") or "")).strip()
    id_j = _jaccard(_extract_identifiers(ta), _extract_identifiers(tb))
    tok_c = _cosine(_token_counts(ta), _token_counts(tb))
    if la is not None and lb is not None:
        if abs(la - lb) > CLUSTER_LINE_DIAMETER:
            return False
        return id_j >= 0.25 or tok_c >= 0.35
    return id_j >= 0.50 and tok_c >= 0.50


def _can_join_cluster(item, cluster_members):
    """Complete-linkage rule: item must match EVERY member in the cluster."""
    i_disproved = item["status"] == "disproved"
    for m in cluster_members:
        if (m["status"] == "disproved") != i_disproved:
            return False
        if m["file"] != item["file"]:
            return False
        il, ml = item["line"], m["line"]
        id_j = _jaccard(item["_ids"], m["_ids"])
        tok_c = _cosine(item["_toks"], m["_toks"])
        if il is not None and ml is not None:
            if abs(il - ml) > CLUSTER_LINE_DIAMETER:
                return False
            if not (id_j >= 0.25 or tok_c >= 0.35):
                return False
        else:
            if not (id_j >= 0.50 and tok_c >= 0.50):
                return False
    return True


def dedupe_findings(raw_findings, *, replicated_totals=None):
    """Pure read-side deduplication and consensus aggregation across steps / matrix passes.

    Each input dict should include:
      file, line (optional int), end_line (optional), severity, title, evidence,
      status (optional: candidate|confirmed|disproved),
      step_id, step_key, matrix_label (optional), matrix_mode (optional: orthogonal|replicated).

    replicated_totals: optional dict `{step_id: N_done}` giving the denominator for replicated passes.
    """
    replicated_totals = replicated_totals or {}
    prepared = []
    for raw in raw_findings or ():
        if not isinstance(raw, dict):
            continue
        nf = _normalize_file(raw.get("file") or "")
        line = raw.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            line = None
        end_line = raw.get("end_line")
        if (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < 1
                or line is None or end_line < line):
            end_line = None
        sev = raw.get("severity") if raw.get("severity") in SEVERITY_RANK else "P2"
        title = str(raw.get("title") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()
        st = raw.get("status") if raw.get("status") in ("candidate", "confirmed", "disproved") else "confirmed"
        text_for_sim = (title + " " + evidence).strip()
        prepared.append({
            "file": nf,
            "line": line,
            "end_line": end_line,
            "severity": sev,
            "title": title,
            "evidence": evidence,
            "status": st,
            "step_id": raw.get("step_id") or "",
            "step_key": raw.get("step_key") or "",
            "matrix_label": raw.get("matrix_label") or "",
            "matrix_mode": raw.get("matrix_mode") or "orthogonal",
            "_ids": _extract_identifiers(text_for_sim),
            "_toks": _token_counts(text_for_sim),
        })

    # Stable deterministic order: file, line (None last), severity rank, step_key
    prepared.sort(key=lambda x: (
        x["file"],
        x["line"] if x["line"] is not None else 10**9,
        SEVERITY_RANK.get(x["severity"], 9),
        x["step_key"],
        x["title"],
    ))

    clusters = []
    for item in prepared:
        placed = False
        for members in clusters:
            if _can_join_cluster(item, members):
                members.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    results = []
    seen_fids = {}
    for members in clusters:
        # Pick representative: prefer confirmed over candidate, then highest severity, longest evidence, lowest line
        members_sorted = sorted(
            members,
            key=lambda x: (
                0 if x["status"] == "confirmed" else 1,
                SEVERITY_RANK.get(x["severity"], 9),
                -len(x["evidence"]),
                x["line"] if x["line"] is not None else 10**9,
                x["step_key"],
            ),
        )
        rep = members_sorted[0]
        sev_votes = {}
        for m in members:
            sev_votes[m["severity"]] = sev_votes.get(m["severity"], 0) + 1

        # Separate orthogonal corroboration from replicated pass consensus
        corroborated_by = []
        replicated_by_step = {}
        source_steps = []
        for m in members:
            label = m["matrix_label"] or m["step_id"] or m["step_key"]
            if m["step_key"] and m["step_key"] not in source_steps:
                source_steps.append(m["step_key"])
            if m["matrix_mode"] == "replicated" and m["step_id"]:
                replicated_by_step.setdefault(m["step_id"], set()).add(m["step_key"] or label)
            else:
                if label and label not in corroborated_by:
                    corroborated_by.append(label)

        consensus_ratio = None
        for sid, keys in sorted(replicated_by_step.items()):
            total_done = int(replicated_totals.get(sid) or 0)
            if total_done >= 2:
                consensus_ratio = {"votes": len(keys), "total": total_done, "step_id": sid}
                break

        lines = [m["line"] for m in members if m["line"] is not None]
        min_line = min(lines) if lines else None
        max_line = max(lines) if lines else None
        end_cands = [m["end_line"] for m in members if m["end_line"] is not None]
        if max_line is not None and max_line != min_line:
            end_cands.append(max_line)
        merged_end_line = max(end_cands) if end_cands else None
        base_fid = canonical_finding_id(rep["file"], min_line, rep["title"], rep["evidence"])
        if rep["status"] == "disproved":
            base_fid = base_fid + ":d"
        cnt = seen_fids.get(base_fid, 0) + 1
        seen_fids[base_fid] = cnt
        fid = base_fid if cnt == 1 else f"{base_fid}-{cnt}"
        results.append({
            "finding_id": fid,
            "file": rep["file"],
            "line": min_line,
            "end_line": merged_end_line,
            "severity": rep["severity"],
            "severity_votes": sev_votes,
            "title": rep["title"],
            "evidence": rep["evidence"],
            "status": rep["status"],
            "step_id": rep["step_id"],
            "source_steps": source_steps,
            "corroborated_by": corroborated_by,
            "consensus_ratio": consensus_ratio,
            "occurrences": len(members),
        })

    results.sort(key=lambda x: (
        1 if x["status"] == "disproved" else 0,
        SEVERITY_RANK.get(x["severity"], 9),
        x["file"],
        x["line"] if x["line"] is not None else 10**9,
        x["finding_id"],
    ))
    return results
