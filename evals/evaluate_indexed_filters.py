#!/usr/bin/env python3
"""Synthetic fixed-installation comparison; never accepts an existing user DB.

Quality misses are reported separately from parity/invariant failures. Timings
are observations, not CI thresholds. Composite-filter token comparisons use a
fixed answer set, not an agent judging which records to fetch.
"""
import argparse
from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import importlib.resources
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
LEGACY_COMMIT = "ff966896ec9c28497430ec18b2c39706ee86d5ab"
LEGACY_SHA256 = "a38d6fdad925a1f2045b13faad83a611bbe01ed274fba42b0888772df4b60e31"
TABLES = ("metadata", "projects", "entries", "entry_projects", "relations", "entry_history", "entry_events",
          "entities", "entity_history", "graph_nodes", "graph_edges", "edge_history")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def environment(root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("RICHI_", "PYTHON")) and key != "CODEX_THREAD_ID"}
    home = Path(root) / "synthetic-home"
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"))
    return env


def summary(samples):
    ordered = sorted(samples)
    return {"samples_ms": samples, "p50_ms": statistics.median(samples),
            "p95_ms": ordered[math.ceil(len(samples) * .95) - 1],
            "p95_method": "nearest rank; small samples do not establish stable tail latency"}


def physical(database):
    result = {}
    for suffix in ("", "-wal"):
        path = Path(str(database) + suffix)
        if path.exists() and path.stat().st_size:
            result["main" if not suffix else "wal"] = {"bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def logical(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        rows = {table: conn.execute('SELECT rowid,* FROM "' + table + '" ORDER BY rowid').fetchall()
                for table in TABLES}
        rows["metadata"] = [row for row in rows["metadata"] if row[1] != "richi_search_index_version"]
        rows["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return digest(rows)


def cli(python, database, arguments):
    started = time.perf_counter_ns()
    proc = subprocess.run([str(python), "-I", "-B", "-m", "richi", "--config",
        str(database.parent / "config.json"), "--db", str(database), "--compact-json", *arguments],
        capture_output=True, text=True, encoding="utf-8", timeout=180, env=environment(database.parent))
    elapsed = (time.perf_counter_ns() - started) / 1e6
    if proc.returncode or proc.stderr:
        raise RuntimeError(proc.stderr or proc.stdout)
    return proc.stdout, json.loads(proc.stdout), elapsed


def worker(python, root, request):
    proc = subprocess.run([str(python), "-I", "-B", str(Path(__file__).resolve()), "--worker"],
        input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8",
        timeout=600, env=environment(root))
    if proc.returncode or proc.stderr:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def clone(source, destination):
    destination.parent.mkdir(parents=True)
    (destination.parent / "config.json").write_text("{}\n", encoding="utf-8")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as source_conn:
        with closing(sqlite3.connect(destination)) as target:
            source_conn.backup(target)


def basic_entry(identity, **changes):
    prose = ("The synthetic transport retains a verified receipt for each partition. "
             "The checked scope is a single recovery window, not a production-wide conclusion. "
             "A separate source records the observed order of journal writes and acknowledgements. "
             "Only resume after the checkpoint is durable; a cold restore needs manual verification. "
             "The note preserves both the failed alternative and the conditions of the retained approach. ")
    value = {"id": identity, "kind": "note", "title": "Transport observation " + identity,
             "summary": prose + "Fixture sequence " + identity + ".", "project_ids": ["atlas"],
             "knowledge_state": "confirmed", "work_state": "done", "verified_at": "2026-10-01",
             "sources": [{"reference": "https://example.test/indexed/" + identity, "type": "synthetic"}],
             "aliases": [], "tags": ["transport"], "jira_key": None}
    value.update(changes)
    return value


def corpus(size, acceptance):
    data = {family: copy.deepcopy(acceptance[family]) for family in ("project", "entry", "entity", "edge")}
    data["entry"] += [basic_entry("unicode-target", aliases=["Straße ΩЁ"]),
                      basic_entry("phrase-target", aliases=["flux canal"], summary="Flux canal failed under the synthetic load."),
                      basic_entry("hypothesis-target", aliases=["hiddenprobe"], knowledge_state="hypothesis"),
                      basic_entry("superseded-target", aliases=["oldprobe"], knowledge_state="superseded")]
    for index in range(size - len(data["entry"])):
        data["entry"].append(basic_entry("background-%05d" % index,
            title=("Receipt transport", "Cache coordination", "Partition recovery", "Snapshot validation")[index % 4],
            project_ids=["atlas" if index % 3 else "beacon"],
            knowledge_state="hypothesis" if index % 10 == 0 else "superseded" if index % 10 == 1 else "confirmed",
            aliases=["archive %05d" % index, "поток %05d" % index], tags=["transport", "group-%d" % (index % 5)]))
    for index in range(max(0, size // 10 - len(data["entity"]))):
        data["entity"].append({"id": "component-%05d" % index, "kind": "component",
            "title": "Isolated fixture component %d" % index,
            "summary": basic_entry("component")['summary'], "knowledge_state": "confirmed",
            "sources": [{"reference": "https://example.test/component/%d" % index}],
            "aliases": ["component route %d" % index], "tags": ["fixture"], "verified_at": "2026-10-01"})
    cases = copy.deepcopy(acceptance["cases"])
    cases += [
        {"id": "unicode-folded-alias", "query": "STRASSE ΩЕ", "expected_refs": ["entry:unicode-target"]},
        {"id": "alias-phrase", "query": "why flux canal failed", "expected_refs": ["entry:phrase-target"]},
        {"id": "exact-entity", "query": "entity:concept:queue", "expected_refs": ["entity:concept:queue"]},
        {"id": "hypothesis-excluded", "query": "hiddenprobe", "expected_no_match": True},
        {"id": "hypothesis-included", "query": "hiddenprobe", "include_hypotheses": True,
         "expected_refs": ["entry:hypothesis-target"]},
        {"id": "superseded-included", "query": "oldprobe", "include_superseded": True,
         "expected_refs": ["entry:superseded-target"]},
        {"id": "broad-background", "query": "receipt transport", "measure_only": True},
    ]
    return data, cases


def legacy_corpus(document, background, details):
    data = {family: copy.deepcopy(document[family]) for family in ("project", "entry", "entity", "edge")}
    generator = document["generator"]
    for index in range(background):
        identity = "note:background-%05d" % index
        text = generator["background_sentences"][index % len(generator["background_sentences"])].format(index=index)
        text += " The fixture records a separate observation with its own provenance; no production conclusion follows."
        data["entry"].append(dict(id=identity, kind="note", title=generator["background_title"], summary=text,
            project_ids=["relay" if index % 10 < 7 else "ledger"], jira_key=None, knowledge_state="confirmed",
            work_state="done", verified_at="2026-10-01", sources=[{"reference": "https://example.invalid/growth/" + identity,
            "type": "synthetic"}], aliases=[], tags=[]))
    for index in range(details):
        identity = "investigation:detail-%05d" % index
        source = [{"reference": "https://example.invalid/growth/" + identity, "type": "synthetic"}]
        data["entry"].append(dict(id=identity, kind="investigation", title="DEMO-84 receipt investigation %05d" % index,
            summary="Historical detail %d of DEMO-84 describes a synthetic receipt inspection. It does not establish the current implementation or release state." % index,
            project_ids=["relay"], jira_key="DEMO-84", knowledge_state="confirmed", work_state="done",
            verified_at="2026-10-01", sources=source, aliases=[], tags=[]))
        data["edge"].append(dict(id="part:" + identity, from_ref="entry:" + identity, to_ref="entry:task:DEMO-84",
            kind="part_of", description="Historical investigation detail belongs to DEMO-84.",
            knowledge_state="confirmed", sources=source, verified_at="2026-10-01"))
    return data


def filter_corpus(crowded):
    data = {"project": [{"id": "atlas", "name": "Atlas"}, {"id": "beacon", "name": "Beacon"}],
            "entry": [], "entity": [], "edge": []}
    count = 360 if crowded else 24
    for index in range(count):
        wanted = index >= count - 3
        data["entry"].append(basic_entry("filter-%03d" % index,
            title="Filterneedle transport " + ("filterneedle " * (0 if wanted else 4)),
            summary="Filterneedle appears in this scoped synthetic observation. " + basic_entry("scope")["summary"],
            project_ids=["atlas" if wanted or index % 4 else "beacon"],
            kind="decision" if wanted else "note", work_state="done" if wanted else "planned",
            tags=["selected" if wanted else "other"], aliases=[], verified_at="2026-10-01" if wanted else "2020-01-01"))
    wanted = ["entry:filter-%03d" % index for index in range(count - 3, count)]
    cases = [
        {"id": "project-and-kind", "query": "filterneedle", "where": "project = 'atlas' AND kind = 'decision'"},
        {"id": "nested-or-not", "query": "filterneedle", "where": "project = 'atlas' AND (kind = 'decision' OR kind = 'experiment') AND NOT work_state = 'planned'"},
        {"id": "tag-date", "query": "filterneedle", "where": "tags HAS 'selected' AND verified_at >= '2026-10-01'"},
        {"id": "in-and-null", "query": "filterneedle", "where": "kind IN ('decision', 'experiment') AND jira_key IS NULL AND project = 'atlas'"},
    ]
    for case in cases:
        case.update(expected_refs=wanted, expected_exact_set=wanted, limit=8, max_chars=16000)
    return data, cases, wanted


def arguments(case, explain=False):
    args = ["recall", case["query"], "--limit", str(case.get("limit", 8)),
            "--max-chars", str(100000 if explain else case.get("max_chars", 16000))]
    for field in ("project", "where"):
        if case.get(field):
            args += ["--" + field, case[field]]
    for field in ("include_hypotheses", "include_superseded"):
        if case.get(field):
            args.append("--" + field.replace("_", "-"))
    if explain:
        args.append("--explain")
        if case.get("expect_ref"):
            args += ["--expect", case["expect_ref"]]
    return args


def quality(case, response):
    refs = [item["ref"] for item in response["results"]]
    if case.get("measure_only"):
        return []
    missing = ["missing " + ref for ref in case.get("expected_refs", []) if ref not in refs]
    missing += ["excluded " + ref for ref in case.get("excluded_refs", []) if ref in refs]
    if response["no_match"] != bool(case.get("expected_no_match", False)):
        missing.append("no_match differs")
    if case.get("expected_first") and (not refs or refs[0] != case["expected_first"]):
        missing.append("first result differs")
    if "expected_ambiguity" in case and bool(response.get("ambiguity", {}).get("detected")) != case["expected_ambiguity"]:
        missing.append("ambiguity differs")
    if case.get("expected_recheck") and any(not any(card["ref"] == ref and card.get("needs_recheck") for card in response["results"])
            for ref in case.get("expected_refs", [])):
        missing.append("freshness warning differs")
    if case.get("expected_exact_set") and set(refs) != set(case["expected_exact_set"]):
        missing.append("filtered result set differs")
    return missing


def worker_main(request):
    from richi import memory, recall, output, graph
    database = Path(request["database"])
    if request["action"] == "seed":
        database.parent.mkdir(parents=True)
        (database.parent / "config.json").write_text("{}\n", encoding="utf-8")
        data, revisions = copy.deepcopy(request["corpus"]), request.get("revisions", 0)
        target = None
        if revisions:
            target = copy.deepcopy(next(row for row in data["entry"] if row["id"] == "task:DEMO-84"))
            next(row for row in data["entry"] if row["id"] == target["id"])["summary"] += " Historical draft 0; archivedgrowthnote."
        with closing(memory.connect(database, create=True)) as conn:
            memory.initialize(conn, database)
            for family in ("project", "entry", "entity", "edge"):
                for start in range(0, len(data[family]), 250):
                    with memory.transaction(conn):
                        for row in data[family][start:start + 250]:
                            if family == "project": memory.project_put(conn, row)
                            elif family == "entry": memory.entry_put(conn, row)
                            else: graph.put(conn, row, family, memory)
            for index in range(1, revisions + 1):
                row = copy.deepcopy(target)
                if index < revisions: row["summary"] += " Historical draft %d; archivedgrowthnote." % index
                with memory.transaction(conn): memory.entry_put(conn, row)
        return {"physical": physical(database), "logical_sha256": logical(database)}
    if request["action"] == "writes":
        before = physical(database)
        rows, durations = [], {"metadata": [], "text": []}
        with closing(memory.connect(database)) as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM entries ORDER BY id LIMIT 20")]
            for kind in durations:
                for identity in ids:
                    payload = memory.entry_get(conn, identity)
                    payload["expected_updated_at"] = payload.pop("updated_at")
                    payload.pop("created_at")
                    if kind == "metadata": payload["verified_at"] = "2026-10-24"
                    else: payload["summary"] += " Additional synthetic measurement; original scope still applies."
                    started = time.perf_counter_ns()
                    with memory.transaction(conn): memory.entry_put(conn, payload)
                    durations[kind].append((time.perf_counter_ns() - started) / 1e6)
            for row in conn.execute("SELECT id FROM entries ORDER BY id"):
                value = memory.entry_get(conn, row[0])
                value.pop("created_at"); value.pop("updated_at")
                rows.append(value)
            active_wal = physical(database)
        return {"latency": {key: summary(value) for key, value in durations.items()},
                "before": before, "active_wal": active_wal, "after_close": physical(database),
                "current_semantic_sha256": digest(rows), "updates_per_kind": len(ids)}
    before, physical_before = logical(database), physical(database)
    rows = []
    with closing(memory.connect(database, readonly=True)) as conn, output.json_mode(compact=True):
        for case in request["cases"]:
            args = memory.parser().parse_args(arguments(case))
            response = recall.recall(conn, args, memory)
            raw = output.rendered(response)
            if len(raw) > args.max_chars or response["budget"]["output_chars"] != len(raw):
                raise AssertionError("Invalid normal output budget: " + case["id"])
            samples = []
            for _ in range(request.get("repeats", 1)):
                started = time.perf_counter_ns()
                repeated = recall.recall(conn, args, memory)
                samples.append((time.perf_counter_ns() - started) / 1e6)
                if repeated != response: raise AssertionError("Unstable response: " + case["id"])
            trace_args = memory.parser().parse_args(arguments(case, explain=True))
            explained = recall.recall(conn, trace_args, memory)
            rows.append({"id": case["id"], "response": response, "raw": raw,
                "quality_misses": quality(case, response), "latency": summary(samples),
                "trace_counts": explained.get("explain", {}).get("counts", {}),
                "trace_selection": explained.get("selection", {})})
    code = {}
    for path in sorted(Path(memory.__file__).parent.glob("*.py")):
        code[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"cases": rows, "logical_unchanged": logical(database) == before,
            "physical_unchanged": physical(database) == physical_before,
            "runtime": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "code_sha256": code}}


def compare(dataset, cases, root, args, *, revisions=0, repeats=None, candidate_first=False):
    baseline, candidate = root / "baseline" / "memory.sqlite3", root / "candidate" / "memory.sqlite3"
    seed = worker(args.baseline_python, root, {"action": "seed", "database": str(baseline),
                                             "corpus": dataset, "revisions": revisions})
    clone(baseline, candidate)
    before = physical(candidate)
    _, built, build_ms = cli(args.candidate_python, candidate, ["search-index", "build"])
    index_preserved = logical(candidate) == seed["logical_sha256"]
    arm_reports = {}
    order = ("candidate", "baseline") if candidate_first else ("baseline", "candidate")
    for arm in order:
        arm_reports[arm] = worker(getattr(args, arm + "_python"), root, {"action": "measure",
            "database": str(baseline if arm == "baseline" else candidate), "cases": cases,
            "repeats": args.engine_repeats if repeats is None else repeats})
    paired = []
    for old, new in zip(arm_reports["baseline"]["cases"], arm_reports["candidate"]["cases"]):
        equal = old["response"] == new["response"]
        row = {"id": old["id"], "normal_response_equal": equal}
        for arm, value in (("baseline", old), ("candidate", new)):
            row[arm] = {key: value[key] for key in ("quality_misses", "latency", "trace_counts", "trace_selection")}
            row[arm].update(response_sha256=digest(value["response"]), output_chars=len(value["raw"]),
                            returned_refs=[card["ref"] for card in value["response"]["results"]])
        if not equal: row["difference"] = {"baseline": old["response"], "candidate": new["response"]}
        paired.append(row)
    report = {"entries": len(dataset["entry"]), "entities": len(dataset["entity"]), "corpus_sha256": digest(dataset),
              "engine_arm_order": list(order),
              "index_build": {"wall_ms": build_ms, "timing_scope": "complete CLI process including status/serialization",
                              "size_scope": "post-close main/WAL bytes; not peak build WAL", "report": built, "before": before,
                              "after": physical(candidate), "canonical_rows_preserved": index_preserved},
              "runtime": {arm: value["runtime"] for arm, value in arm_reports.items()},
              "read_only": {arm: {key: value[key] for key in ("logical_unchanged", "physical_unchanged")}
                            for arm, value in arm_reports.items()}, "cases": paired}
    return report, arm_reports, baseline, candidate


def tokens(python, messages):
    if python is None: return {"status": "not_measured"}
    cache = os.environ.get("TIKTOKEN_CACHE_DIR")
    expected_cache = ("9b5ad71b2ce5302211f9c61530b329a4922fc6a4", "fb374d419588a4632f3f557e76b4b70aebbca790")
    if not cache or not all((Path(cache) / name).is_file() for name in expected_cache):
        raise ValueError("Set TIKTOKEN_CACHE_DIR to the prepopulated cl100k/o200k cache; network downloads are not part of this benchmark")
    code = """import json,sys,tiktoken,importlib.metadata,tiktoken.load
# A corrupt/missing cache must fail rather than download a replacement.
tiktoken.load.read_file=lambda path: (_ for _ in ()).throw(RuntimeError('tokenizer cache unavailable'))
messages=json.load(sys.stdin)
result={'version':importlib.metadata.version('tiktoken'),'method':'sum independent response encodings','counts':{}}
for name in ('o200k_base','cl100k_base'):
 e=tiktoken.get_encoding(name)
 result['counts'][name]={key:sum(len(e.encode(text,disallowed_special=())) for text in texts) for key,texts in messages.items()}
print(json.dumps(result))
"""
    proc = subprocess.run([str(python), "-I", "-c", code], input=json.dumps(messages), text=True,
                          capture_output=True, timeout=90)
    if proc.returncode: raise RuntimeError(proc.stderr)
    return json.loads(proc.stdout)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--worker":
        print(json.dumps(worker_main(json.load(sys.stdin)), ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path, required=True)
    parser.add_argument("--baseline-revision", default="b8a2a9aa889a7d65d433e985ca3f6a9d4cec4c60",
                        help="Declared source revision used for the fixed baseline installation")
    parser.add_argument("--candidate-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--engine-repeats", type=int, default=20)
    parser.add_argument("--cli-repeats", type=int, default=5)
    parser.add_argument("--tokenizer-python", type=Path)
    parser.add_argument("--legacy-fixture", type=Path)
    parser.add_argument("--skip-legacy", action="store_true", help="Smoke runs only; full verification includes all seven old configurations")
    args = parser.parse_args()
    if args.output.exists() or any(n < 100 for n in args.sizes) or not 1 <= args.engine_repeats <= 100 or not 1 <= args.cli_repeats <= 30:
        parser.error("Use a new output file, sizes >=100, 1..100 engine repeats and 1..30 CLI repeats")
    acceptance_bytes = (HERE / "recall_cases.json").read_bytes()
    acceptance = json.loads(acceptance_bytes)
    legacy_bytes = None
    if not args.skip_legacy:
        legacy_bytes = (args.legacy_fixture.read_bytes() if args.legacy_fixture else subprocess.check_output(
            ["git", "-C", str(HERE.parent), "show", LEGACY_COMMIT + ":evals/memory_growth_cases.json"]))
        if hashlib.sha256(legacy_bytes).hexdigest() != LEGACY_SHA256:
            raise ValueError("Legacy fixture must match the frozen ff966 source bytes")
    report = {"format_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {"sizes": args.sizes, "engine_repeats": args.engine_repeats,
                          "cli_repeats": args.cli_repeats, "legacy_included": not args.skip_legacy},
        "provenance": {"declared_baseline_revision": args.baseline_revision, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "acceptance_sha256": hashlib.sha256(acceptance_bytes).hexdigest(),
                       "legacy_fixture_sha256": LEGACY_SHA256 if legacy_bytes else None},
        "scale": [], "legacy": [], "filters": [], "limitations": [
            "Synthetic acceptance and tool contracts, not blind agent quality or production accuracy.",
            "Unfiltered normal JSON must remain exactly equal; expected tokens do not decrease.",
            "Engine timing excludes process startup; fresh CLI timing shares OS caches and is not a cold-cache test.",
            "Filter workflow uses fixed required refs; compact filtered output and broad overfetch have different selection scope.",
            "Token totals sum independent tool responses; no billed context, reasoning, framing or caching is modeled.",
            "Legacy target misses are quality observations, not silently waived or fixed by faster execution.",
            "Index build and write costs are real tradeoffs; latency is never a CI pass threshold."]}
    messages = {}
    with tempfile.TemporaryDirectory(prefix="richi-indexed-eval-") as temporary:
        root = Path(temporary)
        for scale_index, size in enumerate(args.sizes):
            data, cases = corpus(size, acceptance)
            measured, arms, old_db, new_db = compare(data, cases, root / ("scale-%d" % size), args,
                                                    candidate_first=bool(scale_index % 2))
            cli_before = {arm: (logical(database), physical(database))
                          for arm, database in (("baseline", old_db), ("candidate", new_db))}
            measured["fresh_cli"] = {}
            for case in [cases[0], next(c for c in cases if c["id"] == "unicode-folded-alias"), cases[-1]]:
                latencies = {"baseline": [], "candidate": []}
                for index in range(args.cli_repeats):
                    for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
                        raw, response, elapsed = cli(getattr(args, arm + "_python"), old_db if arm == "baseline" else new_db, arguments(case))
                        expected = next(row["response"] for row in arms[arm]["cases"] if row["id"] == case["id"])
                        if response != expected: raise AssertionError("CLI/engine response mismatch: " + case["id"])
                        latencies[arm].append(elapsed)
                measured["fresh_cli"][case["id"]] = {arm: summary(values) for arm, values in latencies.items()}
            measured["fresh_cli_read_only"] = {arm: {"logical_unchanged": logical(database) == cli_before[arm][0],
                "physical_unchanged": physical(database) == cli_before[arm][1]}
                for arm, database in (("baseline", old_db), ("candidate", new_db))}
            measured["write_cost"] = {}
            for arm, database in (("baseline", old_db), ("candidate", new_db)):
                copy_db = database.parent.parent / ("writes-" + arm) / "memory.sqlite3"
                clone(database, copy_db)
                measured["write_cost"][arm] = worker(getattr(args, arm + "_python"), root,
                    {"action": "writes", "database": str(copy_db)})
                messages["unfiltered-%d-%s" % (size, arm)] = [row["raw"] for row in arms[arm]["cases"]]
            measured["writes_same_current_semantics"] = (measured["write_cost"]["baseline"]["current_semantic_sha256"] ==
                                                         measured["write_cost"]["candidate"]["current_semantic_sha256"])
            report["scale"].append(measured)
        for crowded in (False, True):
            data, cases, wanted = filter_corpus(crowded)
            location = root / ("crowded" if crowded else "workflow")
            old_db, new_db = location / "baseline" / "memory.sqlite3", location / "candidate" / "memory.sqlite3"
            worker(args.baseline_python, location, {"action": "seed", "database": str(old_db), "corpus": data})
            clone(old_db, new_db)
            cli(args.candidate_python, new_db, ["search-index", "build"])
            reads_before = {arm: (logical(database), physical(database))
                            for arm, database in (("baseline", old_db), ("candidate", new_db))}
            candidate = worker(args.candidate_python, location, {"action": "measure", "database": str(new_db),
                "cases": cases, "repeats": args.engine_repeats})
            overfetch = {"id": "overfetch", "query": "filterneedle", "limit": 200, "max_chars": 100000,
                         "measure_only": True}
            raw, baseline, _ = cli(args.baseline_python, old_db, arguments(overfetch))
            baseline_cards = {card["ref"]: card for card in baseline["results"] if card["ref"] in wanted}
            rows = []
            for row in candidate["cases"]:
                selected = {card["ref"]: card for card in row["response"]["results"]}
                same = (set(baseline_cards) == set(wanted) == set(selected) and
                        all(selected[ref]["sources"] == baseline_cards[ref]["sources"] for ref in wanted))
                rows.append({"id": row["id"], "quality_misses": row["quality_misses"], "latency": row["latency"],
                    "returned_refs": list(selected), "baseline_overfetch_same_required_refs_and_sources": same,
                    "output_chars": len(row["raw"]), "trace_counts": row["trace_counts"]})
            if not crowded:
                # Existing search already has strict project scope. Both arms use
                # it, the same limit and full-record output; only predicate differs.
                search_args = ["search", "filterneedle", "--project", "atlas", "--limit", "200"]
                old_raw, old_search, _ = cli(args.baseline_python, old_db, search_args)
                new_raw, new_search, _ = cli(args.candidate_python, new_db, search_args + ["--where",
                    "(kind = 'decision' OR kind = 'experiment') AND NOT work_state = 'planned' AND tags HAS 'selected' AND verified_at >= '2026-10-01'"])
                old_selected = {"entry:" + row["id"]: row for row in old_search["entries"] if "entry:" + row["id"] in wanted}
                new_selected = {"entry:" + row["id"]: row for row in new_search["entries"]}
                equal = set(old_selected) == set(wanted) == set(new_selected) and old_selected == new_selected
                report["filter_workflow"] = {"endpoint": "search", "project": "atlas", "limit": 200,
                    "baseline_full_records": len(old_search["entries"]), "candidate_full_records": len(new_search["entries"]),
                    "required_refs": wanted, "required_full_records_equal": equal,
                    "baseline_chars": len(old_raw), "candidate_chars": len(new_raw),
                    "selection": "fixed required refs; baseline client-filtered after full-record overfetch; existing strict --project used in both arms"}
                if equal:
                    messages["workflow_baseline_overfetch"] = [old_raw]
                    messages["workflow_candidate_filter"] = [new_raw]
            all_reads = {arm: {"logical_unchanged": logical(database) == reads_before[arm][0],
                "physical_unchanged": physical(database) == reads_before[arm][1]}
                for arm, database in (("baseline", old_db), ("candidate", new_db))}
            report["filters"].append({"crowded": crowded, "entries": len(data["entry"]), "required_refs": wanted,
                "baseline_overfetch_chars": len(raw), "baseline_returned_required_refs": list(baseline_cards),
                "baseline_candidates": baseline["candidate_count"], "read_only": all_reads, "cases": rows})
        if legacy_bytes:
            legacy = json.loads(legacy_bytes)
            for legacy_index, (background, details, revisions) in enumerate(((100,0,0),(1000,0,0),(10000,0,0),(100,10,0),(100,100,0),(100,0,10),(100,0,100))):
                name = "b%d-d%d-h%d" % (background, details, revisions)
                measured, _, _, _ = compare(legacy_corpus(legacy, background, details), legacy["cases"],
                    root / ("legacy-" + name), args, revisions=revisions, repeats=1, candidate_first=bool(legacy_index % 2))
                measured["id"] = name
                report["legacy"].append(measured)
        report["tokens"] = tokens(args.tokenizer_python, messages)
    datasets = report["scale"] + report["legacy"]
    report["summary"] = {"normal_pairs": sum(len(row["cases"]) for row in datasets),
        "normal_parity_failures": sum(not case["normal_response_equal"] for row in datasets for case in row["cases"]),
        "quality_misses": {arm: sum(bool(case[arm]["quality_misses"]) for row in datasets for case in row["cases"])
                           for arm in ("baseline", "candidate")},
        "scale_quality_misses": {arm: sum(bool(case[arm]["quality_misses"]) for row in report["scale"] for case in row["cases"])
                                 for arm in ("baseline", "candidate")},
        "legacy_quality_misses": {arm: sum(bool(case[arm]["quality_misses"]) for row in report["legacy"] for case in row["cases"])
                                  for arm in ("baseline", "candidate")},
        "workflow_mismatch": not report["filter_workflow"]["required_full_records_equal"],
        "filter_read_mutations": sum(not value for row in report["filters"] for arm in row["read_only"].values() for value in arm.values()),
        "fresh_cli_mutations": sum(not value for row in report["scale"] for arm in row["fresh_cli_read_only"].values() for value in arm.values()),
        "filter_quality_misses": sum(bool(case["quality_misses"]) for row in report["filters"] for case in row["cases"]),
        "read_mutations": sum(not value for row in datasets for arm in row["read_only"].values() for value in arm.values()),
        "build_mutations": sum(not row["index_build"]["canonical_rows_preserved"] for row in datasets),
        "write_semantics_mismatch": sum(not row["writes_same_current_semantics"] for row in report["scale"])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"report": str(args.output), "summary": report["summary"]}))
    return int(any(report["summary"][key] for key in ("normal_parity_failures", "filter_quality_misses",
                   "read_mutations", "build_mutations", "write_semantics_mismatch", "filter_read_mutations", "fresh_cli_mutations", "workflow_mismatch"))
                   or any(report["summary"]["scale_quality_misses"].values()))


if __name__ == "__main__":
    sys.exit(main())
