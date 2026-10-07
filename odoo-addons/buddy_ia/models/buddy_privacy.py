"""The few privacy rules and closed lists that the engine, the controller and the statistics models share.

No dependency on Odoo on purpose: the pure checks load this module on its own (see docs/VALIDATION.md).
"""
import re

# The administrator's switch «Guardar preguntas»: when on, the text of every question is kept (statistics and votes).
QUESTIONS_PARAM = "buddy_ia.insight_questions"

# What a person can say about an answer, and why (closed lists: nothing else is accepted or stored).
VOTES = [("up", "👍 Útil"), ("down", "👎 No útil")]
REASONS = [
    ("not_asked", "No era lo que pregunté"),
    ("wrong", "Dato incorrecto o desactualizado"),
    ("incomplete", "Incompleta"),
    ("other", "Otro"),
]
VOTE_CODES = tuple(code for code, label in VOTES)
REASON_CODES = tuple(code for code, label in REASONS)


def flag(value):
    """A checkbox stored as a system parameter ('True' when ticked, absent when not)."""
    return str(value or "").strip().lower() in ("true", "1")


_FIELD = re.compile(r'\s*"?(\w+)')


def _field_of(spec):
    """The field a domain leaf, group-by, aggregate or order term points at: «document_ids.name», «document_ids:count»,
    «document_ids desc», «"document_ids"» and «document_ids<tab>desc» are all «document_ids» (the ORM accepts any
    whitespace and optional quotes in an order, so this must too)."""
    match = _FIELD.match(spec) if isinstance(spec, str) else None
    return match.group(1) if match else ""


def _leaves(domain):
    """Every leaf of a (possibly nested) domain, tolerating whatever shape comes in (it is untrusted). A leaf is a list or
    tuple of three whose first two items are text: `["!", "!", leaf]` (a double negation, valid) is NOT one, so the real
    leaf inside it is still found."""
    for item in domain if isinstance(domain, (list, tuple)) else ():
        if isinstance(item, (list, tuple)):
            if len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str):
                yield item
            else:
                yield from _leaves(item)


def mentions_field(domain, names, specs=(), order=None):
    """Whether a search domain, the group-by / aggregate specs or the order text use one of `names` (see buddy_guard.py)."""
    names = set(names)
    if any(_field_of(leaf[0]) in names for leaf in _leaves(domain)):
        return True
    if any(_field_of(spec) in names for spec in specs or ()):
        return True
    return any(_field_of(part) in names for part in str(order or "").split(","))
