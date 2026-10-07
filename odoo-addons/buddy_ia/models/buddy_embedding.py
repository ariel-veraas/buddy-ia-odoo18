"""Semantic search: the vectors of the fragments (kept in plain PostgreSQL, no pgvector) and how they are used.

What is stored: one row per fragment with its vector (`bytea`, little-endian float32, unit length), the model and the
number of dimensions that made it, and the hash of the exact text that was embedded. Vectors made by different models
(or sizes) are never compared: a search only reads the rows of the model and size it asked its question with, and a
change of model or size makes every other row «pending» so the indexer replaces it.

Privacy: embedding a fragment SENDS ITS TEXT to the AI provider, for every fragment of every connected folder (also the
restricted ones), not only for those a question retrieves. That is why the feature is off unless an administrator turns
it on (see docs/SECURITY.md). Permissions are applied BEFORE anything is scored: the similarity is only computed over
the fragments the person's own record rules let them read.

Nobody reads this model through the ORM or RPC: the access list gives it to nobody (the engine and the indexer use
SQL with the server's rights).
"""
import logging
import time
from collections import OrderedDict

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.tools import SQL
from . import buddy_vectors as vec
from .buddy_insight import flag
from .buddy_provider import ProviderError, default_embedding_dims, default_embedding_model, supports_embeddings

_logger = logging.getLogger(__name__)

SEMANTIC_PARAM = "buddy_ia.semantic_search"
MODEL_PARAM = "buddy_ia.embedding_model"
DIMS_PARAM = "buddy_ia.embedding_dims"
# Technical, not on the screen (like buddy_ia.text_min_score): how many standard deviations above the mean similarity a
# fragment must be to count. Lower finds more (and brings more noise), higher is stricter.
MIN_Z_PARAM = "buddy_ia.semantic_min_z"
# What the last indexing pass said: «<code>|<time>». Codes: ok, limit, error:<provider code>.
STATE_PARAM = "buddy_ia.semantic_state"
# Time the question's embedding may take (the whole call). Longer than this the question is answered with the text
# search alone; the answer to the question must not wait for an optional improvement.
QUERY_TIMEOUT = 3.0
# Time that must stay for the answer itself (provider call of up to 30 s inside the 35 s of the query).
ANSWER_RESERVE = 20.0
# One indexing request.
INDEX_CALL_TIMEOUT = 30.0
# Indexing pass limits: how much of a pass may be spent and how many fragments one pass may send.
INDEX_FETCH = 64
MAX_BAD_PER_PASS = 5
# Fragments counted when the dimensions are not known yet (a provider that decides the size itself).
ASSUMED_DIMS = 1536
# Time left below which a pass starts no new request.
MIN_REQUEST_SECONDS = 3.0
# A question reads the vectors it may see in batches of this many rows (a batch is decoded and dropped: the memory of a
# question is a batch, not the whole corpus) and gives up after this long.
SCAN_BATCH = 1500
SCAN_SECONDS = 2.5
# A fragment the provider refused is not offered again for this long (unless its text changes).
REJECT_SECONDS = 6 * 3600
# The state parameter is rewritten when it changes or after this long, not on every pass.
STATE_REFRESH_SECONDS = 6 * 3600
# «Too many fragments» is logged at most this often per database.
TOO_BIG_LOG_SECONDS = 600
# HTTP statuses that mean «this input is the problem» (the rest of the batch can still go).
BAD_INPUT_STATUS = {400, 413, 422}
# The hash of the embedded text, computed by the database exactly as `vec.embedding_text` + `vec.text_hash` do.
HASH_SQL = ("encode(sha256(convert_to(left(coalesce(c.context_text, '') || E'\\n' || coalesce(c.content, ''), %d), "
            "'UTF8')), 'hex')" % vec.EMBED_TEXT_CHARS)

# Per process (a worker): the vectors of recent questions and the memory of a provider that keeps failing.
_QUERIES = vec.QueryCache(256)
_BREAKER = vec.Breaker(limit=3, pause=60.0)
_REJECTED = OrderedDict()
_TOO_BIG_LOGGED = {}


class BuddyEmbedding(models.Model):
    _name = "buddy.embedding"
    _description = "Vector de significado de un fragmento"

    chunk_id = fields.Many2one("buddy.chunk", required=True, ondelete="cascade")  # the unique constraint is its index
    content_hash = fields.Char(required=True)
    embedding_model = fields.Char(required=True)
    dims = fields.Integer(required=True)
    vector = fields.Binary(attachment=False, required=True)

    _sql_constraints = [("chunk_unique", "unique(chunk_id)", "Un fragmento tiene un solo vector.")]

    # ---------------------------------------------------------------------------------- configuration
    @api.model
    def _semantic_cfg(self):
        """{model, dims, provider} when the semantic search is on and can work, else None. Never raises."""
        parameters = self.env["ir.config_parameter"].sudo()
        if not flag(parameters.get_param(SEMANTIC_PARAM)):
            return None
        provider = parameters.get_param("buddy_ia.provider") or "anthropic"
        if not supports_embeddings(provider) or not parameters.get_param("buddy_ia.api_key"):
            return None
        model = str(parameters.get_param(MODEL_PARAM) or "").strip()[:200] or default_embedding_model(provider)
        if not model:
            return None
        try:
            dims = int(parameters.get_param(DIMS_PARAM) or 0)
        except ValueError:
            dims = 0
        if not vec.MIN_DIMS <= dims <= vec.MAX_DIMS:
            dims = default_embedding_dims(provider)
        try:
            min_z = float(parameters.get_param(MIN_Z_PARAM) or vec.SEMANTIC_MIN_Z)
        except ValueError:
            min_z = vec.SEMANTIC_MIN_Z
        if not 0.0 <= min_z <= 6.0:  # also rejects nan and inf
            min_z = vec.SEMANTIC_MIN_Z
        return {"model": model, "dims": dims, "provider": provider, "min_z": min_z}

    @api.model
    def _semantic_requested(self):
        """Whether an administrator turned the search by meaning on (it may still be unable to work: see `_semantic_cfg`)."""
        return flag(self.env["ir.config_parameter"].sudo().get_param(SEMANTIC_PARAM))

    @api.model
    def _self_test_semantic(self, timeout):
        """(ok, readable line) for «Probar conexión»: one tiny embeddings request with the configured model."""
        cfg = self._semantic_cfg()
        if not cfg:
            return False, _("Búsqueda por significado: no se puede usar con este proveedor o falta el modelo de embeddings (Buddy busca solo por palabras).")
        try:
            vectors = self.env["buddy.provider"]._embed(["prueba"], cfg["model"], max(timeout, 1.0), cfg["dims"])
        except ProviderError as exc:
            return False, _("Búsqueda por significado: %(error)s", error=str(exc))
        except TimeoutError:
            return False, _("Búsqueda por significado: el proveedor tardó demasiado en calcular un vector.")
        except Exception as exc:  # noqa: BLE001
            return False, _("Búsqueda por significado: el proveedor no devolvió un vector válido (%(detail)s).", detail=type(exc).__name__)
        if cfg["dims"] and len(vectors[0]) != cfg["dims"]:
            return False, _("Búsqueda por significado: el proveedor no respeta el tamaño de vector pedido (%(dims)s).", dims=cfg["dims"])
        return True, _("Búsqueda por significado OK (%(model)s, %(dims)s números por fragmento)", model=cfg["model"], dims=len(vectors[0]))

    @api.model
    def _dims_sql(self, cfg):
        """The SQL condition «this row has the current model and size» on alias `e`, and its parameters."""
        if cfg["dims"]:
            return "e.embedding_model = %s AND e.dims = %s", (cfg["model"], cfg["dims"])
        return "e.embedding_model = %s", (cfg["model"],)

    # ---------------------------------------------------------------------------------- question side
    @api.model
    def _breaker_key(self, cfg):
        """What the cache of question vectors and the pause after failures are kept per: the database, the model and size and
        the destination (provider and address), so a vector or an outage never carries over to another provider."""
        return (self.env.cr.dbname, cfg["model"], cfg["dims"], cfg.get("provider"),
                (self.env["ir.config_parameter"].sudo().get_param("buddy_ia.api_base") or "").strip())

    @api.model
    def _ask_vector(self, text, cfg, timeout):
        """The unit vector of a question: one small request to the provider."""
        provider = self.env["buddy.provider"]
        return provider._embed([text[:vec.QUERY_TEXT_CHARS]], cfg["model"], timeout, cfg["dims"], provider._settings())[0]

    @api.model
    def _semantic_rows(self, text, sources, limit, cfg, deadline=None):
        """The fragments closest in meaning to `text` among the ones THIS user may read: (rows, info).

        `rows` are (similarity, chunk_id, document_id, sequence) best first (empty when nothing stands out); `info` is
        {"state", "ms"} with state `used` (the search ran), `paused` (the provider kept failing: not even tried),
        `failed`, `too_big` (more vectors than the scan limit), `no_time`. It never raises: any failure means «use the text
        search alone». The user's record rules decide which fragments are scored (the same subselect as the text search).
        """
        started = time.monotonic()

        def done(state, rows=()):
            return list(rows), {"state": state, "ms": int((time.monotonic() - started) * 1000)}

        if self.env.su:
            # Defense in depth: scored under the superuser the record rules would not apply and every fragment would count.
            _logger.warning("Buddy: la búsqueda por significado se pidió sin las reglas de un usuario: no se usa")
            return done("failed")
        key = self._breaker_key(cfg)
        cache_key = key + (vec.query_key(text),)
        vector = _QUERIES.get(cache_key)  # a question already asked needs no provider, even while it is paused
        if vector is None:
            if not _BREAKER.allowed(key):
                return done("paused")
            timeout = QUERY_TIMEOUT
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic() - ANSWER_RESERVE)
            if timeout < 0.5:
                return done("no_time")
            try:
                vector = self._ask_vector(text, cfg, timeout)
                _QUERIES.put(cache_key, vector)
                _BREAKER.success(key)  # only a real answer from the provider says it is back
            except (ProviderError, TimeoutError, ValueError, OSError) as exc:
                if not (isinstance(exc, ProviderError) and exc.status in BAD_INPUT_STATUS):  # a refused question is not an outage
                    _BREAKER.failure(key)
                _logger.warning("Buddy: la búsqueda por significado falló (%s)", getattr(exc, "code", type(exc).__name__))
                return done("failed")
            except Exception as exc:  # noqa: BLE001 - an optional improvement never breaks a question
                _logger.warning("Buddy: la búsqueda por significado falló (%s)", type(exc).__name__)
                return done("failed")
        dims = len(vector)
        capacity = vec.scan_capacity(dims)
        scan_until = time.monotonic() + SCAN_SECONDS
        if deadline is not None:
            scan_until = min(scan_until, deadline - ANSWER_RESERVE)
        scored = []
        try:
            with self.env.cr.savepoint():
                # Pending writes (a new fragment, a moved one) must be in the database before the raw query.
                self.env["buddy.chunk"].flush_model(["document_id", "sequence", "source_id"])
                visible = self.env["buddy.chunk"]._search([("source_id", "in", sources.ids)])
                # Step 1, in the database: which fragments this person may read (their record rules, the same subselect as the
                # text search) have a vector of this model and size. With the statistics of a table that was just filled
                # (the backlog is being indexed and autovacuum has not looked yet) PostgreSQL estimates one row and picks a
                # nested loop that is quadratic here (measured: 5,000 fragments took 2.3 s, 20,000 over 20 minutes); a hash
                # join is linear whatever the statistics say. Only ids come out, so the limit is checked before any vector
                # is read.
                # The previous value is put back after the query (and by the savepoint, if it failed: a setting changed with
                # LOCAL inside a savepoint is undone when the savepoint is rolled back).
                self.env.cr.execute("SHOW enable_nestloop")
                nestloop = self.env.cr.fetchone()[0]
                self.env.cr.execute("SELECT set_config('enable_nestloop', 'off', true)")
                self.env.cr.execute(SQL(
                    "SELECT e.chunk_id FROM buddy_embedding e WHERE e.embedding_model = %s AND e.dims = %s AND e.chunk_id IN %s "
                    "ORDER BY e.chunk_id LIMIT %s", cfg["model"], dims, visible.subselect(), capacity + 1))
                ids = [row[0] for row in self.env.cr.fetchall()]
                self.env.cr.execute("SELECT set_config('enable_nestloop', %s, true)", (nestloop,))
                if len(ids) > capacity:
                    self._log_too_big(capacity)
                    return done("too_big")
                # Step 2: those ids, a batch at a time (a batch is decoded and dropped: the memory of a question is a batch, not
                # the corpus). They are the permitted ones, carried from one query to the next, never a list built from anything
                # else; each vector is looked up by its index.
                for start in range(0, len(ids), SCAN_BATCH):
                    self.env.cr.execute(
                        "SELECT e.chunk_id, c.document_id, c.sequence, e.vector FROM unnest(%s::int[]) AS t(id) "
                        "JOIN buddy_embedding e ON e.chunk_id = t.id JOIN buddy_chunk c ON c.id = t.id",
                        (ids[start:start + SCAN_BATCH],))
                    scored.extend(vec.score_rows(vector, self.env.cr.fetchall(), dims))  # only (similarity, ids) are kept
                    if start + SCAN_BATCH < len(ids) and time.monotonic() > scan_until:
                        return done("no_time")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Buddy: no pude leer los vectores (%s)", type(exc).__name__)
            return done("failed")
        return done("used", vec.select_semantic(scored, limit, min_z=cfg.get("min_z", vec.SEMANTIC_MIN_Z)))

    @api.model
    def _log_too_big(self, capacity):
        """One line per ten minutes and database (a question per second would fill the log with the same news)."""
        now, dbname = time.monotonic(), self.env.cr.dbname
        if now - _TOO_BIG_LOGGED.get(dbname, -TOO_BIG_LOG_SECONDS) >= TOO_BIG_LOG_SECONDS:
            _TOO_BIG_LOGGED[dbname] = now
            _logger.warning("Buddy: demasiados fragmentos para la búsqueda por significado (más de %s): se usa solo el texto", capacity)

    # ---------------------------------------------------------------------------------- indexing
    @api.model
    def _pending_sql(self, cfg):
        """FROM/WHERE of the fragments that need a vector: none yet, another model or size, or the text changed."""
        current, params = self._dims_sql(cfg)
        return ("FROM buddy_chunk c JOIN buddy_source s ON s.id = c.source_id AND s.active "
                "LEFT JOIN buddy_embedding e ON e.chunk_id = c.id "
                "WHERE (e.id IS NULL OR NOT (%s) OR e.content_hash <> %s)" % (current, HASH_SQL)), params

    @api.model
    def _pending_count(self, cfg):
        query, params = self._pending_sql(cfg)
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) " + query, params)
        return self.env.cr.fetchone()[0]

    @api.model
    def _active_chunk_count(self):
        self.env.flush_all()
        self.env.cr.execute("SELECT count(*) FROM buddy_chunk c JOIN buddy_source s ON s.id = c.source_id AND s.active")
        return self.env.cr.fetchone()[0]

    @api.model
    def _over_capacity(self, cfg, total=None):
        """Whether the indexed fragments would not fit the scan limit (the search would never be able to use them)."""
        total = self._active_chunk_count() if total is None else total
        return total > vec.scan_capacity(cfg["dims"] or ASSUMED_DIMS)

    @api.model
    def _store(self, rows, vectors, cfg):
        """Save (or replace) the vectors of fragments: rows are (chunk_id, hash, ...), in the order of `vectors`."""
        cr = self.env.cr
        for row, vector in zip(rows, vectors):
            try:
                with cr.savepoint():
                    cr.execute(
                        "INSERT INTO buddy_embedding (chunk_id, content_hash, embedding_model, dims, vector, create_uid, write_uid, "
                        "create_date, write_date) SELECT %s, %s, %s, %s, %s, %s, %s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC' "
                        "WHERE EXISTS (SELECT 1 FROM buddy_chunk WHERE id = %s) "
                        "ON CONFLICT (chunk_id) DO UPDATE SET content_hash = EXCLUDED.content_hash, "
                        "embedding_model = EXCLUDED.embedding_model, dims = EXCLUDED.dims, vector = EXCLUDED.vector, "
                        "write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date",
                        (row[0], row[1], cfg["model"], len(vector), vec.pack(vector), SUPERUSER_ID, SUPERUSER_ID, row[0]))
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "pgcode", None) != "23503":  # foreign key: the fragment was deleted while this ran
                    raise

    @api.model
    def _embed_group(self, group, cfg, settings, deadline):
        """Vectors for one group of (chunk_id, hash, text) rows. Raises what the provider raised."""
        timeout = min(INDEX_CALL_TIMEOUT, max(deadline - time.monotonic(), 2.0))
        vectors = self.env["buddy.provider"]._embed([row[2] for row in group], cfg["model"], timeout, cfg["dims"], settings)
        if cfg["dims"] and any(len(vector) != cfg["dims"] for vector in vectors):
            # The server ignored `dimensions`: keeping those vectors would make the same fragments pending forever.
            raise ProviderError("dimensions", _("El proveedor no respeta las dimensiones pedidas."))
        return vectors

    @api.model
    def _bad_input(self, exc):
        return isinstance(exc, ValueError) or (isinstance(exc, ProviderError) and exc.status in BAD_INPUT_STATUS)

    @api.model
    def _index_pending(self, deadline, commit=False):
        """Embed the fragments that need it, as many as the time allows. Resumable: what is left is picked up next time.

        `commit` (the cron) saves after each request, so a pass cut short by any limit loses at most the request in
        flight. Returns {"done": fragments saved, "left": fragments still pending, "state": "off|ok|limit|error:<code>"}
        and leaves the same state (with the time) in a system parameter for the settings screen.
        """
        cfg = self._semantic_cfg()
        if not cfg:
            return {"done": 0, "left": 0, "state": "off"}
        if self._over_capacity(cfg):
            self._set_state("limit")
            return {"done": 0, "left": self._pending_count(cfg), "state": "limit"}
        provider = self.env["buddy.provider"]
        try:
            settings = provider._settings()
        except ProviderError as exc:
            self._set_state("error:" + exc.code)
            return {"done": 0, "left": self._pending_count(cfg), "state": "error:" + exc.code}
        done, bad, state, last_id = 0, 0, "ok", 0
        query, params = self._pending_sql(cfg)
        while state == "ok" and deadline - time.monotonic() > MIN_REQUEST_SECONDS:
            self.env.cr.execute("SELECT c.id, " + HASH_SQL + ", left(coalesce(c.context_text, '') || E'\\n' || coalesce(c.content, ''), %d) "
                                % vec.EMBED_TEXT_CHARS + query + " AND c.id > %s ORDER BY c.id LIMIT %s", params + (last_id, INDEX_FETCH))
            fetched = self.env.cr.fetchall()
            if not fetched:
                break
            last_id = fetched[-1][0]
            fetched = [row for row in fetched if not self._recently_rejected(row)]
            for group in vec.batches([(row, row[2]) for row in fetched]):
                if deadline - time.monotonic() <= MIN_REQUEST_SECONDS:
                    break
                group = [item[0] for item in group]
                saved, failure = self._index_group(group, cfg, settings, deadline)
                done += saved
                if commit:
                    self.env.cr.commit()
                if failure:
                    bad += failure.get("bad", 0)
                    if failure.get("code") or bad >= MAX_BAD_PER_PASS:
                        state = "error:" + (failure.get("code") or "invalid_request")
                        break
        self._set_state(state)
        if commit:
            self.env.cr.commit()
        return {"done": done, "left": self._pending_count(cfg), "state": state}

    @api.model
    def _index_group(self, group, cfg, settings, deadline):
        """Embed and save one group: (fragments saved, failure) where failure is None, {"code"} (stop the pass) or
        {"bad"} (that many fragments were skipped because the provider refused them)."""
        try:
            vectors = self._embed_group(group, cfg, settings, deadline)
        except Exception as exc:  # noqa: BLE001
            if self._bad_input(exc) and len(group) > 1:
                return self._index_singly(group, cfg, settings, deadline)
            failure = self._failure(exc)
            if not failure.get("code"):
                self._reject(group[0])
            return 0, failure
        self._store(group, vectors, cfg)
        return len(group), None

    @api.model
    def _index_singly(self, group, cfg, settings, deadline):
        """A group the provider refused as a whole is retried one fragment at a time, so one bad text cannot hold back
        the rest. Each failing fragment is skipped for this pass (it is tried again in the next one)."""
        saved, bad = 0, 0
        for item in group:
            if deadline - time.monotonic() <= MIN_REQUEST_SECONDS or bad >= MAX_BAD_PER_PASS:
                break
            try:
                vectors = self._embed_group([item], cfg, settings, deadline)
            except Exception as exc:  # noqa: BLE001
                failure = self._failure(exc)
                if failure.get("code"):
                    return saved, failure
                self._reject(item)
                bad += 1
                continue
            self._store([item], vectors, cfg)
            saved += 1
        return saved, ({"bad": bad} if bad else None)

    @api.model
    def _reject(self, row):
        """Remember (in this process) a fragment the provider refused, so the next passes do not spend a request on it again
        until its text changes or REJECT_SECONDS go by."""
        _REJECTED[(self.env.cr.dbname, row[0], row[1])] = time.monotonic() + REJECT_SECONDS
        while len(_REJECTED) > 2000:
            _REJECTED.popitem(last=False)

    @api.model
    def _recently_rejected(self, row):
        key = (self.env.cr.dbname, row[0], row[1])
        until = _REJECTED.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            _REJECTED.pop(key, None)
            return False
        return True

    @api.model
    def _failure(self, exc):
        if self._bad_input(exc):
            _logger.warning("Buddy: el proveedor rechazó un fragmento para el indexado por significado")
            return {"code": None, "bad": 1}
        if isinstance(exc, TimeoutError):
            code = "provider_timeout"
        else:
            code = exc.code if isinstance(exc, ProviderError) else "provider_error"
        _logger.warning("Buddy: el indexado por significado se detuvo (%s)", code)
        return {"code": code, "bad": 0}

    @api.model
    def _set_state(self, state):
        """Remember how the last pass went. Rewritten when the state changes or after a few hours, not on every pass (every
        write of a system parameter clears the registry caches of all the workers)."""
        code, when = self._state()
        try:
            recent = bool(when) and (fields.Datetime.now() - fields.Datetime.from_string(when)).total_seconds() < STATE_REFRESH_SECONDS
        except ValueError:
            recent = False
        if code != state or not recent:
            self.env["ir.config_parameter"].sudo().set_param(STATE_PARAM, "%s|%s" % (state, fields.Datetime.now()))

    @api.model
    def _cron_index(self, budget):
        """The indexing step of the Drive cron. Returns the pass result (see `_index_pending`)."""
        return self._index_pending(time.monotonic() + budget, commit=True)

    # ---------------------------------------------------------------------------------- re-use and status
    @api.model
    def _snapshot(self, document):
        """The vectors of a document's current fragments by hash of their text, taken before they are replaced."""
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT e.content_hash, e.embedding_model, e.dims, e.vector FROM buddy_embedding e "
            "JOIN buddy_chunk c ON c.id = e.chunk_id WHERE c.document_id = %s", (document.id,))
        return {digest: (model, dims, bytes(blob)) for digest, model, dims, blob in self.env.cr.fetchall()}

    @api.model
    def _carry_over(self, chunks, old):
        """Give the new fragments of a re-read document the vectors of identical text from before (nothing to pay for a
        document that only changed in one place). Returns how many were reused."""
        if not old or not chunks:
            return 0
        reused = 0
        for chunk in chunks:
            digest = vec.text_hash(vec.embedding_text(chunk.context_text, chunk.content))
            found = old.get(digest)
            if not found:
                continue
            model, dims, blob = found
            self.env.cr.execute(
                "INSERT INTO buddy_embedding (chunk_id, content_hash, embedding_model, dims, vector, create_uid, write_uid, "
                "create_date, write_date) VALUES (%s, %s, %s, %s, %s, %s, %s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC') "
                "ON CONFLICT (chunk_id) DO NOTHING",
                (chunk.id, digest, model, dims, blob, SUPERUSER_ID, SUPERUSER_ID))
            reused += 1
        return reused

    @api.model
    def _coverage(self, source_ids=None):
        """{source_id: (fragments with a current vector, fragments)} for the active folders (all or the given ones)."""
        self.env.flush_all()
        cfg = self._semantic_cfg()
        current, params = self._dims_sql(cfg) if cfg else ("false", ())
        sql = ("SELECT c.source_id, count(*), count(e.id) FILTER (WHERE %s) FROM buddy_chunk c "
               "LEFT JOIN buddy_embedding e ON e.chunk_id = c.id " % current)
        if source_ids is not None:
            sql += "WHERE c.source_id IN %s "
            params = params + (tuple(source_ids) or (0,),)
        else:
            sql += "JOIN buddy_source s ON s.id = c.source_id AND s.active "  # what the indexer works on, as the screen says
        self.env.cr.execute(sql + "GROUP BY c.source_id", params)
        return {source: (done, total) for source, total, done in self.env.cr.fetchall()}

    @api.model
    def _state(self):
        """(code, when) of the last indexing pass ('' and '' when there was none)."""
        raw = str(self.env["ir.config_parameter"].sudo().get_param(STATE_PARAM) or "")
        code, _separator, when = raw.partition("|")
        return code, when

    @api.model
    def _purge_all(self):
        """Erase every vector (administrators, from the settings screen). The fragments themselves stay untouched."""
        self.env.cr.execute("DELETE FROM buddy_embedding")
        count = self.env.cr.rowcount
        _QUERIES.clear()
        self.env["ir.config_parameter"].sudo().set_param(STATE_PARAM, False)
        return count
