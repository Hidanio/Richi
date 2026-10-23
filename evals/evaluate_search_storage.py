#!/usr/bin/env python3
"""Measure bounded retrieval and storage maintenance using synthetic stores only.

No user database argument is accepted. Timings are local observations, not CI
thresholds. Fixed expected records simulate a correct follow-up selection; this
is a tool-contract experiment, not a blind agent-quality evaluation.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time


def environment(root):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("RICHI_", "PYTHON")) and k != "CODEX_THREAD_ID"}
    home = root / "synthetic-home"
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"))
    return env


def cli(database, *args, payload=None):
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "richi", "--config",
         str(database.parent / "config.json"), "--db", str(database),
         "--compact-json", *args],
        input=None if payload is None else json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=90,
        env=environment(database.parent))
    elapsed = (time.perf_counter() - started) * 1000
    if result.returncode or result.stderr:
        raise ValueError(result.stderr or result.stdout)
    return result.stdout, json.loads(result.stdout), elapsed


def write(database, family, records):
    return cli(database, family, "upsert" if family == "project" else "put",
               "--json", "-", payload=records)


def initialize(root, name):
    directory = root / name
    directory.mkdir()
    (directory / "config.json").write_text("{}\n", encoding="utf-8")
    db = directory / "synthetic.sqlite3"
    cli(db, "init")
    write(db, "project", {"id": "example", "name": "Synthetic example",
                           "source": "fixture://project"})
    return db


def record(identity, *, large=False):
    context = "The synthetic rollout uses a checked maintenance window. "
    return {"id": identity, "kind": "decision", "title": "Rollout policy " + identity,
            "summary": context * (110 if large else 35)
                       + "Only enable after validation. Exception: cold restores require a manual review.",
            "knowledge_state": "confirmed", "work_state": "done",
            "sources": [{"reference": "https://example.test/evidence/" + identity,
                         "label": "Synthetic evidence. " * (100 if large else 5)}],
            "project_ids": ["example"], "tags": ["rollout"],
            "aliases": ["rollout-policy"], "verified_at": "2026-10-23"}


def logical_fingerprint(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        data = {}
        for table in ("metadata", "projects", "entries", "entry_projects", "relations",
                      "entry_history", "entry_events", "entities", "entity_history",
                      "graph_nodes", "graph_edges", "edge_history"):
            data[table] = conn.execute('SELECT rowid,* FROM "' + table + '" ORDER BY rowid').fetchall()
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def physical_fingerprint(database):
    return {suffix: hashlib.sha256(Path(str(database) + suffix).read_bytes()).hexdigest()
            for suffix in ("", "-wal")
            if Path(str(database) + suffix).exists() and Path(str(database) + suffix).stat().st_size}


def wire(raw):
    return {"chars": len(raw), "utf8_bytes": len(raw.encode()),
            "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def token_counts(raw_outputs, tokenizer_python):
    if tokenizer_python is None:
        return {"status": "not_measured"}
    code = """import importlib.metadata,json,sys,tiktoken
texts=json.load(sys.stdin)
out={'version':importlib.metadata.version('tiktoken'),'counts':{}}
for name in ('o200k_base','cl100k_base'):
 enc=tiktoken.get_encoding(name)
 counts={key:len(enc.encode(value)) for key,value in texts.items()
         if key not in ('individual_gets','baseline_workflow','candidate_workflow')}
 if 'recall' in counts:
  counts['individual_gets']=sum(value for key,value in counts.items() if key.startswith('get_response_'))
  counts['baseline_workflow']=counts['recall']+counts['individual_gets']
  counts['candidate_workflow']=counts['discover']+counts['fetch']
 out['counts'][name]=counts
out['measurement']='Each actual tool response encoded separately; workflow counts are sums.'
print(json.dumps(out))
"""
    proc = subprocess.run([str(tokenizer_python), "-I", "-c", code],
                          input=json.dumps(raw_outputs), capture_output=True, text=True, timeout=90)
    if proc.returncode:
        raise ValueError(proc.stderr)
    return json.loads(proc.stdout)


def run(root, repeats):
    db = initialize(root, "retrieval")
    write(db, "entry", [record("policy-" + str(i)) for i in range(8)])
    logical_before = logical_fingerprint(db)
    before = physical_fingerprint(db)
    recall_raw, recall, _ = cli(db, "recall", "rollout-policy", "--limit", "8")
    discover_raw, discover, _ = cli(db, "discover", "rollout-policy", "--limit", "8")
    # Deliberately an oracle follow-up: this does not test an agent's selection.
    selected = [item["ref"] for item in recall["results"][:3]]
    assert len(selected) == 3
    assert set(selected) <= {item["ref"] for item in discover["results"]}
    arguments = [value for ref in selected for value in ("--ref", ref)]
    get_outputs = [cli(db, "entry", "get", ref.split(":", 1)[1]) for ref in selected]
    fetch_raw, fetched, _ = cli(db, "fetch", *arguments, "--max-chars", "32000")
    # Match public command contract without assuming dictionary field order.
    outcomes = fetched.get("results", fetched.get("records", []))
    fetched_records = {item["ref"]: item.get("record") for item in outcomes if item.get("record") is not None}
    assert fetched_records == {ref: row[1] for ref, row in zip(selected, get_outputs)}, fetched
    latency = {"individual_gets": [], "batch_fetch": []}
    for i in range(repeats):
        for variant in (("individual_gets", "batch_fetch") if i % 2 == 0
                        else ("batch_fetch", "individual_gets")):
            if variant == "individual_gets":
                duration = sum(cli(db, "entry", "get", ref.split(":", 1)[1])[2] for ref in selected)
            else:
                duration = cli(db, "fetch", *arguments, "--max-chars", "32000")[2]
            latency[variant].append(round(duration, 3))
    assert logical_fingerprint(db) == logical_before
    assert physical_fingerprint(db) == before
    raw = {"recall": recall_raw, "discover": discover_raw,
           "individual_gets": "".join(item[0] for item in get_outputs), "fetch": fetch_raw}
    raw.update({"get_response_" + str(index): item[0] for index, item in enumerate(get_outputs)})
    raw["baseline_workflow"] = raw["recall"] + raw["individual_gets"]
    raw["candidate_workflow"] = raw["discover"] + raw["fetch"]
    retrieval = {"oracle_selected_refs": selected, "read_database_unchanged": True,
                 "full_records_equal": True, "workflow_tool_calls": {"baseline": 4, "candidate": 2},
                 "wire": {key: wire(value) for key, value in raw.items()},
                 "fetch_latency_ms": {key: {"samples": values, "median": statistics.median(values)}
                                      for key, values in latency.items()}}

    graph_db = initialize(root, "graph")
    write(graph_db, "entry", [record("large-" + str(i), large=True) for i in range(30)])
    write(graph_db, "edge", [{"id": "link-" + str(i), "from_ref": "entry:large-0",
                              "to_ref": "entry:large-" + str(i), "kind": "informs",
                              "description": "Synthetic, directed supporting detail.",
                              "sources": [{"reference": "https://example.test/edge/" + str(i)}],
                              "knowledge_state": "confirmed", "verified_at": "2026-10-23"}
                             for i in range(1, 30)])
    before = physical_fingerprint(graph_db)
    graph_raw, full, _ = cli(graph_db, "graph", "neighbors", "entry:large-0", "--depth", "2", "--limit", "30")
    small_raw, small, _ = cli(graph_db, "graph", "neighbors", "entry:large-0", "--depth", "2",
                            "--limit", "30", "--view", "compact", "--max-chars", "16000")
    assert len(small_raw) <= 16000 and small["budget"]["output_chars"] == len(small_raw)
    assert physical_fingerprint(graph_db) == before
    full_ids = {node["id"] for node in full["nodes"]}
    shown = {node["id"] for node in small["nodes"]}
    assert "entry:large-0" in shown and shown <= full_ids
    assert all(edge["from_ref"] in shown and edge["to_ref"] in shown for edge in small["edges"])
    assert len(small_raw) < len(graph_raw) * 0.3
    raw.update(graph_full=graph_raw, graph_compact=small_raw)
    graph = {"full": wire(graph_raw), "compact": wire(small_raw),
             "full_nodes": len(full_ids), "shown_nodes": len(shown),
             "declared_budget": small["budget"], "read_database_unchanged": True,
             "root_retained": True, "no_dangling_edges": True}

    growth_db = initialize(root, "growth")
    write(growth_db, "entry", [record("growth-" + str(i)) for i in range(100)])
    growth = []
    # Keep final current content identical at the even checkpoints.
    for index in range(101):
        if index:
            _, current, _ = cli(growth_db, "entry", "get", "growth-0")
            current["expected_updated_at"] = current.pop("updated_at")
            current.pop("created_at")
            current["work_state"] = "implemented" if index % 2 else "done"
            write(growth_db, "entry", current)
        if index in (0, 10, 100):
            _, stats, _ = cli(growth_db, "storage", "stats", "--deep", "--max-chars", "32000")
            growth.append({"additional_revisions": index, "storage": stats})
    before = logical_fingerprint(growth_db)
    _, recall_before, _ = cli(growth_db, "recall", "growth-0", "--max-chars", "16000")
    _, maintenance, _ = cli(growth_db, "storage", "optimize", "--apply", "--max-chars", "32000")
    _, after_stats, _ = cli(growth_db, "storage", "stats", "--deep", "--max-chars", "32000")
    _, recall_after, _ = cli(growth_db, "recall", "growth-0", "--max-chars", "16000")
    assert logical_fingerprint(growth_db) == before
    assert recall_after == recall_before
    storage = {"growth_snapshots": growth, "maintenance": maintenance,
               "after_maintenance": after_stats, "all_logical_rows_and_rowids_unchanged": True,
               "recall_unchanged": True}
    return {"retrieval": retrieval, "graph": graph, "storage": storage}, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--tokenizer-python", type=Path)
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.repeats <= 30:
        parser.error("Use a new output file and 1..30 repeats")
    # Fail before the experiment if the optional tokenizer/cache is unavailable.
    token_counts({}, args.tokenizer_python)
    import richi
    package = Path(richi.__file__).parent
    code_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(package.glob("*.py"))}
    code_hashes["evals/evaluate_search_storage.py"] = hashlib.sha256(
        Path(__file__).read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="richi-search-storage-eval-") as directory:
        results, raw = run(Path(directory), args.repeats)
        # Reports are public fixtures; retain scope without host-specific paths.
        results = json.loads(json.dumps(results, ensure_ascii=False).replace(
            str(Path(directory)), "<synthetic-root>"))
    report = {"format_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "platform": platform.system() + " " + platform.release(),
              "installed_code_sha256": code_hashes,
              "results": results, "tokens": token_counts(raw, args.tokenizer_python),
              "limitations": [
                  "Synthetic tool contracts and oracle-selected follow-up records, not blind agent quality.",
                  "Token counts cover returned tool text only, not billed model context or reasoning.",
                  "Fresh CLI processes share OS caches; timings are local observations, not SLA.",
                  "Compact graph deliberately omits content; follow exact record pointers for conclusions.",
                  "Index optimization preserves history; free pages need not shrink the database file."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"report": str(args.output), "invariants": "passed"}))


if __name__ == "__main__":
    main()
