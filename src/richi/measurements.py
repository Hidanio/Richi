"""Quantitative observations travel with the source that supplies their calculation."""
import hashlib
import heapq
import json
import re
import sqlite3


FIELDS = ("metric", "value", "unit", "sample")
NARRATIVE_FIELDS = ("metric", "value", "sample")
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


# A single comma or period retains its decimal meaning. Mixed punctuation
# identifies the decimal separator, and repeated dots alone may be a version.
_SPACES = " \u00a0\u202f"
_NUMBER_TRANSLATION = str.maketrans("−", "-", _SPACES)
_MANTISSA = (r"[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]+|"
             r"[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]+|"
             r"[0-9]{1,3}(?:,[0-9]{3}){2,}|"
             rf"[0-9]{{1,3}}(?:[{_SPACES}][0-9]{{3}})+(?:[.,][0-9]+)?|"
             r"[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+")
_NUMBER = r"[+\-−]?(?:" + _MANTISSA + r")(?:e[+\-−]?[0-9]+)?"
_SYMBOL = r"[%‰]|°[cf]?"
_UNIT = r"[^\W\d_]+(?:/[^\W\d_]+)?|" + _SYMBOL
_QUANTITY = re.compile("(?P<number>" + _NUMBER + ")(?P<unit>" + _UNIT + ")?", re.IGNORECASE)
_SPACED_UNIT = re.compile(r"\s+(" + _UNIT + r")(?!\w|[-.:/]\w)", re.IGNORECASE)
# Consume identifiers as a whole, including v0.5, p95, EX-5 and 1.2.3.
_TOKEN = re.compile(_QUANTITY.pattern + r"(?!\w|[-.:/]\w)|" + _SYMBOL + r"|[^\W_]+(?:[-_.:/][^\W_]+)*", re.IGNORECASE)
# In a reported range these words connect or qualify values without supplying
# their unit. For example, both ends of "87 to 92" take the row's declared unit.
_RANGE_WORDS = frozenset("to through versus vs and or before after then compared followed per "
                         "до против и или от после затем".split())


def _description_after(text, end):
    """A word a preposition can introduce, as in '5 in July'."""
    from . import recall
    word = _SPACED_UNIT.match(text, end)
    if word is None:
        return False
    folded = word[1].casefold()
    return folded not in _RANGE_WORDS and folded not in recall.STOPWORDS


def _connector(word, text, end):
    """Inside prose a preposition can separate terms. At an endpoint it can be a unit (in, с)."""
    from . import recall
    folded = word.casefold()
    if folded in _RANGE_WORDS:
        return _TOKEN.search(text, end) is not None
    return folded in recall.STOPWORDS and not (len(word) == 1 and word.isupper()) and _description_after(text, end)


def _tokens(text):
    """Yield each lexical token or complete quantity with its unit and source end."""
    consumed = 0
    for token in _TOKEN.finditer(text):
        if token.start() < consumed:
            continue
        unit, end = token["unit"] or "", token.end()
        if token["number"] and not unit:
            spaced = _SPACED_UNIT.match(text, end)
            if spaced and not _connector(spaced[1], text, spaced.end()):
                unit, end = spaced[1], spaced.end()
        consumed = end
        yield token, unit, end


# A prefixed alphabetic unit like Mbar can look exactly like a capitalized word.
# This distinction is only for bare lexical queries, never qualified quantities.
_BARE_UNIT_BASES = {"bar", "bit", "bps", "byte", "kat", "lux", "mol", "rad"}


def _symbol_case(word):
    return any(c.isupper() for c in word[1:]) and (not word.isupper() or len(word) <= 3)


def lexical_text(text, spellings):
    """Keep ordinary words for prefix search; case-bearing symbols have a separate exact match."""
    if not re.search("[0-9]", text):
        return text
    parts, start = [], 0
    for token, unit, end in _tokens(text):
        # In unstructured prose a word after a count can describe its sample.
        # Preserve such words, including uppercase headings. Reserve a visibly
        # cased abbreviation (MW, mW, MB/s) for literal symbol matching.
        if not unit:
            continue
        # Also recognize the distinction from the question. For example, Mbps
        # alone looks like a capitalized word, but MBps distinguishes bits
        # from bytes. A differing prefix on the same base distinguishes Mbar
        # from mbar without suppressing ordinary titlecase words like Warm.
        folded = unit.casefold()
        case_variant = any(
            word != unit and word.casefold() == folded and
            ((len(word) <= 5 and _symbol_case(word)) or folded[1:] in _BARE_UNIT_BASES)
            for word in spellings)
        if token["unit"] or len(unit) <= 2 or not unit.isalpha() or _symbol_case(unit) or case_variant:
            parts.append(text[start:end - len(unit)])
            start = end
    return " ".join([*parts, text[start:]]) if parts else text


def query_terms(text, unit_starts=None):
    from . import recall
    terms = {}
    rate_end = 0
    for token, unit, _ in _tokens(text):
        if token["number"]:
            if token.start() < rate_end:
                continue  # A scale such as "per 100 requests" belongs to its numerator.
            word = token["number"] + unit
            start = token.end("number")
            if text[start:start + 1] == " ":
                start += 1
            rate, rate_end = _rate_at(text, start)
            if unit_starts is not None and (word in terms or len(terms) < 64):
                unit_starts.setdefault(_number(token["number"]), []).append((start, word, rate))
        else:
            word = token[0]
            folded = recall.normalize(word)
            # A standalone symbol such as W or A can identify the declared unit.
            single_symbol = len(word) == 1 and (word.isupper() or word == text.strip())
            if folded in recall.STOPWORDS and not single_symbol:
                continue
        terms[word] = None
    return list(terms)[:64]


def _number(value):
    value = value.lower().translate(_NUMBER_TRANSLATION).lstrip("+").replace("e+", "e")
    if "," in value and "." in value:
        grouping = "," if value.rfind(",") < value.rfind(".") else "."
        value = value.replace(grouping, "")
    elif value.count(",") > 1:
        value = value.replace(",", "")
    value = value.replace(",", ".")
    return re.sub(r"^(-?)\.", r"\g<1>0.", value)


_UNIT_CONTINUATION = re.compile(r"\w|[-.:/]\w|\s+per\s+[0-9]", re.IGNORECASE)
_RATE_SEPARATOR = re.compile(r"(?:\s+per\s+|\s*/\s*)(?=[^\W\d_]|[%‰°])", re.IGNORECASE)
_UNIT_RATE_SEPARATOR = re.compile(r"(?<=[^\W\d_]|[%‰°])" + _RATE_SEPARATOR.pattern, re.IGNORECASE)
_PER_WORD = re.compile(r"\bper\b", re.IGNORECASE)
_RATE = re.compile(r"(?P<numerator>[^\W\d_]+(?:\s+[^\W\d_]+)*?)"
                   + r"(?:\s+per\s+(?P<scale>" + _NUMBER + r")\s+|" + _RATE_SEPARATOR.pattern + r")"
                   + r"(?P<denominator>[^\W\d_]+)", re.IGNORECASE)


def _rate_at(text, start):
    """Read a written numerator and denominator without crossing into the next claim."""
    from . import recall
    rate = _RATE.match(text, start)
    if rate and not any(part.casefold() in recall.STOPWORDS or part.casefold() in _RANGE_WORDS
                        for part in rate["numerator"].split()[1:]):
        separator = " per " + rate["scale"] + " " if rate["scale"] else "/"
        return rate["numerator"] + separator + rate["denominator"], rate.end()
    return "", start


def _phrase_at(text, start, unit):
    """Match the full unit. 'RPCs' is a total; 'RPCs per second' and 'RPCs/second' are rates."""
    for index, part in enumerate(unit.split("/")):
        if index:
            separator = _RATE_SEPARATOR.match(text, start)
            if separator is None:
                return False
            start = separator.end()
        if not text.startswith(part, start):
            return False
        start += len(part)
    return not _UNIT_CONTINUATION.match(text, start) and not _RATE_SEPARATOR.match(text, start)


class MeasurementQuery:
    def __init__(self, text):
        from . import recall
        self.text = " ".join(text.split())
        self.unit_starts = {}
        self.terms = query_terms(self.text, self.unit_starts)
        self.words, self.unit_words, self.numbers = set(), {}, {}
        for term in self.terms:
            quantity = _QUANTITY.fullmatch(term)
            if quantity:
                key = (_number(quantity["number"]), quantity["unit"] or "")
                self.numbers.setdefault(key, set()).add(term)
            else:
                folded = recall.normalize(term)
                self.unit_words[term] = folded
                if len(term) > 1 and folded not in recall.STOPWORDS:
                    self.words.add(folded)

    def lookup_terms(self):
        # All published prefix indexes have the unsigned leading digits. A
        # leading decimal point (".5") can also stand for a zero integer part.
        result = set(self.unit_words.values())
        for number, _ in self.numbers:
            digits = re.findall("[0-9]+", number)
            result.add(digits[0])
            if len(digits[0]) > 3:
                # Published indexes also have a prefix at the last group of a
                # written thousands group, e.g. "234" in both 1,234 and 1.234.
                result.add(digits[0][-3:])
            if number.lstrip("-").startswith("0."):
                result.add(digits[1])
        return result

    def word_hits(self, narrative, units=()):
        from . import recall
        normalized = recall.normalize(narrative)
        found = {term for term in self.words if recall.matches_term(term, normalized)}
        for unit in units:
            for spelling, folded in self.unit_words.items():
                if unit == spelling or (len(spelling) > 1 and recall.matches_term(spelling, unit)):
                    found.add(folded)
        return found

    def matched_quantity_units(self, hits):
        return {unit for (_, unit), terms in self.numbers.items() if unit and not terms.isdisjoint(hits)}

    def has_quantity_hit(self, hits):
        return any(not terms.isdisjoint(hits) for terms in self.numbers.values())

    def _declared_phrase_hits(self, number, unit):
        """Bind the full recorded unit, such as 'completed RPCs', to its query number."""
        for start, term, required_rate in self.unit_starts.get(number, ()):
            if (not required_rate or unit.startswith(required_rate)) and _phrase_at(self.text, start, unit):
                yield term

    def quantity_hits(self, row, fields=FIELDS):
        """Match recorded quantities and unit symbols, including the MW part of 5MW."""
        declared_unit = row["unit"].strip()
        declared_phrase = _UNIT_RATE_SEPARATOR.sub("/", " ".join(declared_unit.split())) if self.numbers else ""
        unit_hits = {declared_unit} if "unit" in fields and declared_unit in self.unit_words else set()
        numbers = set()
        for field in fields:
            text = row[field]
            if not self.numbers and not (any(unit in text for unit in self.unit_words) and re.search("[0-9]", text)):
                continue
            if self.numbers:
                # Retain "100 per mode" as a sample count; normalize separators after a written unit.
                text = _UNIT_RATE_SEPARATOR.sub("/", text)
            has_rate = self.numbers and ("/" in text or _PER_WORD.search(text))
            rate_end = _rate_at(text, 0)[1] if field == "unit" and has_rate else 0
            for token, unit, end in _tokens(text):
                literal = token["number"]
                if literal is None or token.start() < rate_end:
                    continue
                if unit in self.unit_words:
                    unit_hits.add(unit)
                if self.numbers:
                    number = _number(literal)
                    start = end - len(unit)
                    rate = ""
                    if unit and has_rate:
                        rate, rate_end = _rate_at(text, start)
                        unit = rate or unit
                    written = set(self._declared_phrase_hits(number, unit)) if unit else set()
                    if field == "value" and unit and declared_phrase.startswith(unit + " "):
                        # A shared first word cannot establish the full written unit.
                        written = ()
                        if _phrase_at(text, start, declared_phrase):
                            unit = declared_phrase
                    numbers.update(written)
                    if written and rate:
                        unit_hits.add(rate)
                    if not token["unit"] or unit == declared_unit:
                        numbers.update(self.numbers.get((number, ""), ()))
                    # The declaration fills a missing unit only. In "5 ms → 1 s"
                    # a declaration of "s" cannot turn the first value into 5 s.
                    if field == "value" and (not unit or unit == declared_phrase):
                        phrase_hits = set(self._declared_phrase_hits(number, declared_phrase))
                        if phrase_hits:
                            numbers.update(phrase_hits)
                            if " " in declared_phrase:
                                unit_hits.update(declared_phrase.split())
                            if "/" in declared_phrase:
                                unit_hits.add(declared_phrase)
        return numbers | {self.unit_words[unit] for unit in unit_hits if unit in self.unit_words}, unit_hits


def attach(card, sources, query):
    """Choose observations across *all* sources, including ones outside the source excerpt."""
    count = sum(len(source.get("measurements", ())) for source in sources)
    if not count:
        return
    def matches():
        for index, source in enumerate(sources, 1):
            for row in source.get("measurements", ()):
                narrative = " ".join(lexical_text(row[field], query.unit_words) for field in NARRATIVE_FIELDS) if query.words else ""
                words = query.word_hits(narrative, (row["unit"].strip(),))
                numbers, units = query.quantity_hits(row)
                metric_words = query.word_hits(lexical_text(row["metric"], query.unit_words)) if query.words else set()
                metric_numbers, _ = query.quantity_hits(row, ("metric",))
                yield (query.has_quantity_hit(numbers), len(words | numbers), len(units),
                       len(metric_words | metric_numbers)), index, row

    card["measurements"] = []
    for _, index, row in heapq.nlargest(BRIEF_LIMIT, matches(), key=lambda item: item[0]):
        citation = {k: v for k, v in sources[index - 1].items() if k != "measurements"}
        card["measurements"].append(dict(row, source_index=index, source=citation))
    card["measurements_omitted"] = count - len(card["measurements"])
