"""Bounded, explainable recall over existing records; no writes or network calls."""

import json
import re

from . import compact


STOPWORDS = set("""
а без бы был была были было быть в вам вас ваш ваша ваши весь во вот все всего всех
вы где да для до его ее её если есть еще ещё же за зачем здесь и из или им их к
как какая какие какой каких когда кто ли либо мы на над нам нас наш наша наши не
него нее неё нет но ну о об он она они оно от по под при про с со так такая такие
такой там те тем то того тоже только том тут ты у уже чего чем через что чтобы это
эта эти этот я мне меня мой можно нужно надо почему сколько ли делали делалось
the a an and or of to in on at by for from with as is are was were be been being
this that these those it its we our us you your they their them i my me do does did
have has had can could should would will what which when where who why how about
any some all ever before please tell know explain whether tried trying try
такое такой такую этой этом этим сделали сделано сделать делает делают делать
работает работают работал работали работать использует используют использовали
использовать обрабатывает обрабатывают обрабатывали обрабатывать означает означают
означало означать происходит происходят происходило происходить расскажи объясни
покажи описать покажи показать устроен устроена устроено устроены
пробовал пробовала пробовали пробовать пробуем пробуют попробовали попробовал
попытка попытки попытку попыток пытались пытался попытались попытаться
проверяли проверял проверяла проверили проверил проверила проверять проверить
исследовали исследовал исследовала исследовать исследуем исследуют
тестировали тестировал тестировала тестировать тестируем тестируют
use uses used using work works worked working handle handles handled handling
process processes processed processing mean means meant happen happens happened
make makes made making doing done describe show shows shown get gets got
attempt attempts attempted attempting test tested testing investigate investigated
investigating check checked checking
""".split())
JIRA = re.compile(r"(?<![\w])([A-Za-z][A-Za-z0-9_]{1,20}-\d+|[A-Za-z][A-Za-z_]{1,20}\d+)(?![\w])")
WORDS = re.compile(r"[^\W_]+(?:[-_.:/][^\W_]+)*", re.UNICODE)
CURRENT = re.compile(
    r"\b(?:сейчас|сегодня|текущ\w*|актуальн\w*|последн\w*|замерж\w*|выпущен\w*|"
    r"развернут\w*|влито|current|currently|latest|now|today|deployed|released|merged)\b|"
    r"\b(?:в|на)\s+(?:проде|production)\b", re.IGNORECASE)
CANDIDATE_LIMIT = 160


class RecallTrace:
    """Observe this retrieval pass; never run another search to infer a cause."""

    def __init__(self, conn, expected_ref, states, api):
        self.counts = {"fts_rows_fetched": 0, "fts_shortlisted": 0,
                       "entries_scanned": 0, "entities_scanned": 0,
                       "direct_rejected": {}, "direct_candidates": 0,
                       "graph_seeds": 0, "graph_links_fetched": 0,
                       "graph_links_examined": 0, "graph_rejected": {},
                       "graph_candidates": 0, "candidates": 0, "selected": 0}
        self.expected = None
        self.selected = {}
        self.fts_truncated = self.graph_truncated = False
        if expected_ref is not None:
            expected_ref = api.string(expected_ref, "expect")
            family, _, identity = expected_ref.partition(":")
            if family not in {"entry", "entity"} or not identity:
                raise api.MemoryError("expect must be a full entry:<id> or entity:<id> reference")
            api.identifier(identity, "expect id")
            table = "entries" if family == "entry" else "entities"
            # One exact metadata lookup distinguishes missing from state-filtered.
            # It does not score the record or query FTS a second time.
            row = (conn.execute("SELECT knowledge_state FROM " + table + " WHERE 1=1 AND id = ?",
                                (identity,)).fetchone() if table_exists(conn, table) else None)
            state = row[0] if row else None
            self.expected = {"ref": expected_ref, "knowledge_state": state,
                             "direct_stage": "missing" if row is None else
                             "filtered_state" if state not in states else "not_evaluated",
                             "graph_stage": "not_reached"}

    def direct(self, node_ref, stage):
        if stage != "candidate":
            rejected = self.counts["direct_rejected"]
            rejected[stage] = rejected.get(stage, 0) + 1
        if self.expected and node_ref == self.expected["ref"]:
            self.expected["direct_stage"] = stage

    def graph(self, node_ref, stage):
        if stage != "candidate":
            rejected = self.counts["graph_rejected"]
            rejected[stage] = rejected.get(stage, 0) + 1
        if (self.expected and node_ref == self.expected["ref"] and
                self.expected["graph_stage"] != "candidate"):
            self.expected["graph_stage"] = stage

    def selection(self, direct, graph, chosen, fts_truncated, graph_truncated):
        self.fts_truncated, self.graph_truncated = fts_truncated, graph_truncated
        self.counts.update(direct_candidates=len(direct), graph_candidates=len(graph),
                           candidates=len({item["ref"] for item in direct} | set(graph)),
                           selected=len(chosen))
        self.selected = {item["ref"]: index + 1 for index, item in enumerate(chosen)}
        if self.expected:
            ranks = {item["ref"]: index + 1 for index, item in enumerate(direct)}
            self.expected["direct_rank"] = ranks.get(self.expected["ref"])

    def view(self, response, detail=0):
        returned = {item["ref"]: index + 1 for index, item in enumerate(response["results"])}
        counts = dict(self.counts, returned=len(returned),
                      budget_omitted=len(self.selected.keys() - returned.keys()))
        value = {"counts": counts, "fts_shortlist_truncated": self.fts_truncated,
                 "graph_links_truncated": self.graph_truncated}
        if detail:
            keep = {"direct_candidates", "graph_candidates", "candidates", "selected", "returned", "budget_omitted"}
            value["counts"] = {key: count for key, count in counts.items() if key in keep}
            value["details_omitted_for_budget"] = True
        if detail > 1:
            value.pop("counts")
        if self.expected:
            expected = dict(self.expected)
            node_ref = expected["ref"]
            expected["selected_position"] = self.selected.get(node_ref)
            expected["returned_position"] = returned.get(node_ref)
            if node_ref in returned:
                outcome = "returned"
            elif node_ref in self.selected:
                outcome = "budget"
            elif expected["direct_stage"] == "candidate" or expected["graph_stage"] == "candidate":
                outcome = "rank_or_limit"
            else:
                outcome = expected["direct_stage"]
            expected["outcome"] = outcome
            value["expected"] = expected
        return value


def fit_explained_response(response, maximum, trace):
    """Reserve bounded space for observed diagnostics, then report actual omissions.

    Selection/scoring are already complete. Only the presentation budget changes
    when diagnostics are requested. Compact's fallback may drop arbitrary extra
    fields, so attach the explanation after fitting rather than losing it there.
    """
    ordinary = compact.fit_response(response, maximum)

    def attach(value, detail):
        value["explain"] = trace.view(value, detail)
        value["budget"]["max_chars"] = maximum
        for _ in range(8):
            size = len(compact.rendered(value))
            if value["budget"]["output_chars"] == size:
                break
            value["budget"]["output_chars"] = size
        return value

    # Keep exactly the ordinary cards if the complete explanation already fits.
    if len(compact.rendered(attach(ordinary, 0))) <= maximum:
        return ordinary
    for detail in range(3):
        diagnostic = trace.view(ordinary, detail)
        # Allow for nested indentation and the few digits/positions that change
        # after fitting. The final serialized size, not this estimate, is binding.
        reserve = len(compact.rendered({"explain": diagnostic})) + 160
        value = attach(compact.fit_response(response, max(0, maximum - reserve)), detail)
        if len(compact.rendered(value)) <= maximum:
            return value
    # The legal ref length bounds the remaining expected metadata. A tiny
    # fallback can therefore retain an honest outcome even for huge source URLs.
    value = {"results": [], "edges": [], "candidate_count": response["candidate_count"],
             "no_match": response["no_match"], "truncated": True,
             "budget": {"max_chars": maximum, "output_chars": 0,
                        "omitted_results": response["candidate_count"],
                        "omitted_edges": len(response["edges"])}}
    if response.get("ambiguity"):
        count = response["ambiguity"]["concept_count"]
        value["ambiguity"] = {"detected": True, "concept_count": count,
                              "candidates": [], "omitted_candidates": count}
    return attach(value, 2)


def normalize(value):
    return str(value or "").casefold().replace("ё", "е")


def phrase(value):
    return " ".join(WORDS.findall(normalize(value)))


def terms_for(query):
    # An issue key is one routing key, never an OR search for a generic prefix.
    keys = sorted({canonical_key(match.group(1)) for match in JIRA.finditer(query)})
    remainder = JIRA.sub(" ", query)
    terms = []
    for term in WORDS.findall(normalize(remainder)):
        if term not in STOPWORDS and len(term) > 1 and term not in terms:
            terms.append(term)
    return terms[:64], keys


def canonical_key(value):
    value = value.casefold()
    if "-" in value:
        return value
    match = re.fullmatch(r"([a-z][a-z_]+)(\d+)", value)
    return match.group(1) + "-" + match.group(2) if match else value


def key_variants(key):
    # Keep the numeric suffix intact: ABC-17 / ABC17 never matches ABC-170.
    return (key, key.replace("-", ""))


def decode(row, arrays=("sources", "tags", "aliases")):
    result = dict(row)
    for field in arrays:
        result[field] = json.loads(result[field])
    return result


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE 1=1 AND type = 'table' AND name = ?", (name,)).fetchone() is not None


def matches_key(record, keys):
    if not keys:
        return True
    texts = [record.get(field, "") for field in ("id", "jira_key", "title", "summary")]
    texts += record.get("aliases", [])
    return any(re.search(r"(?<![\w])" + re.escape(variant) + r"(?![\w])", normalize(text))
               for key in keys for variant in key_variants(key) for text in texts)


def matches_term(term, text):
    # A meaningful term must start at a lexical boundary. Prefix matching keeps
    # the same recall affordance as FTS without accepting arbitrary infixes.
    return bool(re.search(r"(?<![^\W_])" + re.escape(term), text))


def technical_subjects(query):
    # Explicit names/identifiers provide a stronger anchor than question verbs:
    # mixed-case names, acronyms, qualified names and version-bearing tokens.
    result = []
    for raw in WORDS.findall(JIRA.sub(" ", query)):
        term = normalize(raw)
        if term in STOPWORDS or len(term) < 2 or not re.search(r"[A-Za-z]", raw):
            continue
        if re.search(r"[A-Z0-9_.:/-]", raw) and term not in result:
            result.append(term)
    return result


def matches_subject(record, subjects):
    if not subjects:
        return True
    texts = [record.get(field, "") for field in ("id", "title", "summary")]
    texts += record.get("aliases", []) + record.get("tags", [])
    return any(matches_term(subject, normalize(text)) for subject in subjects for text in texts)


def short_subjects(query):
    # Symbols qualify meaningful lexical matches; never use them as FTS prefixes.
    return {term for term in WORDS.findall(normalize(query))
            if len(term) == 1 and term.isalpha() and term not in STOPWORDS}


def matches_short_subjects(record, subjects):
    # A symbol can qualify a longer question through an existing factual text.
    # This guard never adds lexical score: bare Y / "what is Y" still need a
    # deliberate alias and cannot retrieve every incidental single-letter use.
    texts = [normalize(record.get(field, "")) for field in ("title", "summary")]
    texts += [normalize(alias) for alias in record.get("aliases", [])]
    return all(any(re.search(r"(?<![\w])" + re.escape(subject) + r"(?![\w])", text)
                   for text in texts) for subject in subjects)


def concept_alias_matches(record, query):
    if record.get("kind") != "concept":
        return []
    query_phrase = " " + phrase(query) + " "
    # Exact token/phrase boundaries distinguish Y from Yandex and BIT from BITSET.
    return sorted({alias for value in record.get("aliases", [])
                   if (alias := phrase(value)) and (" " + alias + " ") in query_phrase
                   and (alias not in STOPWORDS or phrase(query) == alias)})


def concept_projects(conn, node_ref):
    """Only a sourced, confirmed outgoing used_in edge defines concept scope."""
    if not table_exists(conn, "graph_edges"):
        return []
    rows = conn.execute("""
        SELECT p.id, e.sources FROM graph_edges e
            JOIN projects p ON e.to_ref = 'project:' || p.id
        WHERE e.from_ref = ? AND e.kind = 'used_in'
            AND e.knowledge_state = 'confirmed'
        ORDER BY p.id
    """, (node_ref,))
    return sorted({row[0] for row in rows if json.loads(row[1])})


def lexical(record, query, terms, keys, family):
    reasons, score = [], 0.0
    if not matches_key(record, keys):
        return score, reasons
    identity = normalize(record["id"])
    normalized_query = normalize(query).strip()
    if normalized_query in {identity, family + ":" + identity}:
        score += 1000
        reasons.append("exact_id")
    record_key = normalize(record.get("jira_key"))
    variants = {variant for key in keys for variant in key_variants(key)}
    canonical_identity = any(identity in {key, "task:" + key, "jira:" + key} for key in variants)
    if keys and (record_key in variants or canonical_identity):
        score += 800
        reasons.append("exact_jira_key")
        if canonical_identity:
            score += 150
            reasons.append("canonical_issue_record")
    title = normalize(record["title"])
    summary = normalize(record["summary"])
    aliases = [phrase(value) for value in record.get("aliases", [])]
    tags = [normalize(value) for value in record.get("tags", [])]
    query_phrase = phrase(query)
    exact_aliases = [alias for alias in aliases if alias and alias == query_phrase]
    if exact_aliases:
        score += 200
        reasons.append("exact_alias")
    alias_phrases = [alias for alias in aliases if " " in alias and (" " + alias + " ") in (" " + query_phrase + " ")]
    if alias_phrases and not exact_aliases:
        score += 60
        reasons.append("alias_phrase")
    concept_aliases = concept_alias_matches(record, query)
    if concept_aliases:
        significant = " ".join(term for term in WORDS.findall(query_phrase) if term not in STOPWORDS)
        if significant in concept_aliases:
            if not exact_aliases:
                score += 200
            reasons.append("exact_concept_alias")
        elif not (exact_aliases or alias_phrases):
            score += 60
        reasons.extend("concept_alias:" + alias[:64] for alias in concept_aliases[:8])
    matched = []
    for term in terms:
        points = 0
        if matches_term(term, title):
            points += 12
        if any(matches_term(term, alias) for alias in aliases):
            points += 8
        if any(matches_term(term, tag) for tag in tags):
            points += 6
        if matches_term(term, summary):
            points += 2
        if matches_term(term, identity):
            points += 4
        if points:
            score += points
            matched.append(term)
    for key in keys:
        # Whole Jira keys may be mentioned in another record's evidence or summary.
        patterns = [r"(?<![\w])" + re.escape(variant) + r"(?![\w])" for variant in key_variants(key)]
        if any(re.search(pattern, text) for pattern in patterns for text in (title, summary, " ".join(aliases), identity)):
            score += 25
            matched.append(key)
    if matched:
        score += 30 * len(set(matched)) / max(1, len(terms) + len(keys))
        reasons.append("keywords:" + ",".join(word[:64] for word in matched[:8]))
    if score and record.get("kind") == "decision" and re.search(r"\b(?:почему|причин\w*|why|reason\w*)\b", normalized_query):
        score += 18
        reasons.append("decision_question")
    return score, reasons


def lexical_rejection(record, score, reasons, keys, subjects, symbols):
    if not score:
        return "jira_guard" if keys and not matches_key(record, keys) else "weak_lexical"
    if not keys and "exact_id" not in reasons:
        if not matches_subject(record, subjects):
            return "subject_guard"
        if not matches_short_subjects(record, symbols):
            return "short_subject_guard"
    return None


def candidates(conn, query, terms, keys, states, project, api, trace=None):
    states_sql = ",".join("?" for _ in states)
    candidates_by_ref, fts_ranks, discarded_weak = {}, {}, 0
    subjects = technical_subjects(query)
    symbols = short_subjects(query)
    has_fts = table_exists(conn, "entries_fts")
    fts_truncated = False
    if has_fts and (terms or keys):
        expression = " OR ".join('"' + word.replace('"', '""') + '"*' for word in terms)
        key_expressions = ['"' + variant.replace('"', '""') + '"' for key in keys for variant in key_variants(key)]
        expression = " OR ".join(key_expressions) if keys else expression
        rows = conn.execute("""
            SELECT e.id
            FROM entries e
                JOIN entries_fts ON entries_fts.rowid = e.rowid
            WHERE 1=1 AND entries_fts MATCH ? AND e.knowledge_state IN (""" + states_sql + """ )
            ORDER BY bm25(entries_fts, 8.0, 6.0, 1.0, 10.0, 3.0, 5.0), e.id
            LIMIT ?
        """, [expression] + states + [CANDIDATE_LIMIT + 1]).fetchall()
        fts_truncated = len(rows) > CANDIDATE_LIMIT
        fts_ranks = {row[0]: rank for rank, row in enumerate(rows[:CANDIDATE_LIMIT])}
        if trace:
            trace.counts.update(fts_rows_fetched=len(rows), fts_shortlisted=len(fts_ranks))

    # A light scan supplies exact aliases/IDs that tokenization can miss. When FTS
    # is absent the same scan also acts as the Unicode lexical fallback.
    rows = conn.execute("SELECT * FROM entries WHERE 1=1 AND knowledge_state IN (" + states_sql + ")", states)
    for row in rows:
        record = decode(row)
        node_ref = "entry:" + record["id"]
        if trace:
            trace.counts["entries_scanned"] += 1
        score, reasons = lexical(record, query, terms, keys, "entry")
        rank = fts_ranks.get(record["id"])
        # FTS rank and project proximity can rank evidence, but cannot turn an
        # absent substantive lexical anchor into evidence for the question.
        rejection = lexical_rejection(record, score, reasons, keys, subjects, symbols)
        if rejection:
            discarded_weak += int(rank is not None)
            if trace:
                trace.direct(node_ref, rejection)
            continue
        exact = any(reason in {"exact_id", "exact_jira_key", "exact_alias", "alias_phrase"} for reason in reasons)
        if has_fts and rank is None and not exact:
            if trace:
                # This proves absence from the executed shortlist, not whether
                # a hypothetical unbounded FTS search would have matched it.
                trace.direct(node_ref, "fts_not_shortlisted")
            continue
        if rank is not None:
            score += 15 / (1 + rank / 8)
            reasons.append("weighted_fts")
        record["project_ids"] = [r[0] for r in conn.execute("SELECT project_id FROM entry_projects WHERE 1=1 AND entry_id = ? ORDER BY project_id", (record["id"],))]
        if project and project in record["project_ids"]:
            score += 18
            reasons.append("project_match")
        candidates_by_ref[node_ref] = {"record": record, "ref": node_ref, "score": score, "reasons": reasons}
        if trace:
            trace.direct(node_ref, "candidate")

    if table_exists(conn, "entities"):
        for row in conn.execute("SELECT * FROM entities WHERE 1=1 AND knowledge_state IN (" + states_sql + ")", states):
            record = decode(row)
            node_ref = "entity:" + record["id"]
            if trace:
                trace.counts["entities_scanned"] += 1
            score, reasons = lexical(record, query, terms, keys, "entity")
            rejection = lexical_rejection(record, score, reasons, keys, subjects, symbols)
            if rejection:
                if trace:
                    trace.direct(node_ref, rejection)
                continue
            record.update(project_ids=[], work_state=None)
            if record["kind"] == "concept":
                record["project_ids"] = concept_projects(conn, node_ref)
                if project and project in record["project_ids"]:
                    score += 18
                    reasons.append("confirmed_concept_scope")
            elif project and table_exists(conn, "graph_edges"):
                # Only an explicitly recorded entity/project link grants this boost.
                linked = conn.execute("SELECT 1 FROM graph_edges WHERE 1=1 AND knowledge_state IN (" + states_sql + ") AND ((from_ref = ? AND to_ref = ?) OR (to_ref = ? AND from_ref = ?)) LIMIT 1",
                                      states + [node_ref, "project:" + project, node_ref, "project:" + project]).fetchone()
                if linked:
                    score += 18
                    reasons.append("explicit_project_link")
            reasons.append("entity_lexical")
            candidates_by_ref[node_ref] = {"record": record, "ref": node_ref, "score": score, "reasons": reasons,
                                           "concept_aliases": concept_alias_matches(record, query)}
            if trace:
                trace.direct(node_ref, "candidate")
    return candidates_by_ref, fts_truncated, "fts5" if has_fts else "scan", discarded_weak


def load_node(conn, node_ref, states, api):
    family, _, identity = node_ref.partition(":")
    if family == "entry":
        record = api.entry_record(conn, identity)
    elif family == "entity" and table_exists(conn, "entities"):
        row = conn.execute("SELECT * FROM entities WHERE 1=1 AND id = ?", (identity,)).fetchone()
        record = decode(row) if row else None
        if record:
            record.update(project_ids=[], work_state=None)
            if record["kind"] == "concept":
                record["project_ids"] = concept_projects(conn, node_ref)
    else:
        return None
    return record if record and record["knowledge_state"] in states else None


def graph_candidates(conn, direct, states, api, keys=(), trace=None):
    result, edges, truncated = {}, {}, False
    if not table_exists(conn, "graph_edges"):
        return result, edges, truncated
    states_sql = ",".join("?" for _ in states)
    for anchor in direct[:3]:
        if trace:
            trace.counts["graph_seeds"] += 1
        links = conn.execute("SELECT * FROM graph_edges WHERE 1=1 AND (from_ref = ? OR to_ref = ?) AND knowledge_state IN (" + states_sql + ") AND kind <> 'belongs_to_project' ORDER BY id LIMIT 101",
                             [anchor["ref"], anchor["ref"]] + states).fetchall()
        truncated = truncated or len(links) > 100
        if trace:
            trace.counts["graph_links_fetched"] += len(links)
        for row in links[:100]:
            neighbor = row["to_ref"] if row["from_ref"] == anchor["ref"] else row["from_ref"]
            if trace:
                trace.counts["graph_links_examined"] += 1
            if not json.loads(row["sources"]):
                if trace:
                    trace.graph(neighbor, "unbacked_edge")
                continue
            if neighbor == anchor["ref"]:
                if trace:
                    trace.graph(neighbor, "self_link")
                continue
            record = load_node(conn, neighbor, states, api)
            if record is None or not matches_key(record, keys):
                if trace:
                    trace.graph(neighbor, "node_unavailable" if record is None else "jira_guard")
                continue
            if trace:
                trace.graph(neighbor, "candidate")
            reason = "graph:" + row["kind"] + ":" + anchor["ref"]
            score = min(70, anchor["score"] * 0.45)
            candidate = result.get(neighbor)
            if candidate is None:
                result[neighbor] = {"record": record, "ref": neighbor, "score": score, "reasons": [reason]}
            else:
                candidate["score"] = max(score, candidate["score"])
                candidate["reasons"].append(reason)
            evidence = json.loads(row["sources"])
            edge_record = {key: row[key] for key in ("from_ref", "to_ref", "kind", "knowledge_state", "verified_at")}
            edge_record.update(id="edge:" + row["id"], origin="explicit", description=row["description"][:500],
                               description_is_excerpt=len(row["description"]) > 500,
                               sources=evidence[:2], source_count=len(evidence), sources_omitted=max(0, len(evidence) - 2))
            edges[edge_record["id"]] = edge_record
    return result, edges, truncated


def rank(items):
    return sorted(items, key=lambda item: (-item["score"], item["ref"]))


def concept_ambiguity(direct):
    """Describe competing recorded meanings before --limit or budget truncation."""
    by_alias = {}
    for item in direct:
        for alias in item.get("concept_aliases", []):
            by_alias.setdefault(alias, set()).add(item["ref"])
    shared = {alias: refs for alias, refs in by_alias.items() if len(refs) > 1}
    if not shared:
        return None
    refs = set().union(*shared.values())
    matches = [item for item in direct if item["ref"] in refs]
    candidates = []
    for item in matches[:16]:
        record = item["record"]
        projects = record["project_ids"]
        candidate = {"ref": item["ref"], "title": record["title"][:200],
                     "project_ids": projects[:8], "knowledge_state": record["knowledge_state"]}
        if len(record["title"]) > 200:
            candidate["title_truncated"] = True
        if len(projects) > 8:
            candidate["project_ids_omitted"] = len(projects) - 8
        candidates.append(candidate)
    aliases = sorted(shared)
    return {"detected": True, "matched_aliases": [alias[:200] for alias in aliases[:8]],
            "aliases_truncated": len(aliases) > 8 or any(len(alias) > 200 for alias in aliases[:8]),
            "concept_count": len(matches), "candidates": candidates,
            "omitted_candidates": max(0, len(matches) - len(candidates)),
            "note": "Several recorded concepts share the queried alias. Project context ranks scopes; it does not establish a single meaning."}


def recall(conn, args, api, *, bounded=True):
    query = api.string(args.query, "query").strip()
    if len(query) > 2000:
        raise api.MemoryError("Recall query must be at most 2000 characters")
    if not 1 <= args.limit <= 200:
        raise api.MemoryError("Recall limit must be between 1 and 200")
    if not 2000 <= args.max_chars <= 100000:
        raise api.MemoryError("max_chars must be between 2000 and 100000")
    project = getattr(args, "project", None)
    if project:
        api.require_project(conn, project)
    states = ["confirmed"]
    if getattr(args, "include_hypotheses", False):
        states.append("hypothesis")
    if getattr(args, "include_superseded", False):
        states.append("superseded")
    expected_ref = getattr(args, "expect", None)
    trace = RecallTrace(conn, expected_ref, states, api) if getattr(args, "explain", False) or expected_ref is not None else None
    terms, keys = terms_for(query)
    direct_by_ref, fts_truncated, backend, discarded_weak = candidates(conn, query, terms, keys, states, project, api, trace)
    direct = rank(direct_by_ref.values())
    graph_by_ref, edges_by_id, graph_truncated = graph_candidates(conn, direct, states, api, keys, trace)
    # Reserve at most two places for connected evidence; keep exact/lexical hits first.
    graph_only = rank(item for node_ref, item in graph_by_ref.items() if node_ref not in {x["ref"] for x in direct[:args.limit]})
    protected = sum(any(reason in {"exact_id", "exact_jira_key", "exact_alias", "exact_concept_alias"} for reason in item["reasons"]) for item in direct[:args.limit])
    additions = graph_only[:min(2, max(0, args.limit - max(1, protected)))]
    chosen = direct[:max(0, args.limit - len(additions))] + additions
    if trace:
        trace.selection(direct, graph_by_ref, chosen, fts_truncated, graph_truncated)
    selected = {item["ref"] for item in chosen}
    results = []
    asks_current = bool(CURRENT.search(query))
    for item in chosen:
        record = item["record"]
        output = compact.card(record, item["ref"], summary_chars=1100)
        reasons = list(item["reasons"])
        if item["ref"] in graph_by_ref:
            reasons.extend(reason for reason in graph_by_ref[item["ref"]]["reasons"] if reason not in reasons)
        rechecks = []
        if asks_current:
            rechecks.append("Question asks about current status; recorded evidence must be checked against its authoritative source.")
        if not record.get("verified_at"):
            rechecks.append("No verified_at is recorded.")
        output.update(match_reasons=reasons, retrieval_score=round(item["score"], 3),
                      needs_recheck=bool(rechecks), recheck_reasons=rechecks)
        results.append(output)
    response = {
        "query": query[:500], "query_is_excerpt": len(query) > 500, "project": project, "results": results,
        "edges": [edge for edge in edges_by_id.values() if edge["from_ref"] in selected and edge["to_ref"] in selected],
        "no_match": not bool(results), "candidate_count": len(set(direct_by_ref) | set(graph_by_ref)),
        "selection": {"method": "weighted_lexical_then_explicit_1hop",
                      "search_backend": backend, "terms": [term[:64] for term in terms[:16]], "jira_keys": keys[:20],
                      "knowledge_states": states, "project_mode": "boost", "graph_depth": 1,
                      "result_limit": args.limit},
    }
    ambiguity = concept_ambiguity(direct)
    if ambiguity:
        response["ambiguity"] = ambiguity
    if len(terms) > 16 or len(keys) > 20:
        response["selection"].update(terms_omitted=max(0, len(terms) - 16), jira_keys_omitted=max(0, len(keys) - 20))
    if fts_truncated or graph_truncated:
        response["selection"].update(fts_candidate_limit=CANDIDATE_LIMIT, fts_candidates_truncated=fts_truncated,
                                      graph_links_per_seed=100, graph_candidates_truncated=graph_truncated)
    if asks_current:
        response["selection"]["current_status_requested"] = True
    if discarded_weak:
        response["selection"]["discarded_weak_matches"] = discarded_weak
    if keys:
        response["selection"]["jira_mode"] = "required_whole_key"
    if not results:
        response["coverage_note"] = "No indexed record found; this does not prove that the work never happened."
        response["selection"]["abstention_reason"] = "no_matching_issue_key" if keys else "no_meaningful_lexical_anchor"
    if not bounded:
        # In-process composition applies its own single output budget. Selection,
        # guards and excerpts are exactly the same as for standalone recall.
        return response
    return fit_explained_response(response, args.max_chars, trace) if trace else compact.fit_response(response, args.max_chars)
