#!/usr/bin/env python3
"""Measure synthetic memory growth; quality misses are data, not harness errors.

Default exit 0 means execution/invariants passed, NOT that every target was
retrieved. --fail-on-miss also makes measured quality misses return exit 1.
All stores and homes are disposable; there is deliberately no --db argument.
"""
import argparse
import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
NORMAL_BUDGET = 16000
DIAGNOSTIC_BUDGET = 100000
AS_OF = "2026-10-15"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def environment(directory):
    root = Path(directory) / "home"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("RICHI_", "PYTHON")) and key != "CODEX_THREAD_ID"}
    env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / ".config"),
               XDG_DATA_HOME=str(root / ".local" / "share"))
    return env


def cli(database, *arguments, payload=None, timeout=60):
    encoded = None if payload is None else json.dumps(payload, ensure_ascii=False)
    started = time.perf_counter_ns()
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "richi", "--config", str(database.parent / "config.json"),
         "--db", str(database), *arguments], input=encoded, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout, env=environment(database.parent))
    elapsed = (time.perf_counter_ns() - started) / 1000000
    if proc.returncode:
        raise ValueError(proc.stderr.strip() or "Richi returned a nonzero exit status")
    if proc.stderr:
        raise ValueError("Unexpected CLI stderr: " + proc.stderr.strip())
    return proc.stdout, json.loads(proc.stdout), elapsed


def validate(document):
    if not isinstance(document, dict):
        raise ValueError("Fixture must be a JSON object")
    for field in ("project", "entry", "entity", "edge", "cases"):
        if (not isinstance(document.get(field), list)
                or any(not isinstance(row, dict) for row in document[field])):
            raise ValueError("Fixture %s must be an array of objects" % field)
        if any(not isinstance(row.get("id"), str) or not row["id"] for row in document[field]):
            raise ValueError("Fixture %s rows need nonempty string IDs" % field)
    generator = document.get("generator")
    if not isinstance(generator, dict):
        raise ValueError("Fixture generator must be an object")
    for field in ("background_title", "history_target"):
        if not isinstance(generator.get(field), str) or not generator[field]:
            raise ValueError("Generator %s must be a nonempty string" % field)
    if (not isinstance(generator.get("background_sentences"), list)
            or not generator["background_sentences"]
            or any(not isinstance(text, str) or not text for text in generator["background_sentences"])):
        raise ValueError("Provide nonempty background prose templates")
    if (not isinstance(generator.get("version"), int) or isinstance(generator["version"], bool)
            or generator["version"] < 1):
        raise ValueError("Generator version must be a positive integer")
    catalog = {}
    for family in ("entry", "entity"):
        for record in document.get(family, []):
            ref = family + ":" + record["id"]
            if ref in catalog:
                raise ValueError("Duplicate fixture reference: " + ref)
            catalog[ref] = record
    cases = document.get("cases", [])
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Provide nonempty cases with unique IDs")
    for case in cases:
        for field in ("expected_refs", "excluded_refs"):
            if (not isinstance(case.get(field, []), list)
                    or any(not isinstance(ref, str) for ref in case.get(field, []))):
                raise ValueError("Case %s must be an array of reference strings" % field)
        if not isinstance(case.get("expect_ref"), str):
            raise ValueError("Case expect_ref must be a reference string")
        for field in ("expected_no_match", "include_hypotheses"):
            if field in case and not isinstance(case[field], bool):
                raise ValueError("Case %s must be boolean" % field)
        if case.get("project") is not None and not isinstance(case["project"], str):
            raise ValueError("Case project must be a project ID")
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError("Each case needs a query")
        if not isinstance(case.get("limit"), int) or isinstance(case["limit"], bool) or not 1 <= case["limit"] <= 200:
            raise ValueError("Case limit must be 1..200")
        expected = case.get("expected_refs", [])
        if bool(expected) == bool(case.get("expected_no_match", False)):
            raise ValueError("Specify expected_refs OR expected_no_match: " + case["id"])
        for ref in expected + case.get("excluded_refs", []) + [case["expect_ref"]]:
            if ref not in catalog:
                raise ValueError("Unknown expected reference: " + ref)
    if document["generator"]["history_target"] not in {record["id"] for record in document["entry"]}:
        raise ValueError("Unknown history target")
    return catalog


def dataset_specs(quick=False):
    axes = {"background": [8, 24], "details": [0, 3, 12], "history": [0, 2, 5]} if quick else {
        "background": [100, 1000, 10000], "details": [0, 10, 100], "history": [0, 10, 100]}
    baseline = dict(background=axes["background"][0], details=0, history=0)
    specs = {}
    for axis, values in axes.items():
        for value in values:
            params = dict(baseline, **{axis: value})
            identity = "b{background}-d{details}-h{history}".format(**params)
            if identity not in specs:
                specs[identity] = dict(params, id=identity, axes=[])
            specs[identity]["axes"].append(axis)
    return list(specs.values())


def generate(document, background, details):
    """Deterministic nested current corpus; history is an independent write axis."""
    corpus = {family: copy.deepcopy(document.get(family, []))
              for family in ("project", "entry", "entity", "edge")}
    generator = document["generator"]
    for index in range(background):
        identity = "note:background-%05d" % index
        summary = generator["background_sentences"][index % len(generator["background_sentences"])].format(index=index)
        # Several natural sentences retain scope, rather than one repeated byte.
        summary += " The fixture records a separate observation with its own provenance; no production conclusion follows."
        corpus["entry"].append(dict(
            id=identity, kind="note", title=generator["background_title"], summary=summary,
            project_ids=["relay" if index % 10 < 7 else "ledger"], jira_key=None,
            knowledge_state="confirmed", work_state="done", verified_at="2026-10-01",
            sources=[{"reference": "https://example.invalid/growth/" + identity, "type": "synthetic"}],
            aliases=[], tags=[]))
    for index in range(details):
        identity = "investigation:detail-%05d" % index
        source = [{"reference": "https://example.invalid/growth/" + identity, "type": "synthetic"}]
        corpus["entry"].append(dict(
            id=identity, kind="investigation", title="DEMO-84 receipt investigation %05d" % index,
            summary="Historical detail %d of DEMO-84 describes a synthetic receipt inspection. It does not establish the current implementation or release state." % index,
            project_ids=["relay"], jira_key="DEMO-84", knowledge_state="confirmed", work_state="done",
            verified_at="2026-10-01", sources=source, aliases=[], tags=[]))
        corpus["edge"].append(dict(id="part:" + identity, from_ref="entry:" + identity,
                                   to_ref="entry:task:DEMO-84", kind="part_of",
                                   description="Historical investigation detail belongs to DEMO-84.",
                                   knowledge_state="confirmed", sources=source, verified_at="2026-10-01"))
    return corpus


def seed(database, corpus, revisions, timeout=60, history_target="task:DEMO-84"):
    database.parent.mkdir(parents=True, exist_ok=True)
    (database.parent / "config.json").write_text("{}\n", encoding="utf-8")
    cli(database, "init", timeout=timeout)
    target = next(row for row in corpus["entry"] if row["id"] == history_target)
    for family in ("project", "entry", "entity", "edge"):
        rows = copy.deepcopy(corpus[family])
        if family == "entry" and revisions:
            next(row for row in rows if row["id"] == history_target)["summary"] += " Historical draft 0; archivedgrowthnote."
        for start in range(0, len(rows), 250):
            cli(database, family, "upsert" if family == "project" else "put", "--json", "-",
                payload=rows[start:start + 250], timeout=timeout)
    # Exactly `revisions` additional full-payload writes; final current content
    # equals the zero-history dataset. No direct SQL inserts or schema changes.
    for revision in range(1, revisions + 1):
        payload = copy.deepcopy(target)
        if revision < revisions:
            payload["summary"] += " Historical draft %d; archivedgrowthnote." % revision
        cli(database, "entry", "put", "--json", "-", payload=payload, timeout=timeout)


def summarize_latencies(samples):
    ordered = sorted(samples)
    return {"samples_ms": samples, "median_ms": statistics.median(samples),
            "p95_ms": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
            "p95_method": "nearest-rank; small samples do not estimate a stable tail",
            "min_ms": min(samples), "max_ms": max(samples)}


def classify_result(raw, response, case, catalog, maximum):
    """Quality misses and safety/serialization failures are independent outcomes."""
    cards = response["results"]
    refs = [card["ref"] for card in cards]
    expected = case.get("expected_refs", [])
    positions = {ref: refs.index(ref) + 1 if ref in refs else None for ref in expected}
    found = len(set(expected) & set(refs[:case["limit"]]))
    misses = ["target absent: " + ref for ref in expected if ref not in refs[:case["limit"]]]
    if response.get("no_match") is not bool(case.get("expected_no_match", False)):
        misses.append("no_match expectation differs")
    failures = []
    checks = {
        "serialized budget": len(raw) <= maximum,
        "declared output size": response.get("budget", {}).get("output_chars") == len(raw),
        "unique returned refs": len(refs) == len(set(refs)),
        "result limit": len(refs) <= case["limit"],
        "no_match reflects candidates": response.get("no_match") is (response.get("candidate_count", 0) == 0),
        "excluded refs absent": not set(case.get("excluded_refs", [])) & set(refs),
    }
    for card in cards:
        record = catalog.get(card["ref"])
        if record is None:
            failures.append("unknown returned ref: " + card["ref"])
            continue
        allowed = {"confirmed"} | ({"hypothesis"} if case.get("include_hypotheses") else set())
        if card.get("knowledge_state") not in allowed or card.get("knowledge_state") != record["knowledge_state"]:
            failures.append("knowledge state changed/escaped filter: " + card["ref"])
        if card.get("work_state") != record.get("work_state"):
            failures.append("work state changed: " + card["ref"])
        if not card.get("sources") or any(source not in record["sources"] for source in card["sources"]):
            failures.append("provenance missing/changed: " + card["ref"])
        family, identity = card["ref"].split(":", 1)
        if card.get("read_more") != {"command": family + " get", "id": identity}:
            failures.append("full-read pointer changed: " + card["ref"])
    failures.extend(name for name, passed in checks.items() if not passed)
    return {"returned_refs": refs, "positions": positions, "hit_at_limit": bool(found) if expected else None,
            "recall_at_limit": found / len(expected) if expected else None,
            "reciprocal_rank": 1 / min(p for p in positions.values() if p is not None) if found else 0 if expected else None,
            "no_match": response.get("no_match"), "candidate_count": response.get("candidate_count"),
            "output_chars": len(raw), "truncated": response.get("truncated"), "budget": response.get("budget"),
            "quality_misses": misses, "invariant_failures": failures}


def measure_case(database, case, catalog, repeats, timeout):
    flags = ["--project", case["project"]] if case.get("project") else []
    if case.get("include_hypotheses"):
        flags.append("--include-hypotheses")
    command = ["recall", case["query"], "--limit", str(case["limit"])] + flags
    # First call is separately reported; OS/file caches were not flushed.
    raw, response, first_elapsed = cli(database, *command, "--max-chars", str(NORMAL_BUDGET), timeout=timeout)
    first = classify_result(raw, response, case, catalog, NORMAL_BUDGET)
    samples, repeated = [], []
    for _ in range(repeats):
        raw, response, elapsed = cli(database, *command, "--max-chars", str(NORMAL_BUDGET), timeout=timeout)
        samples.append(elapsed)
        repeated.append(classify_result(raw, response, case, catalog, NORMAL_BUDGET))
    invariant_failures = sorted({failure for row in [first] + repeated for failure in row["invariant_failures"]})
    if any(row["returned_refs"] != first["returned_refs"] for row in repeated):
        invariant_failures.append("normal recall returned refs changed across read-only repeats")
    quality_misses = sorted({failure for row in [first] + repeated for failure in row["quality_misses"]})
    raw, response, elapsed = cli(database, *command, "--max-chars", str(DIAGNOSTIC_BUDGET),
                                 "--expect", case["expect_ref"], timeout=timeout)
    diagnostic_check = classify_result(raw, response, case, catalog, DIAGNOSTIC_BUDGET)
    invariant_failures.extend("diagnostic: " + failure for failure in diagnostic_check["invariant_failures"])
    if not isinstance(response.get("explain"), dict):
        invariant_failures.append("diagnostic explain is missing")
    return {"id": case["id"], "query": case["query"], "limit": case["limit"],
            "expected_refs": case["expected_refs"], "expect_ref": case["expect_ref"],
            "quality_misses": quality_misses, "invariant_failures": invariant_failures,
            "normal": dict(first, first_call_ms=first_elapsed, repeated=summarize_latencies(samples)),
            "diagnostic": {"elapsed_ms": elapsed, "output_chars": len(raw),
                           "max_chars": DIAGNOSTIC_BUDGET, "returned_refs": diagnostic_check["returned_refs"],
                           "budget": response.get("budget"), "truncated": response.get("truncated"),
                           "selection": response.get("selection"), "explain": response.get("explain")}}


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def readonly_digest(database):
    # Readers may create/remove an empty WAL on some SQLite/VFS versions.
    # It contains no frames, so absence and zero bytes are equivalent here.
    # Always hash database bytes and every nonempty WAL; SHM is coordination.
    return {suffix or "database": file_digest(Path(str(database) + suffix))
            for suffix in ("", "-wal") if Path(str(database) + suffix).exists()
            and (not suffix or Path(str(database) + suffix).stat().st_size > 0)}


def storage(database):
    # sqlite3.Connection's context manager manages a transaction, not closing.
    # Release the reader before later CLI calls and the final byte comparison.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        counts = {table: conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                  for table in ("entries", "entities", "graph_edges", "entry_history", "entity_history", "edge_history")}
        chars, byte_count = conn.execute("SELECT coalesce(sum(length(summary)),0), coalesce(sum(length(CAST(summary AS BLOB))),0) FROM entries").fetchone()
        before_bytes, after_bytes = conn.execute("SELECT coalesce(sum(length(CAST(before_json AS BLOB))),0), coalesce(sum(length(CAST(after_json AS BLOB))),0) FROM entry_history").fetchone()
        pages = {name: conn.execute("PRAGMA " + name).fetchone()[0]
                 for name in ("page_size", "page_count", "freelist_count", "user_version")}
        backend = conn.execute("SELECT value FROM metadata WHERE key='search_backend'").fetchone()[0]
    return {"counts": counts, "current_summary_chars": chars, "current_summary_utf8_bytes": byte_count,
            "entry_history_before_json_bytes": before_bytes, "entry_history_after_json_bytes": after_bytes,
            "files_bytes": {suffix or "database": Path(str(database) + suffix).stat().st_size
                            for suffix in ("", "-wal", "-shm") if Path(str(database) + suffix).exists()},
            "sqlite_pages": pages, "search_backend": backend}


def installed_metadata(directory):
    code = """import hashlib, json, platform, sqlite3, sys
from importlib.resources import files
import richi
names = ('memory.py','recall.py','compact.py','punisher.py','graph.py','schema.sql','graph_schema.sql')
root = files('richi')
print(json.dumps({'richi_version': richi.__version__, 'python': sys.version,
 'executable': sys.executable, 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
 'installed_module': str(root), 'code_sha256': {n: hashlib.sha256(root.joinpath(n).read_bytes()).hexdigest() for n in names if root.joinpath(n).is_file()}}))
"""
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True,
                          text=True, encoding="utf-8", timeout=30, env=environment(directory))
    if proc.returncode:
        raise ValueError("Installed runtime inspection failed: " + proc.stderr)
    return json.loads(proc.stdout)


def run_dataset(directory, spec, document, repeats, timeout):
    database = Path(directory) / spec["id"] / "synthetic.sqlite3"
    corpus = generate(document, spec["background"], spec["details"])
    catalog = {family + ":" + row["id"]: row for family in ("entry", "entity") for row in corpus[family]}
    started = time.perf_counter_ns()
    seed(database, corpus, spec["history"], timeout, document["generator"]["history_target"])
    seed_elapsed = (time.perf_counter_ns() - started) / 1000000
    before = readonly_digest(database)
    sizes = storage(database)
    full_reads, failures = [], []
    for ref in sorted({case["expect_ref"] for case in document["cases"]} |
                      {ref for case in document["cases"] for ref in case["expected_refs"]}):
        family, identity = ref.split(":", 1)
        arguments = [family, "get"] + (["--"] if identity.startswith("-") else []) + [identity]
        raw, response, _ = cli(database, *arguments, timeout=timeout)
        mismatched = [field for field, value in catalog[ref].items() if response.get(field) != value]
        failures.extend("full get changed %s: %s" % (ref, field) for field in mismatched)
        full_reads.append({"ref": ref, "read_arguments": arguments, "output_chars": len(raw),
                           "summary_chars": len(response["summary"]), "summary_sha256": digest(response["summary"]),
                           "sources": response["sources"], "knowledge_state": response["knowledge_state"],
                           "work_state": response.get("work_state"), "content_matches_fixture": not mismatched})
    cases = []
    for case in document["cases"]:
        try:
            cases.append(measure_case(database, case, catalog, repeats, timeout))
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            cases.append({"id": case["id"], "harness_error": str(exc), "quality_misses": [], "invariant_failures": []})
    raw, audit, elapsed = cli(database, "punisher", "--project", "relay", "--as-of", AS_OF,
                               "--summary-warn-chars", "6000", "--limit", "20", "--max-chars", "16000", timeout=timeout)
    if len(raw) > NORMAL_BUDGET or audit["budget"]["output_chars"] != len(raw):
        failures.append("punisher serialized budget mismatch")
    if audit["health"]["status"] != "ok":
        failures.append("synthetic store integrity health is not ok")
    after = readonly_digest(database)
    if before != after:
        failures.append("read-only operations changed database/WAL bytes")
    return dict(spec, current_corpus_sha256=digest(corpus), seed_elapsed_ms=seed_elapsed,
                storage=sizes, read_only={"before": before, "after": after, "unchanged": before == after},
                full_reads=full_reads, cases=cases, invariant_failures=failures,
                punisher={"elapsed_ms": elapsed, "output_chars": len(raw), "report": audit})


def exit_status(report, fail_on_miss=False):
    summary = report["summary"]
    return int(not summary["execution_completed"] or bool(summary["invariant_failures"])
               or (fail_on_miss and bool(summary["quality_misses"])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Small independent axes for CI; not the full growth study")
    parser.add_argument("--repeats", type=int, default=3, help="Timed repeats after the first call, 1..20 (default 3)")
    parser.add_argument("--timeout", type=float, default=60, help="Each CLI call timeout in seconds, >0..600")
    parser.add_argument("--cases", type=Path, default=HERE / "memory_growth_cases.json")
    parser.add_argument("--output", type=Path, help="Create a new JSON report; refuse existing paths")
    parser.add_argument("--fail-on-miss", action="store_true", help="Also exit nonzero on measured quality misses")
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 20 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 600:
        parser.error("Use repeats 1..20 and a finite timeout >0..600")
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter_ns()
    try:
        if args.output and args.output.exists():
            raise ValueError("Refusing to overwrite an existing report")
        fixture_bytes = args.cases.read_bytes()
        runner_sha256 = file_digest(Path(__file__))
        document = json.loads(fixture_bytes)
        validate(document)
        specs = dataset_specs(args.quick)
        with tempfile.TemporaryDirectory(prefix="richi-memory-growth-") as temporary:
            metadata = installed_metadata(temporary)
            datasets = []
            for spec in specs:
                try:
                    datasets.append(run_dataset(temporary, spec, document, args.repeats, args.timeout))
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
                    datasets.append(dict(spec, harness_error=str(exc), cases=[], invariant_failures=[]))
        all_cases = [case for dataset in datasets for case in dataset["cases"]]
        errors = [row["harness_error"] for row in datasets + all_cases if row.get("harness_error")]
        invariants = sum(len(row["invariant_failures"]) for row in datasets + all_cases)
        misses = sum(bool(case["quality_misses"]) for case in all_cases)
        report = {
            "schema_version": 1, "mode": "quick" if args.quick else "full",
            "configuration": {"repeats_after_first_call": args.repeats, "timeout_seconds": args.timeout,
                              "normal_max_chars": NORMAL_BUDGET, "diagnostic_max_chars": DIAGNOSTIC_BUDGET,
                              "punisher_as_of": AS_OF, "independent_axes": True,
                              "fail_on_miss": args.fail_on_miss},
            "provenance": dict(metadata, fixture_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
                               generator_sha256=runner_sha256, generator_version=document["generator"]["version"],
                               started_at_utc=started_at, completed_at_utc=datetime.now(timezone.utc).isoformat(),
                               duration_ms=(time.perf_counter_ns() - started) / 1000000),
            "scenarios": document["cases"], "datasets": datasets,
            "summary": {"execution_completed": not errors, "harness_errors": errors,
                        "invariant_failures": invariants, "quality_misses": misses,
                        "quality_expectations_met": not errors and invariants == 0 and misses == 0,
                        "datasets": len(datasets), "cases": len(all_cases)},
            "limitations": [
                "Quality_misses counts dataset/case pairs; exit 0 without --fail-on-miss only confirms execution and invariants.",
                "Synthetic retrieval observations, not production accuracy, LLM quality, time savings or token estimates.",
                "Normal timings include process startup, CLI work and stdout capture; JSON parsing/checks are outside the timer.",
                "The first call is not a cold-cache measurement; caches are not flushed. Host workload is uncontrolled.",
                "Diagnostic --expect runs separately with a larger budget and can return different cards; its duration is not normal latency.",
                "Recall --project boosts relevance; punisher --project scopes records strictly but integrity checks remain global.",
                "FTS shortlist and graph caps can lose targets. Counts/outcomes describe executed work, not a hypothetical exhaustive search.",
                "The crowded lexical query has many legitimate lexical matches; missing its designated target measures bounded discoverability, not objective irrelevance of the other results.",
                "DB bytes include indexes/history/free pages; history JSON byte counts do not attribute physical SQLite table storage.",
                "Read-only digests compare database bytes and nonempty WAL bytes; an absent versus empty WAL is equivalent, and transient SHM is excluded.",
                "History axes have identical final current content; content hashes exclude generated created_at/updated_at timestamps.",
                "This fixture grows entries and their task-detail edges, not entities; entity counters are reported but entity-growth performance is not measured.",
                "Sources are fictional and never fetched. Full get checks preserve provenance/states; excerpts may omit conditions.",
            ],
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        print(encoded, end="")
        return exit_status(report, args.fail_on_miss)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
