"""Vector helpers of the semantic search: no Odoo, no database, no third-party packages (standard library only).

The embeddings come from the AI provider's `/embeddings` endpoint and are kept in PostgreSQL as plain `bytea`
(little-endian float32, normalized to unit length), so the cosine similarity of two vectors is their dot product and
the database needs no extension (no pgvector). Everything here is pure functions and two tiny thread-safe helpers, so
it is tested without Odoo (tests/test_vectors.py).
"""
import array
import hashlib
import heapq
import math
import sys
import threading
import time
from collections import OrderedDict
from operator import mul

# The text sent to the provider for a fragment is its context (title, sub-folders, folder) and its body, cut here. A
# fragment is about 1000 characters; the cut only protects against an unusual one and keeps a request far below the
# input limit of the models (2048 tokens for the smallest).
EMBED_TEXT_CHARS = 4000
# Characters of a question sent to be embedded.
QUERY_TEXT_CHARS = 1000
MIN_DIMS = 8
MAX_DIMS = 3072
# One request carries at most this many texts and characters (a response of 16 vectors of 3072 numbers is ~1 MB, below
# the 2 MB the provider client reads).
BATCH_ITEMS = 16
BATCH_CHARS = 32000
# How many floats a query may scan (chunks x dimensions): 16 million is 64 MB of vectors, about 20,000 fragments of 768
# dimensions. Measured in pure Python: ~1 s per question at that size (docs/ARCHITECTURE.md). Above it the semantic
# search pauses (the text search keeps working) and the administrator is told.
MAX_SCAN_FLOATS = 16_000_000
# ...and never more than this many fragments, however small the vectors (a tiny size would otherwise allow millions).
MAX_SCAN_CHUNKS = 50_000
# A semantic candidate must score at least this many standard deviations above the mean similarity of the visible
# fragments (similarities of one question against a corpus are tightly packed, a fixed cosine would not carry from one
# model to another); with fewer fragments than MIN_STATS_CHUNKS the statistic means nothing and a plain floor is used.
# 1.5 was chosen by running the golden set at 2.0 and at 1.5 (docs/EVALUACION.md): at 2.0 a paraphrase whose best fragment
# stood at 1.81 deviations was left out; at 1.5 it passes and nothing else got worse.
SEMANTIC_MIN_Z = 1.5
SEMANTIC_FLOOR = 0.2
MIN_STATS_CHUNKS = 8
RRF_K = 60

_SUMPROD = getattr(math, "sumprod", None)  # Python 3.12+: a dot product in C
_BIG_ENDIAN = sys.byteorder == "big"
assert array.array("f").itemsize == 4


def scan_capacity(dims):
    """How many fragments one question may score at this vector size."""
    return min(MAX_SCAN_FLOATS // max(int(dims), 1), MAX_SCAN_CHUNKS)


def embedding_text(context, content):
    """The exact text embedded for a fragment. The database computes the same string to find stale vectors (see
    buddy_embedding.PENDING_SQL): keep both in step."""
    return ((context or "") + "\n" + (content or ""))[:EMBED_TEXT_CHARS]


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(values):
    """A unit-length float32 array from the numbers a provider returned. ValueError when it cannot be a vector."""
    if not isinstance(values, (list, tuple)) or not MIN_DIMS <= len(values) <= MAX_DIMS:
        raise ValueError("Vector inválido.")
    try:
        vector = array.array("f", values)
    except (TypeError, OverflowError) as exc:  # a string, None or a number float32 cannot hold
        raise ValueError("Vector inválido.") from exc
    norm = math.sqrt(dot(vector, vector))
    if not math.isfinite(norm) or norm < 1e-9:
        raise ValueError("Vector inválido.")
    return array.array("f", (value / norm for value in vector))


def pack(vector):
    """The bytes stored in the database: float32, little-endian."""
    data = array.array("f", vector)
    if _BIG_ENDIAN:
        data.byteswap()
    return data.tobytes()


def unpack(blob):
    data = array.array("f")
    data.frombytes(blob)
    if _BIG_ENDIAN:
        data.byteswap()
    return data


def dot(first, second):
    return _SUMPROD(first, second) if _SUMPROD else sum(map(mul, first, second))


def score_rows(query, rows, dims):
    """Cosine similarity of the (unit) `query` against each (chunk_id, document_id, sequence, blob) row.

    Rows whose vector does not have exactly `dims` numbers are skipped (a different model left them behind).
    Returns [(similarity, chunk_id, document_id, sequence)] in no particular order.
    """
    scored, size = [], dims * 4
    for chunk_id, document_id, sequence, blob in rows:
        if blob is None or len(blob) != size:
            continue
        scored.append((dot(query, unpack(blob)), chunk_id, document_id, sequence))
    return scored


def select_semantic(scored, limit, min_z=SEMANTIC_MIN_Z, floor=SEMANTIC_FLOOR):
    """The `limit` most similar fragments that stand out from the rest, best first.

    `scored` is the output of `score_rows`. With MIN_STATS_CHUNKS fragments or more a candidate must be `min_z`
    standard deviations above the mean (and above the floor); with fewer only the floor applies.
    """
    count = len(scored)
    if not count or limit <= 0:
        return []
    cut = floor
    if count >= MIN_STATS_CHUNKS:
        mean = sum(item[0] for item in scored) / count
        deviation = math.sqrt(sum((item[0] - mean) ** 2 for item in scored) / count)
        cut = max(floor, mean + min_z * deviation)
    best = heapq.nlargest(limit, scored, key=lambda item: (item[0], -item[1]))
    return [item for item in best if item[0] >= cut]


def rrf(rankings, weights=None, k=RRF_K):
    """Reciprocal Rank Fusion: [(id, score)] best first from several best-first lists of ids.

    A chunk's score is the sum over the lists of weight / (k + position), positions starting at 1, so a chunk that
    both searches like beats one only a single search found, and no raw score (text rank and cosine live on different
    scales) is ever compared. Ties keep the order of the first list that mentions the chunk.
    """
    totals, first = {}, {}
    for number, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else float(weights[number])
        for position, identifier in enumerate(ranking, 1):
            totals[identifier] = totals.get(identifier, 0.0) + weight / (k + position)
            first.setdefault(identifier, (number, position))
    return sorted(totals.items(), key=lambda item: (-item[1], first[item[0]]))


def batches(items, max_items=BATCH_ITEMS, max_chars=BATCH_CHARS):
    """Groups of (key, text) pairs for one request each, within both limits (a single long text goes alone)."""
    group, size = [], 0
    for item in items:
        length = len(item[1])
        if group and (len(group) >= max_items or size + length > max_chars):
            yield group
            group, size = [], 0
        group.append(item)
        size += length
    if group:
        yield group


def parse_embeddings(data, expected):
    """The unit vectors of an OpenAI-style `/embeddings` answer, in the order of the inputs.

    ValueError (never the provider's text) when the answer is not what was asked for: wrong count, non-numeric,
    zero or mixed lengths. Gemini leaves `index` empty on the first item, so the position is used unless every item
    carries a consistent index.
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != expected:
        raise ValueError("Respuesta de embeddings inválida.")
    indexes = [item.get("index") if isinstance(item, dict) else None for item in items]
    if all(type(index) is int for index in indexes) and sorted(indexes) == list(range(expected)):
        items = [item for _index, item in sorted(zip(indexes, items), key=lambda pair: pair[0])]
    vectors = [normalize(item.get("embedding") if isinstance(item, dict) else None) for item in items]
    if len({len(vector) for vector in vectors}) > 1:
        raise ValueError("Respuesta de embeddings inválida.")
    return vectors


def query_key(text):
    """The cache key of a question: lower case with the spaces collapsed, then hashed (the cache never keeps the text of
    a question in the memory of the process)."""
    return hashlib.sha256(" ".join((text or "").lower().split())[:QUERY_TEXT_CHARS].encode("utf-8")).hexdigest()


class QueryCache:
    """A small thread-safe LRU of question vectors that forgets them after `ttl` seconds. The key says which model and
    dimensions made it. The clock is injectable for tests."""

    def __init__(self, size=256, ttl=900.0, clock=time.monotonic):
        self.size, self.ttl, self.clock = size, ttl, clock
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            if self.clock() >= found[0]:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return found[1]

    def put(self, key, value):
        with self._lock:
            self._items[key] = (self.clock() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        return len(self._items)


class Breaker:
    """Stops asking a provider that keeps failing (so every question does not wait for its timeout again).

    After `limit` failures in a row the key is closed for `pause` seconds; then ONE request is let through as a probe
    (the others keep waiting `probe` seconds for its verdict): a success opens the key again at once, a failure closes it
    for a new pause. The clock is injectable for tests.
    """

    def __init__(self, limit=3, pause=60.0, clock=time.monotonic, probe=10.0):
        self.limit, self.pause, self.clock, self.probe = limit, pause, clock, probe
        self._state = {}
        self._lock = threading.Lock()

    def allowed(self, key):
        with self._lock:
            failures, until = self._state.get(key, (0, 0.0))
            if failures < self.limit:
                return True
            now = self.clock()
            if now < until:
                return False
            self._state[key] = (failures, now + self.probe)  # the probe: nobody else goes through meanwhile
            return True

    def failure(self, key):
        with self._lock:
            failures, _until = self._state.get(key, (0, 0.0))
            failures += 1
            self._state[key] = (failures, self.clock() + self.pause if failures >= self.limit else 0.0)
            return failures

    def success(self, key):
        with self._lock:
            self._state.pop(key, None)
