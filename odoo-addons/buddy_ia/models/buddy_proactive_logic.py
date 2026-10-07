"""Pure rules of the gentle proactivity (E4b): the switches, the suggestion chips and the small texts around them.

No dependency on Odoo on purpose: the pure checks load this module on its own (see docs/VALIDATION.md). The models
(`buddy_proactive.py`, `buddy_digest.py`), the settings and the controller call these functions; nothing here touches a
database. Everything the widget receives from here is plain text that has already been cleaned and bounded.
"""
import re
from contextlib import contextmanager

from .buddy_journey_logic import clean_text
from .buddy_text import fold

# The switches of the settings screen. All of them are ON when the parameter is missing (nothing here sends anything to
# the AI provider, and the weekly summary stays inside Odoo): they are written as explicit «1» / «0».
CHIPS_PARAM = "buddy_ia.proactive_chips"
NUDGE_PARAM = "buddy_ia.proactive_nudge"
DIGEST_PARAM = "buddy_ia.digest_enabled"
# Who is told (an Odoo activity, never an e-mail) that a new weekly summary is ready.
DIGEST_NOTIFY_PARAM = "buddy_ia.digest_notify"
DIGEST_NOTIFY = [
    ("responsible", "Quien atiende los tickets (si no hay, los administradores)"),
    ("admins", "Los administradores"),
    ("none", "Nadie: solo guardar el resumen"),
]
DIGEST_NOTIFY_CODES = tuple(code for code, label in DIGEST_NOTIFY)
DEFAULT_DIGEST_NOTIFY = "responsible"

# The topics most asked about («temas más consultados»): documents cited by at least MIN_TOPIC_QUERIES answered queries on at least
# MIN_TOPIC_DAYS different CLOSED days of the last TOPIC_DAYS days (today is never counted). The statistics say nothing about WHO asked,
# so this is not «5 people»: it only stops one person's afternoon of questions about one document from becoming a «topic of the
# company» for everybody who reads it (and stops the day's count from showing when somebody consulted a document just now).
# docs/SECURITY.md says so.
MIN_TOPIC_QUERIES = 10
MIN_TOPIC_DAYS = 3
TOPIC_DAYS = 30
# The folder of a topic must be readable by at least this many people, whatever the minimum chosen for the votes.
MIN_TOPIC_AUDIENCE = 5
TOPIC_CACHE_SECONDS = 1800
TOPIC_ERROR_SECONDS = 60  # when computing them fails, «none» is remembered this long (no request retries a slow query)
TOPIC_STATEMENT_MS = 5000  # the query of the topics never runs longer than this: the person is waiting for the chat to open
TOPIC_CANDIDATES = 12  # what the cached query keeps (each person sees only the ones they may read)
MAX_TOPIC_CHIPS = 4
# What a person may be offered at most (the widget shows 3 or 4 of them, rotating).
POOL_LIMIT = 8
CHIP_TEXT_CHARS = 200  # what is sent as the question (a step's suggested question is up to 200 characters)
CHIP_LABEL_CHARS = 80  # what the button shows (the same limit as the chips the administrator writes)
CHIP_KINDS = ("step", "topic", "next")

_EXTENSION = re.compile(r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|csv|html?|rtf|odt|ods|odp|gdoc|gsheet|gslides)$", re.IGNORECASE)
# What may survive of a document title when it becomes a question: letters, digits, spaces and a few plain signs (no full stops, colons,
# quotation marks or guillemets: nothing that could close the sentence of the template and start another one). Same idea as the name of
# a journey in the stage line of the prompt.
_INERT = re.compile(r"[^\w ()/+\-]")


@contextmanager
def statement_timeout(cr, milliseconds):
    """No query inside the block runs longer than `milliseconds` (the setting is local to the transaction and is put back afterwards).
    Open the savepoint INSIDE this block: when a query fails the savepoint is undone first, and only then the setting is restored (in an
    aborted transaction nothing could be executed)."""
    cr.execute("SHOW statement_timeout")
    previous = cr.fetchone()[0]
    cr.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(milliseconds)),))
    try:
        yield
    finally:
        cr.execute("SELECT set_config('statement_timeout', %s, true)", (previous,))


def switch_on(raw):
    """A switch that is ON unless the parameter explicitly says «0», «false», «no» or «off». A parameter that does not exist
    (Odoo's `get_param` answers `False`, `None` or an empty text) leaves it on."""
    if raw is None or raw is False or raw == "":
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def notify_mode(raw):
    """Who is told about a new weekly summary: one of the closed list, anything else is the default."""
    value = str(raw or "").strip()
    return value if value in DIGEST_NOTIFY_CODES else DEFAULT_DIGEST_NOTIFY


def strip_extension(name):
    """A file name without its extension (the title people know the document by)."""
    return _EXTENSION.sub("", name or "")


def fit_title(template, title, limit=CHIP_LABEL_CHARS):
    """`template` (it holds `%(title)s`) with the title cut so the whole text stays within `limit`; the cut ends in «…».

    The title is a file name written by somebody else: it is made one plain line first (`clean_text`) and never trusted for
    its length, and reduced to plain words (`_INERT`): a file name can say anything, and this one ends up as a question the person "asks".
    An empty title gives an empty text (there is nothing to ask about).
    """
    title = " ".join(_INERT.sub("", strip_extension(clean_text(title, 200))).split())
    if not title or "%(title)s" not in template:
        return ""
    room = limit - len(template.replace("%(title)s", ""))
    if room < 4:
        return ""
    if len(title) > room:
        title = title[: room - 1].rstrip() + "…"
    return template % {"title": title}


def label_for(text):
    """The text of a button: the question itself when it is short enough, else its beginning ending in «…»."""
    text = clean_text(text, CHIP_TEXT_CHARS)
    return text if len(text) <= CHIP_LABEL_CHARS else text[: CHIP_LABEL_CHARS - 1].rstrip() + "…"


def make_chip(kind, text, ask_id=None):
    """One suggestion as the widget receives it, or None when the text is empty or the kind is unknown.

    `ask_id` is the id of the person's progress row of a step of the kind «preguntarle al buddy»: asking that question IS the step,
    so the widget ticks it once the answer arrived (as it does with the button of the step). Anything but a positive integer is none.
    """
    text = clean_text(text, CHIP_TEXT_CHARS)
    if kind not in CHIP_KINDS or not text:
        return None
    chip = {"kind": kind, "text": text, "label": label_for(text)}
    if type(ask_id) is int and 0 < ask_id < 2 ** 31:
        chip["ask_id"] = ask_id
    return chip


def build_pool(due, topics, upcoming, limit=POOL_LIMIT):
    """The suggestions of one person, most urgent first: the questions of the steps due today or late, then the topics the
    company asks about most, then the questions of the steps that come next. Each item is a text or a `(text, ask_id)` pair.
    A question already in the list (the same words, accents and capitals aside) is not repeated."""
    pool, seen = [], set()
    for kind, items in (("step", due), ("topic", topics), ("next", upcoming)):
        for item in items:
            text, ask_id = item if isinstance(item, tuple) and len(item) == 2 else (item, None)
            chip = make_chip(kind, text, ask_id)
            key = " ".join(fold(chip["text"]).split()) if chip else ""
            if chip and key not in seen:
                seen.add(key)
                pool.append(chip)
    return pool[:limit]
