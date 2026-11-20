"""Quantitative observations travel with the source that supplies their calculation."""
import hashlib
import heapq
import json
import re
import sqlite3


FIELDS = ("metric", "value", "unit", "sample")
BRIEF_LIMIT = 8
KEY_PREFIX = "source-measurements:v1:"
INLINE_PREDICATE = """instr(sources, '"measurements"') > 0"""


def index_inline_sources(conn):
    """Locate sources produced by older development writers without indexing their text."""
    for table in ("entries", "entities", "graph_edges"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            conn.execute("CREATE INDEX IF NOT EXISTS richi_inline_measurements_" + table + " ON " + table
                         + " (knowledge_state, id) WHERE " + INLINE_PREDICATE)


INFO_PREFIX = "source-measurements-info:v1:"
HISTORY = {"entry": ("entry_history", "entry_id"), "entity": ("entity_history", "object_id"),
           "edge": ("edge_history", "object_id")}


def validate(rows):
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("source.measurements must be an array of at most 100 observations")
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("Each measurement requires metric, value, unit and sample")
        for field in FIELDS:
            text = row[field]
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise ValueError("measurement." + field + " must be a nonempty string")


def mapping(sources):
    return {str(index): source["measurements"] for index, source in enumerate(sources) if "measurements" in source}


def headers(sources):
    return [{key: value for key, value in source.items() if key != "measurements"} for source in sources]


def _digest(citations):
    encoded = json.dumps(citations, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def store(conn, family, record, history_id):
    """Keep the source column writable by older releases, in the same transaction as history."""
    ref = family + ":" + record["id"]
    rows = mapping(record["sources"])
    if rows:
        version = {"updated_at": record["updated_at"], "history_id": history_id,
                   "headers_sha256": _digest(headers(record["sources"]))}
        info = dict(version, counts={index: len(items) for index, items in rows.items()})
        conn.executemany("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                         [(prefix + ref, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
                          for prefix, value in ((KEY_PREFIX, dict(version, rows=rows)), (INFO_PREFIX, info))])
    else:
        conn.execute("DELETE FROM metadata WHERE key IN (?, ?)", (KEY_PREFIX + ref, INFO_PREFIX + ref))


def separate_inline(conn, family, record, dumps):
    """An unchanged full put may come from a development store with inline annotations."""
    if not mapping(record["sources"]):
        return
    table = {"entry": "entries", "entity": "entities", "edge": "graph_edges"}[family]
    changed = conn.execute("UPDATE " + table + " SET sources = ? WHERE id = ? AND sources = ?",
                           (dumps(headers(record["sources"])), record["id"], dumps(record["sources"])))
    if changed.rowcount:
        history_table, owner = HISTORY[family]
        history = conn.execute("SELECT max(id) FROM " + history_table + " WHERE " + owner + " = ?",
                               (record["id"],)).fetchone()
        store(conn, family, record, history[0])


def _matching_info(raw, leading):
    # An intermediate writer can replace observations without updating the
    # compact companion. The revision leads every value store() writes.
    if raw:
        info = json.loads(raw)
        if leading.startswith('{"updated_at":' + json.dumps(info["updated_at"]) + ','):
            return info


def _info(conn, ref):
    key = KEY_PREFIX + ref
    row = conn.execute("SELECT i.value, substr(m.value,1,80) FROM metadata m LEFT JOIN metadata i ON i.key = ? "
                       "WHERE m.key = ?", (INFO_PREFIX + ref, key)).fetchone()
    if row is None:
        return None
    info = _matching_info(*row)
    if info:
        return info
    # The first development data format has no count row. Project only the
    # small version fields and array lengths where SQLite supports JSON.
    try:
        row = conn.execute("SELECT json_remove(value, '$.rows') FROM metadata WHERE key = ?", (key,)).fetchone()
        saved = json.loads(row[0])
        saved["counts"] = dict(conn.execute("SELECT j.key, json_array_length(j.value) FROM metadata m, "
                                            "json_each(m.value, '$.rows') j WHERE m.key = ?", (key,)))
    except sqlite3.OperationalError as exc:
        if "no such " not in str(exc):
            raise
        raw = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()[0]
        saved = json.loads(raw)
        saved["counts"] = {index: len(rows) for index, rows in saved.pop("rows").items()}
    return saved


def read_cost(conn, family, identity, updated_at, remaining):
    """Count stored annotation and history characters before fetch transfers the record."""
    ref = family + ":" + identity
    row = conn.execute("SELECT length(m.value), i.value, substr(m.value,1,80) FROM metadata m "
                       "LEFT JOIN metadata i ON i.key = ? WHERE m.key = ?", (INFO_PREFIX + ref, KEY_PREFIX + ref)).fetchone()
    if row is None:
        return 0
    chars = row[0]
    saved = _matching_info(row[1], row[2])
    if saved is None and chars > remaining:
        return chars  # Older sidecar layout. Avoid decoding it for the probe.
    saved = saved or _info(conn, ref)
    if saved["updated_at"] == updated_at:
        return chars
    table = {"entry": "entries", "entity": "entities", "edge": "graph_edges"}[family]
    sources = json.loads(conn.execute("SELECT sources FROM " + table + " WHERE id = ?", (identity,)).fetchone()[0])
    if _digest(sources) != saved["headers_sha256"]:
        return 0  # The old writer replaced the citations; a full get omits this sidecar.
    table, owner = HISTORY[family]
    history_chars = conn.execute("SELECT coalesce(sum(length(after_json)), 0) FROM " + table + " WHERE " + owner +
                                 " = ? AND id > ?", (identity, saved["history_id"])).fetchone()[0]
    return chars + history_chars


def _still_current(conn, family, record, saved):
    if saved["updated_at"] == record["updated_at"]:
        return True
    # An old writer retains evidence through a title edit. A removed citation
    # must not revive old observations if another edit adds the citation back.
    citations = record["sources"]
    if _digest(citations) != saved["headers_sha256"]:
        return False
    table, owner = HISTORY[family]
    suffix = " FROM " + table + " WHERE " + owner + " = ? AND id > ? ORDER BY id"
    params = (record["id"], saved["history_id"])
    try:
        # Transfer only the citation data, irrespective of historic summary length.
        changes = conn.execute("SELECT changed_at, json_extract(after_json, '$.sources')" + suffix, params)
        revisions = ((stamp, json.loads(raw)) for stamp, raw in changes)
    except sqlite3.OperationalError as exc:
        if "no such function" not in str(exc):
            raise
        changes = conn.execute("SELECT changed_at, after_json" + suffix, params)
        revisions = ((stamp, json.loads(raw)["sources"]) for stamp, raw in changes)
    last = None
    for last, previous in revisions:
        if previous != citations:
            return False
    return last == record["updated_at"]


def restore(conn, family, record, *, counts_only=False):
    """Enrich current headers, transferring full observations only for full records."""
    if not record["sources"]:
        return
    ref = family + ":" + record["id"]
    saved = _info(conn, ref)
    if saved is None or not _still_current(conn, family, record, saved):
        return  # Earlier development records with inline observations remain readable.
    if counts_only:
        for index, count in saved["counts"].items():
            if count:
                record["sources"][int(index)]["measurements_omitted"] = count
    else:
        raw = conn.execute("SELECT value FROM metadata WHERE key = ?", (KEY_PREFIX + ref,)).fetchone()[0]
        for index, rows in json.loads(raw)["rows"].items():
            record["sources"][int(index)]["measurements"] = rows


_NUMBER = r"[+\-−]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)(?:e[+\-−]?[0-9]+)?"
_QUANTITY = re.compile("(?P<number>" + _NUMBER + r")(?P<unit>[^\W\d_]+(?:/[^\W\d_]+)?|[%‰]|°[cf]?)?", re.IGNORECASE)
# Consume identifiers as a whole, including v0.5, p95, EX-5 and 1.2.3.
_TOKEN = re.compile(_QUANTITY.pattern + r"(?!\w|[-.:/]\w)|[^\W_]+(?:[-_.:/][^\W_]+)*", re.IGNORECASE)


def query_terms(text):
    from . import recall
    words = (token[0] for token in _TOKEN.finditer(text)
             if token["number"] is not None or len(token[0]) > 1 and token[0] not in recall.STOPWORDS)
    return list(dict.fromkeys(words))[:64]


def _number(value):
    value = value.replace(",", ".").replace("−", "-").lstrip("+").replace("e+", "e")
    return re.sub(r"^(-?)\.", r"\g<1>0.", value)


class MeasurementQuery:
    def __init__(self, terms):
        self.words, self.numbers = [], {}
        for term in terms:
            quantity = _QUANTITY.fullmatch(term)
            if quantity:
                key = (_number(quantity["number"]), quantity["unit"] or "")
                self.numbers.setdefault(key, set()).add(term)
            else:
                self.words.append(term)

    def lookup_terms(self):
        # All published prefix indexes have the unsigned leading digits. A
        # leading decimal point (".5") can also stand for a zero integer part.
        result = set(self.words)
        for number, _ in self.numbers:
            digits = re.findall("[0-9]+", number)
            result.add(digits[0])
            if number.lstrip("-").startswith("0."):
                result.add(digits[1])
        return result

    def word_hits(self, text):
        from . import recall
        return {term for term in self.words if recall.matches_term(term, text)}

    def number_hits(self, row, fields=FIELDS):
        from . import recall
        found = set()
        if self.numbers:
            declared_unit = recall.normalize(row["unit"]).strip()
            for field in fields:
                for token in _TOKEN.finditer(recall.normalize(row[field])):
                    if token["number"] is None:
                        continue
                    number = _number(token["number"])
                    unit = token["unit"] or ""
                    found.update(self.numbers.get((number, unit), ()))
                    if unit == declared_unit:
                        found.update(self.numbers.get((number, ""), ()))
                    elif field == "value" and not unit:
                        found.update(self.numbers.get((number, declared_unit), ()))
        return found


def attach(card, sources, terms):
    """Choose observations across *all* sources, including ones outside the source excerpt."""
    from . import recall
    count = sum(len(source.get("measurements", ())) for source in sources)
    if not count:
        return
    query = MeasurementQuery(terms)
    def matches():
        for index, source in enumerate(sources, 1):
            for row in source.get("measurements", ()):
                words = recall.normalize(" ".join(row[field] for field in FIELDS))
                hits = query.word_hits(words) | query.number_hits(row)
                metric = recall.normalize(row["metric"])
                metric_hits = query.word_hits(metric) | query.number_hits(row, ("metric",))
                yield (len(hits), len(metric_hits)), index, row

    card["measurements"] = []
    for _, index, row in heapq.nlargest(BRIEF_LIMIT, matches(), key=lambda item: item[0]):
        citation = {k: v for k, v in sources[index - 1].items() if k != "measurements"}
        card["measurements"].append(dict(row, source_index=index, source=citation))
    card["measurements_omitted"] = count - len(card["measurements"])
