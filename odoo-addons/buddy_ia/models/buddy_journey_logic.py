"""Pure rules of the onboarding journeys (E4): safe links, due dates, stage of a person and a small rate window.

No dependency on Odoo on purpose: the pure checks load this module on its own (see docs/VALIDATION.md). The models in
`buddy_journey.py` and the engine call these functions; nothing here touches a database.
"""
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from datetime import date, timedelta
from urllib.parse import urlsplit

# The two switches of the settings screen (a missing parameter means «reminders on» and «stage context off»).
REMINDERS_PARAM = "buddy_ia.journey_reminders"
STAGE_PARAM = "buddy_ia.journey_stage"

# What a step asks the person to do (closed list: the browser filters it again).
STEP_KINDS = [("task", "Tarea"), ("read", "Leer un documento"), ("ask", "Preguntarle al buddy")]
STEP_KIND_CODES = tuple(code for code, label in STEP_KINDS)

# The states of an assignment.
ASSIGNMENT_STATES = [("active", "En curso"), ("done", "Completado"), ("cancelled", "Cancelado")]

# Limits shared by the models, the engine and the controller.
MAX_DAY_OFFSET = 365
LATE_ASSIGNMENT_DAYS = 7  # a joining date older than this makes the first steps show up as late
MAX_STEPS = 100  # per journey (the widget lists at most MAX_TASKS_SHOWN; a longer journey is a mistake, not a plan)
TITLE_CHARS = 120
DETAIL_CHARS = 600
QUESTION_CHARS = 200
URL_CHARS = 500
# A person sees at most this many steps in the widget (the whole journey is rarely longer than 30).
MAX_TASKS_SHOWN = 60
UPCOMING_DAYS = 14
UPCOMING_SHOWN = 5

# Whitespace, control characters and backslash never belong to a link: browsers read a backslash as a slash, which is how
# https://good.example\@evil.example/ ends up on another host.
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
# The last label of the host is a real top-level domain: letters (or «xn--» punycode), never digits. That leaves out IP addresses
# in any notation (192.168.1.1, 0x7f.0.0.1, 2130706433) and internal names without a domain.
_TLD = re.compile(r"[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59}")


def safe_https_url(url):
    """The address when it is a plain `https` link with a real host and no credentials, else an empty text.

    Same spirit as the Drive links of the answers (`safe_drive_link`) but for any host the administrator trusts: only
    `https`, no `user:password@`, no spaces, controls or backslashes, a dotted host name (no bare names, no `localhost`
    shortcuts typed by mistake) and at most URL_CHARS characters. A port is allowed only as plain digits.
    """
    if not isinstance(url, str) or not url or len(url) > URL_CHARS or _BAD_URL_CHARS.search(url):
        return ""
    if not url.startswith("https://"):
        return ""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port  # `port` raises ValueError when it is not a number
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ""
    if not host or not _HOST.fullmatch(host) or host.endswith(".") or not _TLD.fullmatch(host.rsplit(".", 1)[1]):
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return url


def _invisible(ch):
    """Control characters (C0 and C1) and the invisible «format» ones (zero-width spaces, the marks that change the direction of the
    text: U+202A to U+202E and U+2066 to U+2069, the byte order mark): text that is not what it looks like."""
    return ch < " " or "\x7f" <= ch <= "\x9f" or unicodedata.category(ch) == "Cf"


def clean_text(value, limit):
    """One short line of text (whitespace collapsed, control and invisible format characters dropped), cut at `limit`."""
    if not isinstance(value, str):
        return ""
    value = "".join(" " if ch in "\r\n\t" else ch for ch in value if ch in "\r\n\t" or not _invisible(ch))
    return " ".join(value.split())[:limit]


def due_date(start, day_offset):
    """The day a step is due: the joining date plus `day_offset` days (0 = the first day)."""
    return start + timedelta(days=int(day_offset or 0))


def due_state(due, today, done):
    """«done» / «overdue» / «today» / «upcoming» for one step."""
    if done:
        return "done"
    if due < today:
        return "overdue"
    return "today" if due == today else "upcoming"


def percent(done, total):
    """Whole-number percentage (0 when there is nothing to do)."""
    return int(round(100.0 * done / total)) if total else 0


def stage(start, today):
    """Where a person is in the journey, as `(kind, number)`: the first day, the first week, week N (to week 4), month N.

    `None` when the journey has not started yet. The days are counted from the joining date (day 0 = the first day).
    """
    days = (today - start).days
    if days < 0:
        return None
    if days == 0:
        return ("day", 1)
    if days < 7:
        return ("week", 1)
    if days < 28:
        return ("week", days // 7 + 1)
    return ("month", days // 30 + 1)


def stage_note(journey_name, start, today):
    """The line for the prompt (Spanish: it is addressed to the model), without naming or identifying the person.

    «La persona está en su semana 1 del recorrido "X"». The journey name is data written by an administrator: it is made
    inert (no markup, no quotes, one short line) before it is part of a prompt.
    """
    where = stage(start, today)
    if not where:
        return ""
    kind, number = where
    when = {"day": "su primer día", "week": "su semana %d" % number, "month": "su mes %d" % number}[kind]
    if kind == "week" and number == 1:
        when = "su primera semana"
    # The name is text an administrator or a journey manager wrote and it goes into a prompt: only letters, digits, spaces and a few plain signs
    # survive (no markup, no quotes or guillemets, no full stops or colons: nothing that could close the sentence and start
    # another one).
    name = " ".join(re.sub(r"[^\w ()/+\-]", "", clean_text(journey_name, 60)).split())
    if not name:
        return ""
    return "La persona está en %s del recorrido de onboarding «%s»." % (when, name)


class RateWindow:
    """A sliding window per key kept in this process: at most `limit` hits in `seconds`. Small and bounded (a defense in
    depth for the cheap read route; the vote-like writes are limited with the database by the engine)."""

    MAX_KEYS = 2048

    def __init__(self, limit, seconds, clock=time.monotonic):
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self._hits = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            else:
                self._hits.move_to_end(key)
            while hits and now - hits[0] >= self.seconds:
                hits.popleft()
            allowed = len(hits) < self.limit
            if allowed:
                hits.append(now)
            while len(self._hits) > self.MAX_KEYS:
                self._hits.popitem(last=False)
            return allowed


def as_date(value):
    """A `date` from a date or an ISO text, or None (nothing else is accepted)."""
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None
