"""Pure rules of the weekly summary «Qué falta documentar» (E4b): grouping of the unanswered questions, masking of personal
data in the examples, the windows of time and the limits of every section.

No dependency on Odoo on purpose: the pure checks load this module on its own (see docs/VALIDATION.md). The model
`buddy.digest` runs the queries and calls these functions; nothing here touches a database. The summary is made WITHOUT AI:
it is counting and a small grouping heuristic (words in common), which costs nothing and sends nothing anywhere.
"""
import json
import re
from collections import Counter
from datetime import date, timedelta

from .buddy_journey_logic import clean_text
from .buddy_text import FILLER, _stem as stem, fold

DATA_VERSION = 1
WINDOW_DAYS = 7  # the week the summary talks about (it ends the day it is made, which is not counted: that day is not over)
# Unanswered questions: at most this many are read and grouped per summary (the rest are only counted).
QUESTIONS_READ = 1000
MAX_GROUPS = 6
MIN_GROUP_SIZE = 2
EXAMPLES_PER_GROUP = 2
EXAMPLE_CHARS = 140
TERMS_PER_GROUP = 3
# A word that is in most of the questions says nothing about any of them («odoo», «sistema»): it never starts a group (it can
# still help to name one). Only applied when there are enough questions to tell.
GENERIC_RATIO = 0.6
GENERIC_MIN_QUESTIONS = 8
# Documents: how many each list names.
TOP_DOCUMENTS = 5
UNUSED_LISTED = 15
UNUSED_CANDIDATES = 400  # documents the query looks at; the rest are only counted
# «Sin uso»: a document is a candidate when nothing cited it in the last UNUSED_DAYS days (or in all the history there is, when
# the statistics are younger or are kept for less: but never judged on less than MIN_UNUSED_DAYS).
UNUSED_DAYS = 90
MIN_UNUSED_DAYS = 30
# Onboarding: figures are only given for a journey that this many people follow at the same time, and a step is only named when
# at least STEP_MIN_LATE of them are late with it: with fewer, a number would describe one person.
MIN_PEOPLE = 5
STEP_MIN_LATE = 2
MAX_JOURNEYS = 10
TOP_STEPS = 3
# Folders with problems.
MAX_FOLDERS = 15
# The whole run (every section, one after the other) has this long, in seconds: what does not fit is marked as partial.
BUDGET_SECONDS = 60
# One query never runs longer than this (milliseconds): a slow one skips its section instead of holding the cron.
STATEMENT_TIMEOUT_MS = 20000
MAX_STORED_BYTES = 200_000

SECTIONS = ("unanswered", "downvotes", "doubtful", "unused", "folders", "onboarding")
# The order they are shown in: what asks for work first, what only needs a look last (the list of unused documents is the longest).
RENDER_ORDER = ("unanswered", "downvotes", "doubtful", "folders", "onboarding", "unused")
# Sections only administrators read (the status of a folder and the progress of onboarding are theirs, not the managers').
ADMIN_SECTIONS = ("folders", "onboarding")

# Spanish words that say nothing about the topic of a question (the questions are in Spanish). Folded: no accents.
STOPWORDS = frozenset("""
el la los las lo un una unos unas al del de en a por para con sin sobre entre hasta desde hacia segun ante bajo contra durante tras
y e o u ni que pero sino aunque como si porque pues cuando donde mientras cual cuales quien quienes cuanto cuantos cuanta cuantas
yo tu vos usted ustedes ella ellos ellas nosotros me te se nos le les mi mis tus su sus nuestro nuestra nuestros nuestras
este esta esto estos estas ese esa eso esos esas aquel aquella aquellos aquellas
es son ser soy eres era eran fue fueron sea hay haber ha han he hemos tengo tiene tienen tener tenemos hace hacen hacer hago hacemos
puedo puede pueden poder podemos debo debe deben deber quiero quiere quieren voy va van ir esta estan estoy estar estamos estaba
no mas menos muy tambien ya aun todavia siempre nunca aqui ahi alli asi bien mal solo solamente tan tanto tanta
cosa cosas algo alguien algun alguna algunos algunas todo toda todos todas cada otro otra otros otras mismo misma
tema temas favor hola buenas buenos gracias dia dias vez veces
""".split()) | FILLER

_WORD = re.compile(r"[^\W_]+")
_EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d .\-/]{3,}\d")


def week_window(today):
    """`(first day, day after the last)` of the week that ends the day before `today`."""
    return today - timedelta(days=WINDOW_DAYS), today


def manual_window(today):
    """`(first day, day after the last)` of the summary an administrator asks for NOW: the last seven days including today."""
    return today - timedelta(days=WINDOW_DAYS - 1), today + timedelta(days=1)


def unused_window(history_days, retention_days):
    """How many days back «this document was never cited» can honestly be said: the shortest of the 90 days of the rule, the
    days the statistics are kept and the days they have existed, or 0 when that is less than the minimum (too young to judge)."""
    window = min(UNUSED_DAYS, int(retention_days or 0), int(history_days or 0))
    return window if window >= MIN_UNUSED_DAYS else 0


def question_keys(text, limit=14):
    """The words that name the topic of a question: `[(key, shown word)]`, no duplicates, in order. The key is the folded and
    stemmed word (so «backups», «backup» and «Backup» are one); the shown word keeps the accents of what was written."""
    found, seen = [], set()
    # An address or a link in a question is not its topic (and its pieces would end up naming a group): they are dropped first.
    text = _URL.sub(" ", _EMAIL.sub(" ", str(text or "")))
    for raw in _WORD.findall(text.lower()):
        word = fold(raw)
        if len(word) < 3 or word.isdigit() or word in STOPWORDS:
            continue
        key = stem(word)
        if key not in seen:
            seen.add(key)
            found.append((key, raw))
        if len(found) >= limit:
            break
    return found


def mask_personal(text, limit=EXAMPLE_CHARS):
    """A question as an example in the summary: one plain line, with e-mail addresses, links and long numbers (a national ID, a
    phone, an account) hidden and cut at `limit`. Questions are free text and can name people: nothing here should."""
    text = clean_text(text, 1000)
    text = _EMAIL.sub("[correo]", text)
    text = _URL.sub("[enlace]", text)
    text = _NUMBER.sub("[número]", text)
    return clean_text(text, limit)


def cluster_questions(questions, max_groups=MAX_GROUPS, min_size=MIN_GROUP_SIZE):
    """Group questions by the words they share. `questions` is a list of texts (some may be empty). Returns
    `(groups, ungrouped)`: each group is `{"terms": [...], "count": n, "members": [indexes]}`, biggest first; `ungrouped` is the
    number of non-empty questions that share no topic word with any other.

    The heuristic is greedy and small: take the word in the most questions that are not in a group yet (a word that is in most
    of ALL questions never starts one), the group is those questions, its name is that word plus the words most of the group
    also uses, and go on. Deterministic: ties are broken alphabetically.
    """
    keys = [question_keys(text) for text in questions]
    shown = [dict(pairs) for pairs in keys]
    sets = [set(item) for item in shown]
    remaining = {index for index, item in enumerate(sets) if item}
    total = len(remaining)
    overall = Counter(key for index in remaining for key in sets[index])
    too_generic = {key for key, count in overall.items() if total >= GENERIC_MIN_QUESTIONS and count / total > GENERIC_RATIO}
    groups = []
    while remaining and len(groups) < max_groups:
        counts = Counter(key for index in remaining for key in sets[index] if key not in too_generic)
        if not counts:
            break
        # The most common word; between words in as many questions, the longest one (more specific: «vacaciones» before «año»).
        key, size = min(counts.items(), key=lambda pair: (-pair[1], -len(pair[0]), pair[0]))
        if size < min_size:
            break
        members = sorted(index for index in remaining if key in sets[index])
        inside = Counter(other for index in members for other in sets[index] if other != key)
        # The name: the seed word plus the words that at least half of the group (and two questions) also use.
        extra = [other for other, count in sorted(inside.items(), key=lambda pair: (-pair[1], -len(pair[0]), pair[0]))
                 if count >= max(2, (len(members) + 1) // 2)][: TERMS_PER_GROUP - 1]
        names = {}
        for index in members:
            for name, word in shown[index].items():
                names.setdefault(name, Counter())[word] += 1
        terms = [names[name].most_common(1)[0][0] for name in [key] + extra]
        groups.append({"terms": terms, "count": len(members), "members": members})
        remaining -= set(members)
    groups.sort(key=lambda group: (-group["count"], group["terms"]))
    return groups, len(remaining)


def example_questions(questions, members, limit=EXAMPLES_PER_GROUP):
    """Up to `limit` different, masked examples of the questions of a group (the shortest ones: the clearest)."""
    seen, found = set(), []
    for index in sorted(members, key=lambda item: (len(questions[item] or ""), item)):
        text = mask_personal(questions[index])
        key = fold(text)
        if text and key not in seen:
            seen.add(key)
            found.append(text)
        if len(found) >= limit:
            break
    return found


def load_data(raw):
    """The stored data of a summary as a dict, tolerating whatever is there: anything that is not the expected shape is `{}`
    (a row the code does not understand must show an empty summary, never an error)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw and len(raw) <= MAX_STORED_BYTES else {}
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == DATA_VERSION else {}


def dump_data(data):
    """The text stored for a summary (compact, bounded). Raises ValueError when it would be too big: the sections are all
    capped, so that would be a bug and the run must say so instead of storing a truncated JSON."""
    text = json.dumps(dict(data, version=DATA_VERSION), ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_STORED_BYTES:
        raise ValueError("digest too big")
    return text


def int_list(value, limit):
    """A list of positive integers (document or folder ids) from untrusted data: at most `limit`, no booleans."""
    if not isinstance(value, list):
        return []
    return [item for item in value[:limit] if type(item) is int and 0 < item < 2 ** 31]


def count(value, maximum=10_000_000):
    """A non-negative integer from untrusted data (0 when it is anything else)."""
    return value if type(value) is int and 0 <= value <= maximum else 0


def window_dates(data):
    """`(first day, day after the last)` stored in the data, or `(None, None)`."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    try:
        return date.fromisoformat(window.get("from")), date.fromisoformat(window.get("to"))
    except (TypeError, ValueError):
        return None, None
