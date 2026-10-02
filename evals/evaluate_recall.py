#!/usr/bin/env python3
"""Run public recall acceptance cases against a fresh, disposable database."""

import argparse
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent


def cli(database, *args, payload=None, timeout=30):
    """Every invocation has an explicit database; user configuration is never read."""
    result = subprocess.run(
        [sys.executable, "-B", "-m", "richi", "--config", str(database.parent / "config.json"),
         "--db", str(database), *args],
        input=None if payload is None else json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        env={key: value for key, value in os.environ.items() if not key.startswith("RICHI_")},
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Richi returned an error")
    return result.stdout, json.loads(result.stdout)


def validate(document):
    catalog = {family + ":" + row["id"]: row
               for family in ("entry", "entity") for row in document.get(family, [])}
    cases = document.get("cases", [])
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Provide nonempty cases with unique IDs")
    for case in cases:
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError("Empty query: " + case["id"])
        expected = case.get("expected_refs", [])
        if bool(expected) == bool(case.get("expected_no_match", False)):
            raise ValueError("Specify expected_refs OR expected_no_match: " + case["id"])
        for ref in expected + case.get("excluded_refs", []):
            if ref not in catalog:
                raise ValueError("Unknown expected reference: " + ref)
        if case.get("expected_first") and case["expected_first"] not in expected:
            raise ValueError("expected_first must be an expected reference: " + case["id"])
    return catalog


def evaluate_case(database, case, catalog, args):
    command = ["recall", case["query"], "--max-chars", str(args.max_chars), "--limit", "8"]
    if case.get("project"):
        command += ["--project", case["project"]]
    started = time.monotonic()
    row = {"id": case["id"], "query": case["query"], "failures": []}
    try:
        raw, response = cli(database, *command, timeout=args.timeout)
        results = response.get("results")
        if not isinstance(results, list):
            raise ValueError("Recall did not return a results array")
        refs = [item["ref"] for item in results]
        row.update(returned_refs=refs, output_chars=len(raw), no_match=response.get("no_match"))
        checks = {
            "expected results in top five": set(case.get("expected_refs", [])) <= set(refs[:5]),
            "excluded results absent": not set(case.get("excluded_refs", [])) & set(refs),
            "no-match expectation": response.get("no_match") is bool(case.get("expected_no_match", False)),
            "serialized character budget": len(raw) <= args.max_chars,
            "declared character count": response.get("budget", {}).get("output_chars") == len(raw),
            "source provenance": all(
                item.get("sources") and item["ref"] in catalog
                and all(source in catalog[item["ref"]]["sources"] for source in item["sources"])
                for item in results),
            "knowledge states": all(item["ref"] in catalog and item.get("knowledge_state")
                                    == catalog[item["ref"]]["knowledge_state"] for item in results),
            "work states": all(item.get("work_state") == catalog.get(item["ref"], {}).get("work_state")
                               for item in results if item["ref"].startswith("entry:")),
        }
        if case.get("expected_first"):
            checks["first result"] = bool(refs) and refs[0] == case["expected_first"]
        if "expected_ambiguity" in case:
            checks["concept ambiguity"] = response.get("ambiguity", {}).get("detected", False) is case["expected_ambiguity"]
        if case.get("expected_recheck"):
            checks["freshness warning"] = all(
                any(item["ref"] == ref and item.get("needs_recheck") is True for item in results)
                for ref in case["expected_refs"])
        row["failures"] = [name for name, passed in checks.items() if not passed]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        row["failures"].append(str(exc))
    row.update(passed=not row["failures"], elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=HERE / "recall_cases.json",
                        help="Public fixture document including seeds and fixed expectations")
    parser.add_argument("--max-chars", type=int, default=16000)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, help="Optional new JSON report; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.max_chars < 2000 or args.timeout <= 0:
        parser.error("Use max-chars >= 2000 and a positive timeout")
    try:
        if args.output and args.output.exists():
            raise ValueError("Refusing to overwrite an existing report")
        fixture_bytes = args.cases.read_bytes()
        document = json.loads(fixture_bytes)
        catalog = validate(document)
        code_hashes = {name: hashlib.sha256(files("richi").joinpath(name).read_bytes()).hexdigest()
                       for name in ("memory.py", "recall.py", "compact.py")}
        with tempfile.TemporaryDirectory(prefix="richi-public-eval-") as temporary:
            database = Path(temporary) / "synthetic.sqlite3"
            (database.parent / "config.json").write_text("{}\n", encoding="utf-8")
            cli(database, "init", timeout=args.timeout)
            for family in ("project", "entry", "entity", "edge"):
                if document.get(family):
                    cli(database, family, "upsert" if family == "project" else "put", "--json", "-",
                        payload=document[family], timeout=args.timeout)
            before = database.read_bytes()
            rows = [evaluate_case(database, case, catalog, args) for case in document["cases"]]
            unchanged = database.read_bytes() == before
        passed = sum(row["passed"] for row in rows)
        report = {
            "schema_version": 1, "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "code_sha256": code_hashes, "database_unchanged_by_recall": unchanged,
            "summary": {"cases": len(rows), "passed": passed, "failed": len(rows) - passed},
            "cases": rows,
            "limitations": ["Synthetic acceptance cases, not a blind benchmark or a production accuracy estimate.",
                            "Sources are fictional example.test references; no external evidence is fetched.",
                            "Each run seeds an explicit disposable database and never reads the user's memory."],
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        print(encoded, end="")
        return 0 if passed == len(rows) and unchanged else 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
