"""Recorded decision context for a task. Every supported store has a metadata table."""
KEY_PREFIX = "decision-card:v1:"
MAX_CHARS = 6000


def search_fields(card):
    yield card["question"]
    yield card.get("reconsider_when") or ""
    for statement in (*card["observations"], card.get("recommendation"), card["decision"]):
        if statement:
            yield statement.get("text", "")
            yield statement.get("by", "")
    yield card["decision"]["status"]


def _statement(value, name, api, *, attributed=False):
    if not isinstance(value, dict):
        raise api.MemoryError(name + " must be an object")
    keys = ("text", "reference", "by") if attributed else ("text", "reference")
    api.fields(value, keys)
    return {key: api.string(value.get(key), name + "." + key) for key in keys}


def validate(value, api):
    """Require a recorded outcome and its authority together. Pending needs neither."""
    if not isinstance(value, dict):
        raise api.MemoryError("decision_card must be an object")
    api.fields(value, {"question", "observations", "recommendation", "decision", "reconsider_when"})
    question = api.string(value.get("question"), "decision_card.question")
    observations = value.get("observations", [])
    if not isinstance(observations, list) or len(observations) > 8:
        raise api.MemoryError("decision_card.observations must be an array of at most 8 statements")
    recommendation = value.get("recommendation")
    if recommendation is not None:
        recommendation = _statement(recommendation, "decision_card.recommendation", api, attributed=True)
    decision = value.get("decision")
    if not isinstance(decision, dict):
        raise api.MemoryError("decision_card.decision must contain a status")
    status = api.enum(decision.get("status"), "decision_card.decision.status", {"pending", "accepted", "rejected"})
    outcome = {"status": status}
    if status == "pending":
        api.fields(decision, {"status"})
    else:
        statement = {key: detail for key, detail in decision.items() if key != "status"}
        outcome.update(_statement(statement, "decision_card.decision", api, attributed=True))
    result = {"question": question, "observations": [_statement(item, "decision_card.observations", api) for item in observations],
              "recommendation": recommendation, "decision": outcome,
              "reconsider_when": api.string(value.get("reconsider_when"), "decision_card.reconsider_when", nullable=True)}
    if len(api.dumps(result)) > MAX_CHARS:
        raise api.MemoryError("decision_card exceeds {} characters; link to detailed evidence".format(MAX_CHARS))
    return result
