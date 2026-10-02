"""Bounded reading views. Excerpts are verbatim evidence, never new summaries."""
import argparse
import copy
import json
import re


EXCERPT_NOTE = "Partial verbatim excerpt; limitations may be omitted. Read the full record before drawing a conclusion."
CAUTION = re.compile(r"\b(?:not|no|never|only|failed|without|limit\w*|caveat\w*|unverified|historical|unknown|не|нет|только|без|огранич\w*|неудач\w*|историчес\w*|неподтверж\w*|неизвест\w*)\b", re.I)


def max_chars(value):
    number = int(value)
    if not 2000 <= number <= 100000:
        raise argparse.ArgumentTypeError("max-chars must be between 2000 and 100000")
    return number


def rendered(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def excerpt(text, limit):
    if len(text) <= limit:
        return text, False
    if not limit:
        return "", True
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?!\d)|\n+", text) if s.strip()]
    if not sentences:
        return "", bool(text)
    # Keep complete sentences. Leading context, explicit cautions and final outcome
    # have priority; omitted ranges remain visibly separated.
    eligible = [i for i, sentence in enumerate(sentences) if sentence != "[…]"]
    if not eligible:
        return "", True
    priorities = [eligible[0]] + [i for i in eligible if CAUTION.search(sentences[i])]
    priorities += [eligible[-1]] + eligible
    chosen = set()
    def join(indices):
        pieces, previous = [], -1
        for index in sorted(indices):
            if index != previous + 1:
                pieces.append("[…]")
            pieces.append(sentences[index])
            previous = index
        if previous != len(sentences) - 1:
            pieces.append("[…]")
        return "\n".join(pieces)
    for index in priorities:
        if index not in chosen and len(join(chosen | {index})) <= limit:
            chosen.add(index)
    return join(chosen) if chosen else "", True


def card(record, ref, summary_chars=1100):
    summary, partial = excerpt(record.get("summary", ""), summary_chars)
    title = record.get("title", record.get("name", ""))
    sources = copy.deepcopy(record.get("sources", []))
    projects = record.get("project_ids", [])
    result = {"id": record["id"], "ref": ref, "kind": record.get("kind", "project"),
              "title": title[:300] + ("…" if len(title) > 300 else ""),
              "summary_excerpt": summary, "is_excerpt": partial,
              "summary_chars": len(record.get("summary", "")),
              "knowledge_state": record.get("knowledge_state"), "work_state": record.get("work_state"),
              "verified_at": record.get("verified_at"), "updated_at": record.get("updated_at"),
              "project_ids": projects[:8], "sources": sources[:2], "source_count": len(sources),
              "sources_omitted": max(0, len(sources) - 2),
              "read_more": {"command": ref.split(":", 1)[0] + " get", "id": record["id"]}}
    if partial:
        result["excerpt_note"] = EXCERPT_NOTE
    if len(title) > 300:
        result["title_truncated"] = True
    if len(projects) > 8:
        result["project_ids_omitted"] = len(projects) - 8
    return result


def fit_response(response, maximum):
    """Bound the actual JSON stdout, keeping original source objects intact.

    Over-budget cards are omitted rather than presenting a chopped source URL or
    pretending an excerpt is a complete record. No-match concerns retrieval, not
    whether a matching record could be represented under the requested budget.
    """
    value = copy.deepcopy(response)
    original_results = len(value.get("results", []))
    original_edges = len(value.get("edges", []))
    original_entry = value.get("entry") is not None
    original_omitted = value.get("omitted_results", max(0, value.get("candidate_count", original_results) - original_results))
    original_ambiguity = copy.deepcopy(value.get("ambiguity"))
    value["budget"] = {"max_chars": maximum, "output_chars": 0, "omitted_results": 0, "omitted_edges": 0}
    value["truncated"] = bool(value.get("truncated") or original_omitted)

    def cards():
        return value.get("results", []) + ([value["entry"]] if value.get("entry") else [])

    def measure():
        value["budget"]["omitted_results"] = original_omitted + original_results - len(value.get("results", []))
        value["budget"]["omitted_edges"] = original_edges - len(value.get("edges", []))
        value["truncated"] = bool(value["truncated"] or any(c.get("is_excerpt") or c.get("sources_omitted") or c.get("title_truncated") or c.get("project_ids_omitted") for c in cards()))
        if value.get("ambiguity"):
            ambiguity = value["ambiguity"]
            ambiguity["omitted_candidates"] = ambiguity["concept_count"] - len(ambiguity["candidates"])
            value["truncated"] = bool(value["truncated"] or ambiguity["omitted_candidates"] or
                                      ambiguity.get("aliases_truncated") or
                                      any(c.get("title_truncated") or c.get("project_ids_omitted")
                                          for c in ambiguity["candidates"]))
        for _ in range(8):
            size = len(rendered(value))
            if value["budget"]["output_chars"] == size:
                return size
            value["budget"]["output_chars"] = size
        return len(rendered(value))

    if measure() <= maximum:
        return value
    value["truncated"] = True
    # First shorten excerpts. Evidence objects and IDs are not chopped.
    for length in (650, 300):
        for item in cards():
            text = item.get("summary_excerpt", "")
            if len(text) > length:
                item["summary_excerpt"], _ = excerpt(text, length)
                item["is_excerpt"] = True
                item["excerpt_note"] = EXCERPT_NOTE
        if measure() <= maximum:
            return value
    for item in cards():
        if len(item.get("sources", [])) > 1:
            item["sources"] = item["sources"][:1]
            item["sources_omitted"] = item.get("source_count", 1) - 1
    # Ambiguity survives even when the requested result limit is one. Extra
    # alternatives can be omitted explicitly without choosing a single meaning.
    while len(value.get("ambiguity", {}).get("candidates", [])) > 2 and measure() > maximum:
        value["ambiguity"]["candidates"].pop()
    while value.get("edges") and measure() > maximum:
        value["edges"].pop()
    while len(value.get("results", [])) > 1 and measure() > maximum:
        value["results"].pop()
    while value.get("ambiguity", {}).get("candidates") and measure() > maximum:
        value["ambiguity"]["candidates"].pop()
    if measure() <= maximum:
        return value
    for item in cards():
        if item.get("summary_excerpt"):
            item["summary_excerpt"] = ""
            item["is_excerpt"] = True
            item["excerpt_note"] = EXCERPT_NOTE
    if measure() <= maximum:
        return value
    # The remaining metadata/source itself is too large. Preserve an honest
    # retrieval outcome and an exact command for targeted full reading.
    read_more = [c["read_more"] for c in cards() if c.get("read_more")][:1]
    candidate_count = value.get("candidate_count", original_results or int(original_entry))
    value = {"results": [], "edges": [], "candidate_count": candidate_count,
             "no_match": response.get("no_match", candidate_count == 0), "truncated": True,
             "notice": "Matching data could not fit this budget; increase --max-chars or read the record directly.",
             "read_more": read_more,
             "budget": {"max_chars": maximum, "output_chars": 0,
                        "omitted_results": original_omitted + original_results, "omitted_edges": original_edges}}
    if original_entry:
        value["entry"] = None
        value["entry_omitted"] = True
    if original_ambiguity:
        value["ambiguity"] = {"detected": True, "concept_count": original_ambiguity["concept_count"],
                              "candidates": [], "omitted_candidates": original_ambiguity["concept_count"],
                              "note": "Several recorded meanings exist; ambiguity details could not fit this budget."}
    for _ in range(8):
        size = len(rendered(value))
        if value["budget"]["output_chars"] == size:
            break
        value["budget"]["output_chars"] = size
    return value
