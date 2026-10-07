"""Pure text helpers for retrieval: query terms, synonym groups and the search context of a chunk.

No Odoo imports, so it can be tested without a server. Everything that ends up inside a tsquery is made only of
letters and digits (accents folded): user text and administrator-written synonyms can never inject tsquery syntax.
"""
import re

_FOLD = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
MIN_WORD = 3

# --- synonyms ----------------------------------------------------------------------------------------------------
# Words people use for the same thing when they ask for help or make a request. A group counts as ONE query term:
# a chunk matches it when it contains any member. Multi-word members ("mesa de ayuda") match all their words (AND).
DEFAULT_SYNONYMS = (
    ("ticket", "solicitud", "pedido", "incidente", "caso", "requerimiento", "helpdesk", "mesa de ayuda"),
    # People say "ERP" for the system the documents call Odoo; "backup" is the usual anglicism for "respaldo".
    ("odoo", "erp"),
    ("backup", "respaldo", "resguardo", "copia de seguridad"),
)
MAX_SYNONYM_CHARS = 2000
MAX_SYNONYM_GROUPS = 40
MAX_GROUP_MEMBERS = 12
MAX_MEMBER_CHARS = 40
MAX_MEMBER_WORDS = 4
MAX_GROUP_ALTERNATIVES = 24
_MEMBER_CHARS = re.compile(r"[^\W_]+(?:[ \-][^\W_]+)*")  # letters/digits separated by single spaces or hyphens

# Conversational filler that says nothing about the topic and would only match unrelated text.
FILLER = frozenset("""hola buenas buenos gracias favor podes puedes podrias podria podrian puedo quiero
quisiera necesito decis decime decirme dime indicame indicarme explicame explicarme ayudame saber sabes
guiar guiarme guiame ayudar ayudarme ayudas""".split())


def fold(text):
    """Lowercase with accents folded: what both the index and the question go through."""
    return str(text).translate(_FOLD).lower()


def _word(raw):
    return "".join(ch for ch in raw if ch.isalnum())


def text_terms(message, limit=12):
    """Distinct alphanumeric words with accents folded, for an OR full-text query.

    Only letters and digits survive, so user text can never inject tsquery operators.
    """
    seen, terms = set(), []
    for raw in fold(message).split():
        word = _word(raw)
        if len(word) >= MIN_WORD and word not in seen:
            seen.add(word)
            terms.append(word)
    return terms[:limit]


def _words(phrase):
    return [word for word in (_word(raw) for raw in fold(phrase).split()) if len(word) >= MIN_WORD]


def _keys(word):
    """A word and its plural-less forms ('tickets' -> ticket, 'incidentes' -> incidente, incident)."""
    keys = {word}
    if word.endswith("s") and len(word) > MIN_WORD:
        keys.add(word[:-1])
    if word.endswith("es") and len(word) > MIN_WORD + 1:
        keys.add(word[:-2])
    return keys


# Singular/plural pairs of the suffixes that the Spanish stemmer of PostgreSQL does not always bring together.
_SUFFIX_PAIRS = (("ciones", "cion"), ("siones", "sion"))


def word_variants(word):
    """The word followed by its -cion/-ciones (or -sion/-siones) twin: 'cotizacion' -> cotizacion, cotizaciones.

    Both go into the query as alternatives of one term, so a question in the singular finds a document in the plural
    and the other way round. A word without that ending (or too short to have a stem) is returned alone.
    """
    variants = [word]
    for plural, singular in _SUFFIX_PAIRS:
        if word.endswith(plural) and len(word) >= len(plural) + 2:
            variants.append(word[:-len(plural)] + singular)
        elif word.endswith(singular) and len(word) >= len(singular) + 2:
            variants.append(word[:-len(singular)] + plural)
    return variants


# --- typo tolerance (second try, only when the first search found little) ----------------------------------------
MIN_FUZZY_WORD = 5  # shorter words have too many neighbours at distance 1 ("mesa"/"misa")
MAX_VOCABULARY = 5000
_WORDS = re.compile(r"[^\W_]+")  # runs of letters and digits: "v2.docx" is "v2" and "docx"


def _stem(word):
    """A crude singular: no plural -s and no final -e, so 'tickets'/'ticket', 'clientes'/'cliente', 'backups'/'backup'."""
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def _distance(a, b, limit):
    """Edit distance with adjacent swaps (insert, delete, replace, swap), or limit + 1 as soon as it is certainly higher."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous, before = list(range(len(b) + 1)), None
    for i, char in enumerate(a, 1):
        row = [i] + [0] * len(b)
        for j, other in enumerate(b, 1):
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (char != other))
            if before is not None and i > 1 and j > 1 and char == b[j - 2] and a[i - 2] == other:
                row[j] = min(row[j], before[j - 2] + 1)
        if min(row) > limit:
            return limit + 1
        previous, before = row, previous
    return previous[-1]


def build_vocabulary(texts, synonyms=None, limit=MAX_VOCABULARY):
    """The words (folded, 3 letters or more) of document titles, folders and synonym groups: what a typo can be fixed to."""
    words = set()
    for text in texts:
        words.update(word for word in _WORDS.findall(fold(text)) if len(word) >= MIN_WORD)
        if len(words) >= limit:
            break
    for group in synonyms or ():
        for member in group:
            words.update(_words(member))
    return frozenset(words)


def content_words(words, limit=3000):
    """Words of the documents' own text (as the database split them) ready to join a vocabulary: folded, letters only,
    four letters or more, no duplicates, at most `limit`. Typos are fixed against these too."""
    seen = set()
    for raw in words:
        word = _word(fold(raw))
        if len(word) >= 4 and word.isalpha():
            seen.add(word)
            if len(seen) >= limit:
                break
    return frozenset(seen)


def unknown_words(message, vocabulary):
    """The words of the question that look like typos: long enough to be checked, not filler, and with no word of the
    vocabulary (plural, final -e and -cion twins included) they could be. Bounded like the corrector: only the first
    MAX_QUERY_TOKENS different words are looked at."""
    stems = {_stem(word) for word in vocabulary}
    found = []
    for raw in list(dict.fromkeys(fold(message).split()))[:MAX_QUERY_TOKENS * 3]:
        word = _word(raw)
        if len(word) >= MIN_FUZZY_WORD and word not in FILLER and not word.isdigit() and _stem(word) not in stems:
            found.append(word)
    return found[:MAX_CORRECTED_WORDS]


# A question is at most 2000 characters, but a hostile one of 250 different words used to cost a distance computation
# against the whole vocabulary for each of them. Only this many different words are ever looked at.
MAX_QUERY_TOKENS = 40
MAX_CORRECTED_WORDS = 16


def correct_message(message, vocabulary):
    """The question with every word that looks like a typo of a known word replaced by that word.

    A word is left alone when its singular is in the vocabulary, when it is short, or when it is conversational filler.
    One typo is tolerated in words of up to 7 letters and two in longer ones ('bakups' -> backups, 'tiket' -> ticket).
    Returns (text, changed). Words only ever come from the vocabulary, so the result has only letters and digits.
    """
    by_stem = {}
    for word in sorted(vocabulary, key=lambda w: (len(w), w)):
        by_stem.setdefault(_stem(word), word)
    out, changed = [], False
    answers, searched = {}, 0  # answers: a word already looked up (the same word is never searched twice)
    for raw in fold(message).split()[:MAX_QUERY_TOKENS * 3]:
        word = _word(raw)
        if not word:
            continue
        if word in answers:
            word = answers[word]
            out.append(word)
            continue
        original, stem = word, _stem(word)
        if len(word) >= MIN_FUZZY_WORD and word not in FILLER and stem not in by_stem and searched < MAX_CORRECTED_WORDS:
            searched += 1
            limit = 1 if len(stem) <= 7 else 2
            best = None
            for known_stem, known in by_stem.items():
                gap = _distance(stem, known_stem, limit)
                if gap <= limit and (best is None or (gap, len(known), known) < best[0]):
                    best = ((gap, len(known), known), known)
            if best:
                word, changed = best[1], True
        answers[original] = word
        out.append(word)
    return " ".join(out), changed


# --- parsing the administrator's setting -------------------------------------------------------------------------

def validate_synonyms(raw):
    """Raise ValueError (message in Spanish, for the administrator) when the text is not a valid synonym list."""
    raw = raw or ""
    if len(raw) > MAX_SYNONYM_CHARS:
        raise ValueError("Los sinónimos admiten hasta %d caracteres." % MAX_SYNONYM_CHARS)
    lines = [line.strip() for line in raw.replace("\r", "").split("\n") if line.strip()]
    if len(lines) > MAX_SYNONYM_GROUPS:
        raise ValueError("Podés cargar hasta %d grupos de sinónimos (uno por línea)." % MAX_SYNONYM_GROUPS)
    for number, line in enumerate(lines, 1):
        members = [member.strip() for member in line.split(",")]
        if not 2 <= len(members) <= MAX_GROUP_MEMBERS or not all(members):
            raise ValueError("Línea %d: escribí entre 2 y %d palabras separadas por comas, sin dejar ninguna vacía. "
                             "Ejemplo: vacaciones, licencia, días libres." % (number, MAX_GROUP_MEMBERS))
        for member in members:
            if len(member) > MAX_MEMBER_CHARS or len(member.split()) > MAX_MEMBER_WORDS:
                raise ValueError("Línea %d: «%s» es demasiado largo (hasta %d caracteres y %d palabras)." % (
                    number, member[:20], MAX_MEMBER_CHARS, MAX_MEMBER_WORDS))
            if not _MEMBER_CHARS.fullmatch(member):
                raise ValueError("Línea %d: «%s» tiene caracteres no permitidos. Usá solo letras, números, espacios "
                                 "y guiones, separando las palabras con comas." % (number, member[:20]))
            if not _words(member):
                raise ValueError("Línea %d: «%s» no tiene ninguna palabra de %d letras o más, así que nunca "
                                 "coincidiría." % (number, member, MIN_WORD))


def parse_synonyms(raw):
    """Groups of folded members from the setting. Tolerant: whatever is invalid or over the limits is skipped."""
    groups = []
    for line in (raw or "")[:MAX_SYNONYM_CHARS].replace("\r", "").split("\n")[:MAX_SYNONYM_GROUPS]:
        members = []
        for member in line.split(",")[:MAX_GROUP_MEMBERS]:
            words = _words(member[:MAX_MEMBER_CHARS])[:MAX_MEMBER_WORDS]
            if words and " ".join(words) not in members:
                members.append(" ".join(words))
        if len(members) >= 2:
            groups.append(tuple(members))
    return groups


def all_synonyms(raw=None):
    """The built-in groups followed by the company's."""
    return [tuple(" ".join(_words(member)) for member in group if _words(member)) for group in DEFAULT_SYNONYMS] \
        + parse_synonyms(raw)


# --- query groups ------------------------------------------------------------------------------------------------

def query_groups(message, synonyms=None, limit=12):
    """The question as a list of groups; each group is a list of alternatives; an alternative is a tuple of words.

    A group matches a chunk when ANY alternative does, and an alternative matches when ALL its words do. A word of the
    question that belongs to a synonym group becomes that whole group (merged when several words share it).
    """
    synonyms = all_synonyms() if synonyms is None else synonyms
    tokens = [word for word in (_word(raw) for raw in fold(message).split()) if len(word) >= MIN_WORD]
    tokens = [word for word in tokens if word not in FILLER]
    # Different words only, and no more than MAX_QUERY_TOKENS of them: the cost below grows with the number of words.
    tokens = list(dict.fromkeys(tokens))[:MAX_QUERY_TOKENS]
    groups, consumed = [], set()  # consumed: token positions already used by a multi-word member

    def add(alternatives):
        for group in groups:
            if any(alternative in group for alternative in alternatives):
                group.extend(alt for alt in alternatives if alt not in group)
                return
        groups.append(list(alternatives))

    for index, token in enumerate(tokens):
        if index in consumed:
            continue
        alternatives = []
        for members in synonyms:
            for member in members:
                words = member.split()
                window = tokens[index:index + len(words)]
                if len(window) == len(words) and all(_keys(w) & _keys(k) for w, k in zip(window, words)):
                    alternatives += [tuple(m.split()) for m in members if tuple(m.split()) not in alternatives]
                    if len(words) > 1:
                        consumed.update(range(index, index + len(words)))
                    break
        if not alternatives:
            alternatives = [(variant,) for variant in word_variants(token)]
        elif (token,) not in alternatives and not (consumed & {index}):
            alternatives.insert(0, (token,))
        add(alternatives[:MAX_GROUP_ALTERNATIVES])
    return groups[:limit]


def group_tsquery(alternatives):
    """`((a & b) | (c))`: only letters and digits, so it is safe to hand to to_tsquery."""
    return "(" + " | ".join("(" + " & ".join(alt) + ")" for alt in alternatives) + ")"


# --- conversation memory (follow-up questions) -------------------------------------------------------------------
# «¿Y quién los gestiona?» says nothing by itself: «los» is the backups of the previous turn. When the message depends
# on the conversation, the search is repeated with the terms of the previous question and of the documents the previous
# answer cited. This is plain Python: no call to the provider, and the terms go through `query_groups`, so they are
# letters and digits only like everything else that reaches a tsquery.
MAX_CONTEXT_QUESTIONS = 2       # previous questions of the same person that can lend terms
CONTEXT_QUESTION_CHARS = 300    # of each previous question
MAX_CONTEXT_TITLES = 3          # titles of the documents the previous answer cited
CONTEXT_TITLE_CHARS = 120
MAX_CONTEXT_GROUPS = 8          # query terms that come from the conversation, however long it was
CONTEXT_OWN_WEIGHT = 2          # what the question names counts double against what the conversation lends
SHORT_FOLLOW_UP_WORDS = 5      # a message this short whose own search is weak may be a follow-up too
INTERROGATIVES = frozenset("""que quien quienes cual cuales cuando donde como cuanto cuanta cuantos cuantas porque""".split())

# Words that carry no topic when they come from a previous question («dónde están los backups» is about backups).
CONTEXT_STOPWORDS = FILLER | INTERROGATIVES | frozenset("""por para con sin los las una uno unos unas del este esta estos estas ese esa esos
esas eso esto estan hay son ser era fue tiene tienen tengo hace hacen puedo debo mas muy pero tambien entonces ademas sobre
entre desde hasta algun alguna algo nada todo todos todas cada otro otra otros otras hacer hago hacen asi aqui alli ahi""".split())

_TOPIC_CHANGE = re.compile(
    r"\botr[ao]s?\s+(?:cosa|tema|pregunta|consulta|duda)s?\b|\bcambi\w*\s+de\s+tema\b|\bnuev[ao]s?\s+(?:pregunta|consulta|tema)\b"
    r"|\bpor\s+otro\s+lado\b|\bdejando\s+(?:eso|esto)\b|\bolvida(?:te|lo)\s+de\s+(?:eso|esto|lo anterior)\b")
_CONTINUATION = re.compile(r"^\W*(?:y|e|pero|entonces|ademas|tambien|osea|o\s+sea|asi\s+que|en\s+ese\s+caso)(?!\w)")
_ANAPHORA = re.compile(
    r"\b(?:eso|esto|esos|esas|estos|estas|ese|esa|aquello|aquel|aquella|aquellos|aquellas|ellos|ellas|lo\s+mismo|la\s+misma|"
    r"el\s+mismo|los\s+mismos|las\s+mismas|al\s+respecto|sobre\s+eso|de\s+eso|de\s+ahi|anterior|mencionad[oa]s?|dicho|dicha|"
    r"dichos|dichas|arriba)\b"
    # «¿quién los gestiona?», «¿dónde las guardan?»: the pronoun right after the question word has no noun of its own.
    r"|\b(?:quien|quienes|cuando|donde|como|cuanto|cuantos|cuantas|que)\s+(?:los|las|lo|les|le)\s+\w+")


def follow_up_kind(message):
    """How much a message leans on the conversation: "dependent" (it says so itself: «y…», «eso», «quién los…», or it is
    a question without a subject like «¿cuánto demora?»), "short" (a few words: only worth the conversation when its own
    search comes back weak) or None (self-contained, a greeting, or an explicit change of subject)."""
    text = fold(str(message or ""))[:400]
    words = [word for word in (_word(raw) for raw in text.split()) if word]
    if not words or is_greeting(message) or _TOPIC_CHANGE.search(text):
        return None
    if _CONTINUATION.match(text) or _ANAPHORA.search(text):
        return "dependent"
    content = [word for word in words if len(word) >= MIN_WORD and word not in CONTEXT_STOPWORDS and not word.isdigit()]
    if len(words) <= 4 and len(content) <= 1 and words[0] in INTERROGATIVES:
        return "dependent"
    return "short" if content and len(words) <= SHORT_FOLLOW_UP_WORDS else None  # thanks and the like have no topic


def has_topic(message):
    """Whether the message names something: a word that is not a question word, filler or glue. «¿Sobre qué me podés
    guiar?» and «gracias» do not (the search by meaning is not worth a request for them)."""
    words = (_word(raw) for raw in fold(str(message or ""))[:400].split())
    return any(word and len(word) >= MIN_WORD and word not in CONTEXT_STOPWORDS and not word.isdigit() for word in words)


def context_groups(questions, titles=(), synonyms=None):
    """Query groups (as `query_groups`) taken from the previous questions and the cited titles of the conversation.

    Question words that carry no topic are dropped; the questions come first (most recent first), then the titles; at
    most MAX_CONTEXT_GROUPS in all. The same text goes through `query_groups`, so a word of a synonym group becomes the
    whole group and groups that share words are merged."""
    pieces = []
    for question in list(questions or ())[:MAX_CONTEXT_QUESTIONS]:
        kept = [raw for raw in fold(str(question or ""))[:CONTEXT_QUESTION_CHARS].split() if _word(raw) not in CONTEXT_STOPWORDS]
        pieces.append(" ".join(kept))
    for title in list(titles or ())[:MAX_CONTEXT_TITLES]:
        pieces.append(" ".join(raw for raw in fold(clean_title(title, CONTEXT_TITLE_CHARS)).split() if _word(raw) not in CONTEXT_STOPWORDS))
    return query_groups(" ".join(pieces), synonyms, limit=MAX_CONTEXT_GROUPS)


def combine_groups(current, context, dominant=False):
    """(groups, weights) for one search over the question and the conversation; a conversation term the question
    already has is not repeated. The question's own terms weigh 2 and the conversation's 1: a follow-up is about the
    previous topic unless it says otherwise, but what it names still counts for more. With `dominant` the question's
    terms weigh more than all the conversation's together, so a chunk that matches none of them (it only matches the
    previous topic) always ranks below any that matches one: a result with a score under the weight of one question
    term has nothing of the question in it."""
    context = [group for group in context if group not in current][:MAX_CONTEXT_GROUPS]
    weight = len(context) + 1 if dominant else CONTEXT_OWN_WEIGHT
    return list(current) + context, [weight] * len(current) + [1] * len(context)


# --- text that comes from outside --------------------------------------------------------------------------------
# NUL and the other control characters (everything below U+0020 except tab, line feed and carriage return; DEL; the C1
# range) and lone UTF-16 surrogates: PostgreSQL refuses NUL in text and UTF-8 cannot encode a lone surrogate, so they
# would turn a message into a server error, and they have no place in a question or a ticket anyway.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def strip_control_chars(value):
    """The text without NUL, control characters and lone surrogates (tab, newline and carriage return are kept)."""
    return _CONTROL_CHARS.sub("", value)


# Addresses that can never receive mail (reserved for documentation and tests) or that say «example» in Spanish or
# English: the sample contacts the module installs use them, and the checklist asks the administrator to replace them.
PLACEHOLDER_TLDS = frozenset(("test", "example", "invalid", "localhost"))
PLACEHOLDER_WORDS = frozenset(("example", "ejemplo"))


def is_placeholder_email(email):
    """Whether an address is a sample (empty, without a domain, `.test`, `.example`, `example.com`, `ejemplo.*`...)."""
    address = (email or "").strip().lower()
    local, separator, domain = address.rpartition("@")
    domain = domain.rstrip(".")
    if not separator or not local or "." not in domain:
        return True
    labels = domain.split(".")
    return labels[-1] in PLACEHOLDER_TLDS or any(label in PLACEHOLDER_WORDS for label in labels[:-1])



# --- greetings ---------------------------------------------------------------------------------------------------
# The model sometimes opens every answer with "¡Hola!" although the person just asked a question. The prompt forbids
# it; this is the safety net: a greeting is dropped from the START of an answer unless the person greeted first.
_GREETING_WORD = re.compile(r"hol+a+|buen(?:os|as)?|hey+|hello|hi|saludos|ola|ey")
_GREETING_PREFIXES = ("hola", "buenas", "buenos", "buen", "hey", "hello", "hi", "saludos")
_NAME = r"(?-i:[A-ZÁÉÍÓÚÑ][\w'’-]*)"  # a capitalized word, whatever the rest of the pattern's case rules
_LEADING_GREETING = re.compile(
    r"\s*[¡!]*\s*(?:hol+a+|buen(?:os|as)?(?:\s+d[ií]as?|\s+tardes|\s+noches)?|hey+|hello|hi|saludos)(?!\w)"
    # an optional name, only when punctuation follows it ("Hola, Ana!"), so "Hola Para crear..." keeps its "Para"
    r"(?:[ \t]*,?[ \t]*" + _NAME + r"(?:[ \t]+" + _NAME + r")?(?=[ \t]*[!.,:;]))?"
    r"(?:[ \t]*[!.,:;]+|(?=[ \t]*[¡¿])|[ \t]*\Z)"
    r"[ \t]*(?:[☀-➿⭐️‍\U0001f300-\U0001faff][ \t]*)*(?:\r?\n)*\s*", re.IGNORECASE)


def is_greeting(message):
    """Whether the person greeted: a greeting word among the first three words ('Hola, ¿cómo creo un ticket?')."""
    words = re.findall(r"[a-z]+", fold(str(message or ""))[:80])[:3]
    return any(_GREETING_WORD.fullmatch(word) for word in words)


def strip_greeting(answer):
    """The answer without a greeting at its start ('¡Hola, Ana! Para crear...' -> 'Para crear...').

    Only the opening greeting goes, with the name and punctuation that belong to it; an answer that is nothing but a
    greeting, or that merely starts with a word that looks like one ('Buenas prácticas: ...'), is returned untouched.
    """
    match = _LEADING_GREETING.match(answer)
    if not match:
        return answer
    rest = answer[match.end():]
    if not any(char.isalnum() for char in rest):
        return answer
    return rest[:1].upper() + rest[1:]


def may_be_greeting(prefix):
    """Whether text that starts like this could still turn out to open with a greeting (so a stream holds it back)."""
    head = fold(prefix).lstrip(" \t\r\n¡!")
    if not head:
        return True
    word = re.match(r"[a-z]*", head).group()
    if len(word) == len(head):  # the first word may not be finished yet
        return any(known.startswith(word) for known in _GREETING_PREFIXES) or bool(re.fullmatch(r"hol+a*|hey+", word))
    return bool(_GREETING_WORD.fullmatch(word))


# --- the context stored with each chunk --------------------------------------------------------------------------
_EXTENSION = re.compile(r"\.(?:docx?|pdf|txt|md|csv|html?|xlsx?|pptx?|odt|rtf)\Z", re.IGNORECASE)
MAX_CONTEXT_CHARS = 300


def clean_title(name, limit=80):
    """A file name as a human title: no extension, one line, bounded."""
    title = " ".join(str(name or "").split())
    title = _EXTENSION.sub("", title)
    return title[:limit].rstrip()


def search_context(name, path="", folder=""):
    """Text indexed with MORE weight than the body: document title, sub-folders and connected folder."""
    parts = [clean_title(name, 150), " ".join(str(path or "").replace("/", " ").replace("\\", " ").split()),
             " ".join(str(folder or "").split())]
    return " · ".join(part for part in parts if part)[:MAX_CONTEXT_CHARS]
