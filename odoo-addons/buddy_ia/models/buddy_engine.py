"""Authorized retrieval, bounded inference and short committed session transactions.

Odoo's request transaction uses REPEATABLE READ. Quota/session decisions use
independent READ COMMITTED cursors, with a per-user advisory transaction lock.
No lock survives a provider call. Deleting text never deletes the usage ledger.
"""
import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import SQL
from . import buddy_stream, buddy_vectors, prompts
from .buddy_privacy import QUESTIONS_PARAM, REASON_CODES, flag
from .buddy_provider import ProviderError, default_model
from .buddy_text import (  # noqa: F401 (text_terms is public API)
    CONTEXT_QUESTION_CHARS, MAX_CONTEXT_QUESTIONS, all_synonyms, build_vocabulary, clean_title, combine_groups,
    content_words, context_groups, correct_message, follow_up_kind, group_tsquery, has_topic, is_greeting, is_placeholder_email,
    query_groups, strip_control_chars, strip_greeting, text_terms, unknown_words)

SYNONYMS_PARAM = "buddy_ia.synonyms"
_logger = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 2000
# ts_rank_cd with normalization 32 gives rank / (rank + 1): a score in [0, 1) that does not depend on chunk length.
# The weights are {D, C, B, A}: a word in the body (B) weighs 0.1 and one in the document's context (A: title,
# sub-folders, folder) weighs 0.3, so one body match scores 0.1 / 1.1 = 0.09 as before and the default threshold keeps
# any real match. The administrator can raise it (parameter buddy_ia.text_min_score) when answers get noisy.
TEXT_MIN_SCORE = 0.05
TEXT_RANK_WEIGHTS = "{0.05, 0.05, 0.1, 0.3}"
# An OR query matches any chunk that shares one term. Chunks are ranked by how many of the question's terms (a term is
# a word or a whole synonym group) they contain, and only those with at least this share of the best chunk's matches
# are kept.
TEXT_MATCH_SHARE = 0.6
# The first search is "weak" when nothing matched or even the best chunk scores below this (one body word is 0.09, a word
# of the title 0.23). Only then the question is reformulated (typos fixed against the vocabulary of the titles) and
# searched again, at no cost with the provider; the second result is kept only when it is better than the first.
WEAK_SCORE = 0.12
VOCABULARY_DOCUMENTS = 1500
# The most frequent words of the documents' text a typo can be fixed to, kept per set of folders and refreshed when their
# chunks change (a small cache: a handful of entries, a few thousand words each).
CONTENT_VOCABULARY_WORDS = 3000
CONTENT_VOCABULARY_CACHE = 16
_CONTENT_VOCABULARIES = OrderedDict()
_CONTENT_VOCABULARIES_LOCK = threading.Lock()
DEFAULT_TOP_K = 6
# A message shorter than this is not worth a request to embed it («ok», «sí», «gracias»).
MIN_SEMANTIC_CHARS = 6
# No more than this many chunks of a single document among the best ones, so one repetitive document cannot push out
# every other one. Their neighbours are added afterwards and do not count.
MAX_CHUNKS_PER_DOCUMENT = 3
# Candidates fetched per chunk finally returned (the cap above and the relevance share discard some).
CANDIDATE_FACTOR = 4
# Total characters of document text sent to the model: the best chunks first, then their immediate neighbours.
CONTEXT_BUDGET = 7000
MAX_CHUNK_CHARS = 5000
LOCK_NAMESPACE = 1112884313
# Questions being answered at the same time across ALL users (each one holds a worker and a provider call for up to
# 35 seconds). Above this the newcomer is told to wait, without spending any of its quota.
DEFAULT_MAX_CONCURRENT = 3
MAX_CONCURRENT_LIMIT = 100
GLOBAL_LOCK_KEY = 0  # second part of the advisory lock that serializes the capacity check (user ids start at 1)
# Messages the widget gets back when it opens (the engine itself only uses the last 6 turns as model context).
HISTORY_LIMIT = 30
# Characters of previous conversation sent along with every question (each one is paid for in tokens).
HISTORY_CHARS = 6000
# Limits that protect the provider bill. Per person: DEFAULT_RATE_DAY a day; for the whole system: queries a day.
DEFAULT_RATE_DAY = 40
DEFAULT_DAILY_TOTAL = 1000
MAX_DAILY_TOTAL = 1_000_000
INSIGHT_QUESTION_CHARS = 500
# Feedback (👍/👎): the opaque code of an answer, and the limits of the voting route. A person can vote on this many
# different answers a minute, and change their vote on one answer this many times.
FEEDBACK_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
FEEDBACK_VOTES = ("up", "down", "none")
FEEDBACK_PER_MINUTE = 10
FEEDBACK_MAX_CHANGES = 6
# How long a question may hold its person's «in flight» slot and one of the company's: a little over the 35 s budget of
# one provider call. A request that never gets to run (the browser left before the stream started) frees itself in
# this time instead of blocking that person for a minute and a half.
LEASE_SECONDS = 45
# Model names listed by «Probar conexión» (a provider can offer dozens).
MODELS_SHOWN = 12


# https only, exactly docs.google.com or drive.google.com (no port, no user info), then a path with no whitespace,
# control characters or backslash. Browsers read a backslash as a slash, which is how an address like
# https://docs.google.com\@evil.example/ would otherwise end up on another host. `fullmatch` because "$" would also
# accept a trailing newline.
_DRIVE_LINK = re.compile(r"https://(?:docs|drive)\.google\.com/[^\x00-\x20\x7f\\]*")


def safe_drive_link(url):
    """Only Google's own https hosts reach the browser, whatever Drive returned."""
    return url if isinstance(url, str) and _DRIVE_LINK.fullmatch(url) else ""


class BuddyBusy(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class BuddyEngine(models.AbstractModel):
    _name = "buddy.engine"
    _description = "Motor del Buddy"

    @api.model
    def _require_internal(self):
        if not self.env.user.active or not self.env.user.has_group("base.group_user"):
            raise AccessError(_("Buddy está disponible para usuarios internos activos."))

    @api.model
    def _cfg(self):
        parameters = self.env["ir.config_parameter"].sudo()

        def get(key, default):
            value = parameters.get_param("buddy_ia." + key, str(default))
            return default if value in (None, False, "") else value

        try:
            model = str(get("chat_model", "")).strip()[:200]
            if not parameters.get_param("buddy_ia.api_key"):
                model = ""  # no key is the same as not configured
            elif not model:
                # Only an EMPTY setting gets the provider's default; what the administrator wrote is never replaced.
                model = default_model(parameters.get_param("buddy_ia.provider") or "anthropic")
            cfg = {"chat_model": model, "top_k": int(get("top_k", DEFAULT_TOP_K)),
                   "rate_min": int(get("rate_per_minute", 6)), "rate_day": int(get("rate_per_day", DEFAULT_RATE_DAY)),
                   "max_total": int(get("max_daily_total", DEFAULT_DAILY_TOTAL)),
                   "new_turns": int(get("new_user_turns", 12)), "max_concurrent": int(get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
                   "buddy_name": str(get("buddy_name", "Buddy"))[:80], "company": str(get("company", "la empresa"))[:160],
                   "timeout": 35}
            if not 1 <= cfg["top_k"] <= 10:
                raise ValueError("Parámetros de recuperación inválidos.")
            if not 1 <= cfg["rate_min"] <= 1000 or not 1 <= cfg["rate_day"] <= 100000 or not 0 <= cfg["new_turns"] <= 10000:
                raise ValueError("Parámetros de cuota inválidos.")
            if not 1 <= cfg["max_concurrent"] <= MAX_CONCURRENT_LIMIT:
                raise ValueError("Límite de consultas simultáneas inválido.")
            if not 1 <= cfg["max_total"] <= MAX_DAILY_TOTAL:
                raise ValueError("Tope diario del sistema inválido.")
            # Optional daily spending cap for the whole system (0 = no cap). It needs the token prices to mean anything.
            cfg["max_cost"] = float(get("max_daily_cost", 0))
            if not math.isfinite(cfg["max_cost"]) or not 0 <= cfg["max_cost"] <= 1_000_000:
                raise ValueError("Tope diario de costo inválido.")
            cfg["extra_instructions"] = str(get("extra_instructions", ""))[:1500]
            cfg["dialect"] = str(get("dialect", prompts.DEFAULT_DIALECT))
            if cfg["dialect"] not in prompts.DIALECTS:
                cfg["dialect"] = prompts.DEFAULT_DIALECT
            cfg["text_min_score"] = float(get("text_min_score", TEXT_MIN_SCORE))
            if not math.isfinite(cfg["text_min_score"]) or not 0 <= cfg["text_min_score"] < 1:
                raise ValueError("Puntaje mínimo de texto inválido.")
            # Built-in groups plus the company's; an invalid line of the setting is skipped, never an error here.
            cfg["synonyms"] = all_synonyms(parameters.get_param(SYNONYMS_PARAM) or "")
            # The semantic search: {model, dims, provider} when it is on and the provider can embed, else None. What the
            # search did (state and time) is left in cfg["semantic_info"] for the statistics row.
            cfg["semantic"] = self.env["buddy.embedding"]._semantic_cfg()
            cfg["semantic_info"] = {}
            return cfg
        except (TypeError, ValueError) as exc:
            raise BuddyBusy("not_configured") from exc

    @api.model
    def _configured(self):
        """Whether the AI side can answer at all (key + model): what the widget needs to tell a user the buddy is not
        set up yet. Reads two parameters; nothing about the configuration itself leaves the server."""
        parameters = self.env["ir.config_parameter"].sudo()
        if not parameters.get_param("buddy_ia.api_key"):
            return False
        model = str(parameters.get_param("buddy_ia.chat_model") or "").strip()
        return bool(model or default_model(parameters.get_param("buddy_ia.provider") or "anthropic"))

    @contextmanager
    def _fresh_engine(self, lock=False):
        try:
            with self.env.registry.cursor() as cursor:
                # First statement: never reuse the request's old snapshot.
                cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cursor.execute("SET LOCAL statement_timeout = '5000ms'")
                if lock:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, self.env.uid))
                env = api.Environment(cursor, self.env.uid, dict(self.env.context), su=False)
                engine = env["buddy.engine"]
                engine._require_internal()
                yield engine
                cursor.commit()
        except Exception as exc:
            # PostgreSQL query cancellation, lock timeout, deadlock or concurrent
            # serialization failure are transient; do not mask ACL or code errors.
            if getattr(exc, "pgcode", None) in {"57014", "55P03", "40P01", "40001"}:
                raise BuddyBusy("busy") from exc
            raise

    @api.model
    def _authorization_hash(self):
        # The fingerprint covers PERMISSIONS only: who the user is (groups, companies, active/internal state), which
        # folders those permissions open (ids and their groups) and the rules/ACLs of the knowledge models. It
        # deliberately ignores documents and fragments: a Drive synchronization adds, edits and removes them every
        # day and must not erase everybody's conversations. The accepted cost is documented in docs/SECURITY.md.
        # Only metadata is hashed, never content.
        user = self.env.user
        denied_reads = []
        try:
            # ORM read rules apply here, including domains whose visibility depends on fields that are not
            # themselves part of the hash. The write_date of a folder is NOT used: every synchronization moves it.
            sources = self.env["buddy.source"].search_read([("active", "=", True)], ["group_ids"], order="id")
        except AccessError:
            # A revoked model/field read right is a changed authorization state, not permission to keep replaying
            # the previous cache.
            denied_reads.append("buddy.source")
            sources = []
        # Whether the user may read documents and fragments at all (no data is read).
        can_read = {model: bool(self.env[model].has_access("read")) for model in ("buddy.document", "buddy.chunk")}
        protected_models = ["buddy.source", "buddy.document", "buddy.chunk"]
        rules = self.env["ir.rule"].sudo().search_read(
            [("model_id.model", "in", protected_models)], ["write_date", "active", "domain_force", "groups"], order="id")
        accesses = self.env["ir.model.access"].sudo().search_read(
            [("model_id.model", "in", protected_models)],
            ["write_date", "active", "model_id", "group_id", "perm_read", "perm_write", "perm_create", "perm_unlink"], order="id")
        payload = {"groups": sorted(user.groups_id.ids), "companies": sorted(user.company_ids.ids),
                   "allowed_companies": sorted(self.env.companies.ids), "current_company": self.env.company.id,
                   "user_active": bool(user.active), "user_share": bool(user.share),
                   "sources": sources, "can_read": can_read,
                   "rules": rules, "accesses": accesses, "denied_reads": sorted(denied_reads)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    @api.model
    def _invalidate_session(self, session, authorization_hash):
        if session.inflight_id:
            session.inflight_id.write({"state": "failed", "error_code": "session_changed"})
        self.env["buddy.message"].sudo().search([("user_id", "=", self.env.uid)]).unlink()
        session.write({"generation": session.generation + 1, "authorization_hash": authorization_hash,
                       "inflight_id": False, "inflight_until": False})

    @api.model
    def _session(self):
        session = self.env["buddy.session"].sudo().search([("user_id", "=", self.env.uid)], limit=1)
        fingerprint = self._authorization_hash()
        if not session:
            # Drop legacy unprovenanced history during the upgrade.
            self.env["buddy.message"].sudo().search([("user_id", "=", self.env.uid)]).unlink()
            session = self.env["buddy.session"].sudo().create({"user_id": self.env.uid, "authorization_hash": fingerprint})
        elif session.authorization_hash != fingerprint:
            self._invalidate_session(session, fingerprint)
        return session

    @api.model
    def _session_generation(self):
        self._require_internal()
        with self._fresh_engine(lock=True) as engine:
            generation = engine._session().generation
        return generation

    # The public methods below are the engine's door for the controller (plain Python calls) and nothing else:
    # `@api.private` takes them out of the generic RPC (/web/dataset/call_kw), where any employee could otherwise call
    # them directly (and, before 18.0.4.7.0, `stream_prepare` handed back the provider's API key and the prompt).
    @api.model
    @api.private
    def forget(self):
        self._require_internal()
        with self._fresh_engine(lock=True) as engine:
            session = engine._session()
            engine._invalidate_session(session, session.authorization_hash)
            generation = session.generation
        return generation

    @api.model
    @api.private
    def history(self, limit=HISTORY_LIMIT):
        """The last messages of the user's CURRENT conversation, oldest first, ready for the widget.

        buddy.message is private (no ACL for anyone): like the rest of the engine this reads it with sudo, and the
        explicit user_id filter below is the access control. Only the validated fields of each answer leave the
        server (never request ids, generations or raw content); the browser receives data, not internal state.
        """
        self._require_internal()
        limit = max(1, min(int(limit), HISTORY_LIMIT))
        with self._fresh_engine(lock=True) as engine:
            # _session() also discards a conversation whose permissions changed: nothing stale is ever returned.
            session = engine._session()
            rows = engine.env["buddy.message"].sudo().search(
                [("user_id", "=", engine.env.uid), ("generation", "=", session.generation)], order="id desc", limit=limit)
            tickets = engine._tickets_by_key(rows)
            messages = [engine._public_message(row, tickets) for row in reversed(rows)]
            generation = session.generation
        return {"messages": messages, "session_generation": generation}

    @api.model
    @api.private
    def feedback(self, token, vote, reason=None, include_question=False):
        """Record (or change, or take back) the 👍 / 👎 of one answer. Returns {"vote": "up" | "down" | None}.

        Who may vote: the owner of the answer and nobody else. Every answer that can be rated carries a random opaque
        `feedback_token`, stored on the PRIVATE message (`buddy.message`, which has an owner); the token and the user are
        both part of the search, so a token that is not yours finds nothing («feedback_unknown»), and guessing one is not
        feasible (18 random bytes). One vote per answer: voting again replaces the previous one.

        What is written: an anonymous row of `buddy.feedback` (see its docstring). The row gets neither the token nor the
        message; the message gets a pointer to the row (that is how a changed vote finds it) and the vote itself (that is
        how a reload shows it). The text of the question is read here, from the person's own message, and stored only when
        `include_question` is true (or the administrator turned on «Guardar preguntas») AND the vote is a 👎.

        Limits: FEEDBACK_PER_MINUTE answers per person per minute and FEEDBACK_MAX_CHANGES changes per answer.
        Everything is validated against closed lists before touching the database.
        """
        self._require_internal()
        if (not isinstance(token, str) or not FEEDBACK_TOKEN.fullmatch(token) or vote not in FEEDBACK_VOTES
                or (reason is not None and reason not in REASON_CODES) or type(include_question) is not bool):
            raise BuddyBusy("invalid_request")
        # A code that is not this person's costs one indexed query: no lock, no session, no permission hash (a flood of
        # made-up codes must not hold the person's lock, nor a worker).
        with self._fresh_engine() as engine:
            known = engine.env["buddy.message"].sudo().search_count([
                ("user_id", "=", engine.env.uid), ("role", "=", "assistant"), ("feedback_token", "=", token)])
        if not known:
            raise BuddyBusy("feedback_unknown")
        with self._fresh_engine(lock=True) as engine:
            session = engine._session()  # also discards the conversation of a person whose permissions changed
            messages = engine.env["buddy.message"].sudo()
            row = messages.search([("user_id", "=", engine.env.uid), ("generation", "=", session.generation),
                                   ("role", "=", "assistant"), ("feedback_token", "=", token)], limit=1)
            if not row:
                raise BuddyBusy("feedback_unknown")
            now = fields.Datetime.now()
            recent = messages.search_count([("user_id", "=", engine.env.uid), ("feedback_at", ">=", now - timedelta(seconds=60)),
                                            ("id", "!=", row.id)])
            if recent >= FEEDBACK_PER_MINUTE or row.feedback_changes >= FEEDBACK_MAX_CHANGES:
                raise BuddyBusy("rate_limited")
            feedback = engine.env["buddy.feedback"]
            changes = {"feedback_changes": row.feedback_changes + 1, "feedback_at": now}
            if vote == "none":
                row.feedback_id.sudo().unlink()
                row.write(dict(changes, feedback_vote=False, feedback_id=False))
                return {"vote": None}
            context = row.feedback_context if isinstance(row.feedback_context, dict) else {}
            # Only a 👎 can bring its question (the 👍 has no form, and it is the 👎 that goes to the golden set).
            keep_question = vote == "down" and (include_question or flag(engine.env["ir.config_parameter"].sudo().get_param(QUESTIONS_PARAM)))
            question = ""
            if keep_question and row.request_id:
                asked = messages.search([("user_id", "=", engine.env.uid), ("request_id", "=", row.request_id),
                                         ("role", "=", "user")], limit=1)
                question = asked.content or ""
            record = feedback._store(row.feedback_id.sudo(), {
                "value": vote, "reason": reason, "keep_question": keep_question, "question": question,
                "model": context.get("model"), "document_ids": context.get("docs") or []})
            row.write(dict(changes, feedback_vote=vote, feedback_id=record.id))
        return {"vote": vote}

    @api.model
    def _tickets_by_key(self, rows):
        """The tickets this user already created from these answers, by the chat request id that doubles as the ticket's
        idempotency key: after a reload the card says «Ticket creado» instead of offering to create it again.
        Read with the user's own environment (its record rule: only their tickets) and an explicit filter."""
        keys = [row.request_id for row in rows if row.role == "assistant" and row.request_id]
        if not keys:
            return {}
        found = self.env["buddy.ticket"].search([("user_id", "=", self.env.uid), ("request_key", "in", keys)])
        return {ticket.request_key: {"id": ticket.id, "name": ticket.name} for ticket in found}

    @api.model
    def _public_message(self, row, tickets=None):
        """One stored message as the widget needs it. Re-validated on the way out: stored JSON is not trusted blindly."""
        created = fields.Datetime.to_string(row.create_date) if row.create_date else ""
        if row.role == "user":
            return {"role": "user", "text": row.content, "date": created}
        data = row.response_json if isinstance(row.response_json, dict) else {}

        def text(value, limit):
            return value[:limit] if isinstance(value, str) else ""

        sources = []
        for source in data.get("sources") if isinstance(data.get("sources"), list) else []:
            if isinstance(source, dict):
                sources.append({"title": text(source.get("title"), 300), "owner": text(source.get("owner"), 300),
                                "updated": text(source.get("updated"), 10), "link": safe_drive_link(text(source.get("link"), 2000))})
        ticket = data.get("ticket")
        ticket = {"title": text(ticket.get("title"), 120), "description": text(ticket.get("description"), 2000)} \
            if isinstance(ticket, dict) and text(ticket.get("title"), 120) else None
        contact = data.get("contact")
        contact = {key: text(contact.get(key), 200) for key in ("role", "label", "name", "contact")} \
            if isinstance(contact, dict) else None
        token = row.feedback_token if isinstance(row.feedback_token, str) and FEEDBACK_TOKEN.fullmatch(row.feedback_token) else None
        return {"role": "assistant", "text": row.content, "date": created,
                "expression": data.get("expression") if data.get("expression") in prompts.EXPRESSIONS else "neutral",
                "ring": "doubt" if data.get("ring") == "doubt" else "ok",
                "sources": sources, "ticket": ticket, "contact": contact,
                "confidence": data.get("confidence") if data.get("confidence") in prompts.CONFIDENCE_LEVELS else None,
                "conflict": data.get("conflict") is True,
                # The code that lets the widget vote on this answer, and the vote already given (so a reload shows it).
                "feedback_token": token, "feedback": row.feedback_vote if token and row.feedback_vote else None,
                # The user's OWN chat request id doubles as the idempotency key of the ticket this answer proposes:
                # after a reload, "Crear ticket" recovers the ticket already created instead of making a second one.
                "ticket_key": row.request_id if ticket and row.request_id else None,
                "ticket_created": (tickets or {}).get(row.request_id) if ticket else None}

    @api.model
    def _delete_all_conversations(self):
        """Administrator action: erase every user's conversation and fence every in-flight answer.

        Deleting text never touches the usage ledger (quotas stay). Each session moves to a new generation, so a
        provider answer that arrives later is discarded and every open widget is told its conversation changed.
        """
        self._require_internal()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Solo un administrador puede borrar las conversaciones."))
        messages = self.env["buddy.message"].sudo()
        count = messages.search_count([])
        _logger.warning("Buddy: el usuario con uid %s borró todo el historial de conversaciones (%s mensajes)", self.env.uid, count)
        messages.search([]).unlink()
        for session in self.env["buddy.session"].sudo().search([]):
            if session.inflight_id:
                session.inflight_id.write({"state": "failed", "error_code": "session_changed"})
            session.write({"generation": session.generation + 1, "inflight_id": False, "inflight_until": False})
        return count

    @api.model
    def _reserve_usage(self, cfg, generation, request_id):
        ledger = self.env["buddy.usage"].sudo()
        previous = ledger.search([("user_id", "=", self.env.uid), ("request_id", "=", request_id)], limit=1)
        if previous:
            if previous.generation != generation:
                return previous, "session_changed"
            return previous, "duplicate"
        now = fields.Datetime.now()
        scope = cfg.get("scope", "chat")
        domain = [("user_id", "=", self.env.uid), ("scope", "=", scope)]
        # The daily limit is looked at first: when it is the one reached, waiting a minute does not help, and the
        # person must be told so (a different message, and no "retry" button in the widget).
        for key, interval, code in (("rate_day", timedelta(days=1), "daily_limit"),
                                    ("rate_min", timedelta(minutes=1), "rate_limited")):
            if cfg[key] and ledger.search_count(domain + [("create_date", ">=", now - interval)]) >= cfg[key]:
                return ledger, code
        if scope == "chat" and self._system_limit_reached(cfg, now):
            return ledger, "system_limit"
        return ledger.create({"user_id": self.env.uid, "request_id": request_id, "generation": generation, "scope": scope}), None

    @api.model
    def _system_limit_reached(self, cfg, now):
        """The whole company's daily ceiling (queries and, when prices are set, estimated cost) over the last 24 hours.

        It stops a runaway bill (a script, a loop, a very large company on a small plan) whatever each person's own
        limit is. Called under the global advisory lock taken by `_over_capacity`, so two requests cannot both take
        the last slot.
        """
        since = now - timedelta(days=1)
        if cfg.get("max_total") and self.env["buddy.usage"].sudo().search_count(
                [("scope", "=", "chat"), ("create_date", ">=", since)]) >= cfg["max_total"]:
            return True
        return bool(cfg.get("max_cost")) and self.env["buddy.insight"].sudo()._spent_since(since) >= cfg["max_cost"]

    @api.model
    def _over_capacity(self, cfg):
        """Whether `max_concurrent` questions (from anyone) are being answered right now.

        A question counts from the moment its quota is reserved until its answer is stored or released (or its lease runs out). The check and the reservation that follows it run under one global advisory lock, so two
        users arriving together cannot both take the last slot. Called from `_start_request`, which already holds the
        user's own lock: the order is always user lock, then this one, so they cannot deadlock.
        """
        limit = cfg.get("max_concurrent") or 0
        if not limit:
            return False
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, GLOBAL_LOCK_KEY))
        running = self.env["buddy.session"].sudo().search_count([
            ("inflight_id", "!=", False), ("inflight_until", ">", fields.Datetime.now())])
        return running >= limit

    @api.model
    def _start_request(self, message, cfg, expected_generation, request_id):
        result = {}
        with self._fresh_engine(lock=True) as engine:
            session = engine._session()
            if expected_generation is not None and session.generation != expected_generation:
                result["error"] = "session_changed"
            else:
                previous = engine.env["buddy.usage"].sudo().search([
                    ("user_id", "=", engine.env.uid), ("request_id", "=", request_id)], limit=1)
                if previous:
                    cached = engine.env["buddy.message"].sudo().search([
                        ("user_id", "=", engine.env.uid), ("request_id", "=", request_id),
                        ("generation", "=", session.generation), ("role", "=", "assistant")], limit=1)
                    if previous.generation != session.generation:
                        result["error"] = "session_changed"
                    elif previous.state == "done" and cached.response_json:
                        result["cached"] = dict(cached.response_json)
                    else:
                        result["error"] = previous.error_code or "request_pending"
                elif session.inflight_id and session.inflight_until and session.inflight_until > fields.Datetime.now():
                    result["error"] = "busy"
                else:
                    if session.inflight_id:
                        # Expired lease: fence a late answer before accepting work.
                        engine._invalidate_session(session, session.authorization_hash)
                        result["error"] = "session_changed"
                    elif engine._over_capacity(cfg):
                        result["error"] = "busy"  # nothing was reserved: this attempt costs no quota
                    else:
                        usage, error = engine._reserve_usage(cfg, session.generation, request_id)
                        if error:
                            result["error"] = error
                        else:
                            result = {"usage_id": usage.id, "generation": session.generation,
                                      "authorization_hash": session.authorization_hash, "history": engine._history(cfg),
                                      "request_id": request_id, "is_new": engine._is_new(cfg),
                                      "context": engine._conversation_context(session.generation)}
                            engine._remember("user", message, session.generation, request_id)
                            session.write({"inflight_id": usage.id, "inflight_until": fields.Datetime.now() + timedelta(seconds=LEASE_SECONDS)})
        if result.get("error"):
            raise BuddyBusy(result["error"])
        return result

    @api.model
    def _request_live(self, token, response=None, error=None, insight=None):
        """Check that the request is still the live one; with `response` or `error` it is also closed.

        `insight` (a dict, see `_insight_data`) is the statistics row of a finished query: it is written in the same
        short transaction that closes the reservation, whatever the outcome (an answer, a failure, an abandoned stream).
        """
        valid = False
        with self._fresh_engine(lock=True) as engine:
            session = engine._session()
            usage = engine.env["buddy.usage"].sudo().browse(token["usage_id"]).exists()
            valid = bool(usage and session.generation == token["generation"]
                         and session.authorization_hash == token["authorization_hash"]
                         and session.inflight_id.id == usage.id and usage.state == "reserved")
            if response is not None or error is not None:
                if valid and response is not None:
                    engine._remember("assistant", response["answer"], session.generation, token["request_id"], response, insight)
                    usage.write({"state": "done"})
                elif usage and usage.state == "reserved":
                    usage.write({"state": "failed", "error_code": error or "session_changed"})
                if error is not None:
                    engine.env["buddy.message"].sudo().search([
                        ("user_id", "=", engine.env.uid), ("request_id", "=", token["request_id"]) ]).unlink()
                if session.inflight_id.id == token["usage_id"]:
                    session.write({"inflight_id": False, "inflight_until": False})
                if insight is not None:
                    engine._store_insight(insight)
        if not valid and error is None:
            raise BuddyBusy("session_changed")
        return valid

    @api.model
    def _store_insight(self, data):
        """Write one statistics row. Never raises: a failure here must not turn a good answer into an error."""
        try:
            with self.env.cr.savepoint():
                self.env["buddy.insight"].sudo()._record(data)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Buddy: no pude guardar la estadística de la consulta (%s)", type(exc).__name__)

    @api.model
    def _insight_data(self, message, model, chunks, started, usage, result=None, error=None, semantic=None):
        """What the statistics row keeps about a query (plain data; `buddy.insight._record` decides what is stored).

        The text of the question travels here only so the model can apply the privacy rule in ONE place (kept when the
        buddy could not answer, or when the administrator turned on «Guardar preguntas»); nobody's name is stored.
        """
        chunks = chunks or []
        retrieved = [chunk for chunk in chunks if not chunk.get("neighbor")]
        used = [] if not result or result.get("needs_human") else self._used_chunks(chunks, result.get("sources_used"))
        documents = {}
        for chunk in used:
            if chunk.get("doc_id"):
                documents.setdefault(chunk["doc_id"], True)
        return {"question": message[:INSIGHT_QUESTION_CHARS], "needs_human": bool(result and result.get("needs_human")),
                "retrieved_count": len(retrieved), "best_score": max((float(chunk.get("score") or 0) for chunk in retrieved), default=0.0),
                "document_ids": list(documents), "model": model,
                "tokens_in": (usage or {}).get("in") or 0, "tokens_out": (usage or {}).get("out") or 0,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)), "error": error or False,
                "semantic_state": (semantic or {}).get("state") or False, "semantic_ms": int((semantic or {}).get("ms") or 0),
                "confidence": (result or {}).get("confidence") or False, "conflict": bool((result or {}).get("conflict"))}

    @api.model
    def _create_ticket(self, title, description, request_key, session_generation=None):
        self._require_internal()
        result, error = None, None
        with self._fresh_engine(lock=True) as engine:
            session = engine._session()
            if session_generation is not None and session.generation != session_generation:
                error = "session_changed"
            else:
                ticket = engine.env["buddy.ticket"].create({"name": title, "description": description, "request_key": request_key})
                result = {"id": ticket.id, "name": ticket.name, "session_generation": session.generation}
        if error:
            raise BuddyBusy(error)
        return result

    @api.model
    def _chat_args(self, message, session_generation, request_id):
        """Shared validation of `chat` and `chat_stream`: returns (clean message, request id)."""
        self._require_internal()
        if isinstance(message, str):
            message = strip_control_chars(message)  # NUL would be a database error, not a refusal
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            raise BuddyBusy("invalid_message")
        if session_generation is not None and (type(session_generation) is not int or session_generation < 1):
            raise BuddyBusy("session_changed")
        if request_id is None:
            request_id = str(uuid.uuid4())
        try:
            request_id = str(uuid.UUID(request_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise BuddyBusy("invalid_message") from exc
        return message.strip(), request_id

    @api.model
    @api.private
    def chat(self, message, session_generation=None, request_id=None):
        message, request_id = self._chat_args(message, session_generation, request_id)
        # Data used to assemble context also lives in a fresh READ COMMITTED env.
        with self._fresh_engine() as engine:
            return engine._chat_flow(message, session_generation, request_id)

    # ------------------------------------------------------------------------------------------ streaming
    @api.model
    @api.private
    def chat_stream(self, message, session_generation=None, request_id=None):
        """Same semantics as `chat` (quota, idempotency, session generation, history, permissions) but streamed.

        The PREPARATION (validation, quota reservation, retrieval with the user's own environment, prompt and
        catalog) happens here, right away, before anything is emitted: errors in that phase are raised as BuddyBusy
        exactly like `chat`. What comes back is a generator of `(event, payload)` pairs:
        ("delta", {"text"}) new answer text, ("done", response) the complete validated answer (identical to what
        `chat` returns, already persisted) or ("error", {"error": code, ...}). Closing the generator early (the
        browser went away) releases the reservation and stores nothing.
        """
        return self.stream_events(self.stream_prepare(message, session_generation, request_id))

    @api.model
    @api.private
    def stream_prepare(self, message, session_generation=None, request_id=None):
        """The plan of a streamed query: plain data only (no cursor, no records) that `stream_events` consumes.

        The plan never carries the provider's API key: `stream_events` reads the settings itself, with a fresh cursor."""
        message, request_id = self._chat_args(message, session_generation, request_id)
        with self._fresh_engine() as engine:
            return engine._stream_plan(message, session_generation, request_id)

    @api.model
    def _stream_plan(self, message, expected_generation, request_id):
        cfg = self._cfg()
        if not cfg["chat_model"]:
            raise BuddyBusy("not_configured")
        cfg["deadline"] = time.monotonic() + cfg["timeout"]
        token = self._start_request(message, cfg, expected_generation, request_id)
        if "cached" in token:
            return {"cached": token["cached"]}
        started, chunks = time.monotonic(), []
        try:
            chunks = self._retrieve(message, cfg, token.get("context"))
            catalog = self._catalog()
            self._request_live(token)
            profile = {"name": self.env.user.name, "is_new": token["is_new"], "stage": self.env["buddy.journey.engine"]._stage_note()}
            messages = self._build_messages(message, token["history"], profile, chunks, cfg, catalog)
            self.env["buddy.provider"]._settings()  # only to refuse an unknown provider now; the key is read later
        except BuddyBusy as exc:
            self._request_live(token, error=exc.code, insight=self._insight_data(
                message, cfg["chat_model"], chunks, started, {}, None, exc.code, cfg.get("semantic_info")))
            raise
        except Exception as exc:
            self._request_live(token, error="provider_error", insight=self._insight_data(
                message, cfg["chat_model"], chunks, started, {}, None, "provider_error", cfg.get("semantic_info")))
            _logger.warning("Buddy: consulta fallida (%s)", type(exc).__name__)
            raise BuddyBusy("provider_error") from exc
        return {"token": token, "model": cfg["chat_model"], "messages": messages,
                "chunks": chunks, "message": message, "deadline": cfg["deadline"], "timeout": cfg["timeout"],
                "started": started, "semantic": cfg.get("semantic_info")}

    @api.model
    def _fresh_settings(self):
        """The provider's settings (including the API key) read through a new cursor: the one of the web request is
        closed by the time a stream runs, and the key must not travel inside the plan."""
        with self._fresh_engine() as engine:
            return engine.env["buddy.provider"]._settings()

    @api.model
    def _failure_code(self, exc):
        if isinstance(exc, BuddyBusy):
            return exc.code
        if isinstance(exc, (ValueError, TypeError)):
            return "invalid_response"
        if isinstance(exc, TimeoutError):
            return "provider_timeout"
        _logger.warning("Buddy: proveedor falló (%s)", getattr(exc, "code", type(exc).__name__))
        return self._provider_code(exc)

    @api.model
    def _release_stream(self, token, code, insight=None):
        """Free a reservation whose answer will never be stored (never raises: it runs while unwinding)."""
        try:
            self._request_live(token, error=code, insight=insight)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Buddy: no pude liberar la consulta (%s)", type(exc).__name__)

    @api.model
    def _release_unstarted(self, plan):
        """Free the reservation of a plan whose stream never ran (the browser left before the first byte was read)."""
        token = plan.get("token") if isinstance(plan, dict) else None
        if not token:
            return  # an answer served from the cache reserved nothing
        data = self._insight_data(plan["message"], plan["model"], plan["chunks"], plan.get("started") or time.monotonic(),
                                  {}, None, "client_disconnected", plan.get("semantic"))
        self._release_stream(token, "client_disconnected", data)

    @api.model
    def _stream_error(self, code):
        payload = {"error": code}
        if code == "session_changed":
            try:
                # Not self._session_generation(): that one reads self.env.user, whose cursor is closed by now.
                with self._fresh_engine(lock=True) as engine:
                    payload["session_generation"] = engine._session().generation
            except Exception:  # noqa: BLE001 - the widget refreshes its config when no generation comes
                pass
        return payload

    @api.model
    @api.private
    def stream_events(self, plan):
        """Run a prepared plan. Does NOT use the environment of the web request (its cursor is closed once the route
        returned): only the provider, with the settings read at the start of the stream, and fresh cursors for the
        database."""
        if "cached" in plan:
            yield "done", plan["cached"]
            return
        token = plan["token"]
        cfg = {"deadline": plan["deadline"], "timeout": plan["timeout"]}
        extractor, parts, size, persisted, source = buddy_stream.AnswerExtractor(), [], 0, False, None
        started, usage, result, sent = plan.get("started") or time.monotonic(), {}, None, False

        def failure(code):
            """The statistics row of a query that ends without a stored answer (None when it was already sent)."""
            if sent:
                return None
            return self._insight_data(plan["message"], plan["model"], plan["chunks"], started, usage, result, code, plan.get("semantic"))

        # An opening greeting is held back (and removed from the final answer too) unless the person greeted first.
        gate = buddy_stream.GreetingGate(strip=not is_greeting(plan["message"]))
        try:
            source = self.env["buddy.provider"]._chat_stream(
                plan["model"], plan["messages"], self._remaining(cfg, cfg["timeout"]), settings=self._fresh_settings(), usage=usage)
            for fragment in source:
                size += len(fragment)
                if size > buddy_stream.MAX_STREAM_CHARS:
                    raise ValueError("Respuesta demasiado larga.")
                parts.append(fragment)
                text = gate.feed(extractor.feed(fragment))
                if text:
                    yield "delta", {"text": text}
            text = gate.finish()  # an answer shorter than the hold-back window is still waiting here
            if text:
                yield "delta", {"text": text}
            result = self._normalize(prompts.parse_model_json("".join(parts)), plan["message"])
            contact = None
            if result["needs_human"] or self._is_unsure(result):
                with self._fresh_engine() as engine:
                    contact = engine._contact_for(result["contact_role"])
            response = self._compose_response(result, plan["message"], plan["chunks"], token["generation"], contact)
            self._remaining(cfg)
            insight = self._insight_data(plan["message"], plan["model"], plan["chunks"], started, usage, result, None, plan.get("semantic"))
            sent = True
            self._request_live(token, response=response, insight=insight)
            persisted = True
            yield "done", response
        except GeneratorExit:
            if not persisted:
                self._release_stream(token, "client_disconnected", failure("client_disconnected"))
            raise
        except Exception as exc:  # noqa: BLE001
            code = self._failure_code(exc)
            if not persisted:
                self._release_stream(token, code, failure(code))
            yield "error", self._stream_error(code)
        finally:
            if source is not None:
                source.close()

    @staticmethod
    def _is_unsure(result):
        """The documents contradict each other or the model found only weak evidence: the answer says so, and the
        widget adds a discreet note and the contact to confirm with (no ticket: it did answer)."""
        return bool(result.get("conflict")) or result.get("confidence") == "low"

    @api.model
    def _compose_response(self, result, message, chunks, generation, contact):
        human = result["needs_human"]
        unsure = self._is_unsure(result)
        ticket = result["ticket"] or {"title": _("Consulta: %(text)s", text=message[:70]), "description": message}
        sources = [] if human else self._used_sources(chunks, result.get("sources_used"))
        return {"answer": result["answer"], "expression": result["expression"], "ring": "doubt" if human or unsure else "ok",
                "sources": sources,
                "ticket": ticket if human else None, "contact": contact if human or unsure else None,
                "confidence": result.get("confidence"), "conflict": bool(result.get("conflict")),
                # An answer about the documentation (or the honest «not documented») can be rated; small talk, greetings
                # and off-topic replies cannot: no code, no buttons. Random and opaque: see `feedback`.
                "feedback_token": secrets.token_urlsafe(18) if human or sources else None,
                "provider": "llm", "error": None, "session_generation": generation}

    @api.model
    def _chat_flow(self, message, expected_generation, request_id):
        cfg = self._cfg()
        if not cfg["chat_model"]:
            raise BuddyBusy("not_configured")
        cfg["deadline"] = time.monotonic() + cfg["timeout"]
        token = self._start_request(message, cfg, expected_generation, request_id)
        if "cached" in token:
            return token["cached"]
        # What the statistics row will say: filled in as the query advances, written once when it ends.
        started, usage, chunks, result, sent = time.monotonic(), {}, [], None, False
        cfg["usage"] = usage  # the provider call drops the token counts here (see _ask_model)

        def failure(code):
            data = None if sent else self._insight_data(message, cfg["chat_model"], chunks, started, usage, result, code, cfg.get("semantic_info"))
            return {"error": code, "insight": data}

        try:
            chunks = self._retrieve(message, cfg, token.get("context"))
            catalog = self._catalog()
            self._request_live(token)
            # Always one provider call, even without fragments: greetings and "what can you help me with?" are answered
            # from the catalog, and a concrete question with no information is declared undocumented by the model.
            profile = {"name": self.env.user.name, "is_new": token["is_new"], "stage": self.env["buddy.journey.engine"]._stage_note()}
            result = self._normalize(self._ask_model(message, token["history"], profile, chunks, cfg, catalog), message)
            contact = self._contact_for(result["contact_role"]) if result["needs_human"] or self._is_unsure(result) else None
            response = self._compose_response(result, message, chunks, token["generation"], contact)
            self._remaining(cfg)
            insight = self._insight_data(message, cfg["chat_model"], chunks, started, usage, result, None, cfg.get("semantic_info"))
            sent = True  # from here on the row travels with the closing call: a late failure must not write a second one
            self._request_live(token, response=response, insight=insight)
            return response
        except BuddyBusy as exc:
            self._request_live(token, **failure(exc.code))
            raise
        except Exception as exc:
            self._request_live(token, **failure("provider_error"))
            _logger.warning("Buddy: consulta fallida (%s)", type(exc).__name__)
            raise BuddyBusy("provider_error") from exc

    @api.model
    def _remaining(self, cfg, maximum=20):
        remaining = cfg.get("deadline", time.monotonic() + cfg["timeout"]) - time.monotonic()
        if remaining <= 0:
            raise BuddyBusy("provider_timeout")
        return min(maximum, remaining)

    @api.model
    def _retrieve(self, message, cfg, context=None):
        """PostgreSQL full-text search over the chunks this user may read (ORM rules, no sudo, no id lists).

        The question becomes groups of synonyms (see buddy_text.query_groups); a chunk matches a group when it has any
        of its words. The best chunks come first, then each one's neighbours while the context budget allows.
        `context` is what this same person said and was answered in the previous turn (`_conversation_context`): a
        question that leans on it («¿y quién los gestiona?») is searched again with those terms added.
        """
        groups = query_groups(message, cfg.get("synonyms"))
        sources = self.env["buddy.source"].search([("active", "=", True)])
        if not sources or not (groups or (context and context.get("questions"))):
            return []
        rows = self._search_rows(groups, sources, cfg) if groups else []
        # «Fully matched»: the best chunk has a word of EVERY group of the question, so nothing in it is a stray typo.
        fully_matched = bool(rows) and rows[0][4] >= len(groups)
        if groups and (self._weak(rows) or (not fully_matched and self._has_unknown_word(message, sources, cfg))):
            # Few or poor results, or a word that appears nowhere in the documents ("bakups" next to a word that does
            # match): maybe a typo ("bakups", "tiket"). Try once more with the words fixed against the titles and the
            # text of the documents and keep whichever search was better. Nothing here calls the provider.
            wider = self._widened_groups(message, groups, sources, cfg)
            if wider:
                more = self._search_rows(wider, sources, cfg)
                if self._quality(more, wider) > self._quality(rows, groups):
                    rows = more
        rows = self._conversation_rows(message, groups, rows, sources, cfg, context)
        rows = self._hybrid_rows(message, groups, rows, sources, cfg, context)
        best, per_document = [], {}
        for row in rows:
            if per_document.get(row[1], 0) >= MAX_CHUNKS_PER_DOCUMENT:
                continue
            per_document[row[1]] = per_document.get(row[1], 0) + 1
            best.append(row)
            if len(best) >= cfg["top_k"]:
                break
        return self._assemble_context(best)

    @api.model
    def _hybrid_rows(self, message, groups, rows, sources, cfg, context):
        """`rows` (the text search) fused with the search by meaning, or `rows` untouched when the latter is off, not worth
        it for this message or finds nothing that stands out.

        The meaning of the question is looked for among the fragments THIS user may read (the same record rules as the
        text search, applied before anything is scored). One small request to the provider (cached by question, bounded
        in time, skipped for greetings and small talk and after repeated failures); if it fails the text search stands.
        """
        semantic = cfg.get("semantic")
        if not semantic or not self._worth_embedding(message, context):
            return rows
        found, info = self.env["buddy.embedding"]._semantic_rows(
            self._semantic_text(message, context), sources, min(cfg["top_k"] * CANDIDATE_FACTOR, 60), semantic, cfg.get("deadline"))
        cfg["semantic_info"] = info
        return self._fuse_rows(rows, found) if found else rows

    @staticmethod
    def _worth_embedding(message, context):
        """Greetings, thanks, «what can you help me with?» and tiny messages say nothing a search could find: no request
        for them. A follow-up that names nothing («¿y eso?») is embedded together with the previous question."""
        if is_greeting(message) or len(message.strip()) < MIN_SEMANTIC_CHARS:
            return False
        return has_topic(message) or bool(context and context.get("questions") and follow_up_kind(message) == "dependent")

    @staticmethod
    def _semantic_text(message, context):
        """What is embedded: the message, and for a «dependent» follow-up («¿y quién los gestiona?») the person's previous
        question before it, so it means something on its own (the same borrowing the text search does)."""
        if context and context.get("questions") and follow_up_kind(message) == "dependent":
            return (context["questions"][0] + "\n" + message)[:buddy_vectors.QUERY_TEXT_CHARS]
        return message

    @staticmethod
    def _fuse_rows(rows, semantic):
        """Reciprocal Rank Fusion of the text ranking (`rows`, best first) and the semantic one (`semantic`: similarity,
        chunk, document, sequence). Rows keep the text search's own data; a fragment only the semantic search found gets
        its similarity as score and no matched terms."""
        merged = {row[0]: row for row in rows}
        for similarity, chunk_id, document_id, sequence in semantic:
            merged.setdefault(chunk_id, (chunk_id, document_id, sequence, max(0.0, min(0.99, float(similarity))), 0))
        order = buddy_vectors.rrf([[row[0] for row in rows], [item[1] for item in semantic]])
        return [merged[chunk_id] for chunk_id, _score in order]

    @api.model
    def _conversation_context(self, generation):
        """What the search may borrow from this person's previous turn: its question(s) and the titles of the documents
        its answer cited, as plain data.

        Only the messages of THIS person (the ones `_history` already sends to the model) and of the current
        conversation (generation) are read, so another person's text can never lend a term; an answer whose
        documents are no longer readable would have dropped the conversation with the authorization fingerprint. The
        result is used only as search terms (letters and digits, through `query_groups`): it never reaches the prompt
        and it is never stored.
        """
        rows = self.env["buddy.message"].sudo().search(
            [("user_id", "=", self.env.uid), ("generation", "=", generation)], order="id desc", limit=2 * MAX_CONTEXT_QUESTIONS)
        questions, titles, last_answer = [], [], True
        for row in rows:  # newest first: the answer of the previous turn, then its question, and so on
            if row.role == "assistant" and last_answer:
                sources = (row.response_json or {}).get("sources") if isinstance(row.response_json, dict) else None
                titles = [source["title"] for source in sources[:3] if isinstance(source, dict) and isinstance(source.get("title"), str)] \
                    if isinstance(sources, list) else []
            elif row.role == "user" and len(questions) < MAX_CONTEXT_QUESTIONS:
                questions.append((row.content or "")[:CONTEXT_QUESTION_CHARS])
            last_answer = False
        return {"questions": questions, "titles": titles} if questions else None

    @api.model
    def _conversation_rows(self, message, groups, rows, sources, cfg, context):
        """`rows` improved with the previous turn when the question needs it, else `rows` untouched.

        «Dependent» questions («¿y quién los gestiona?», «¿y eso?») are always searched again with the terms of the
        previous question and of the documents it cited, the question's own terms weighing more than all of those
        together. A merely short question is only worth it when its own search was weak, and then only if the question's
        own words matched. An explicit change of subject, a greeting or a self-contained question never looks back.
        Same permissions as any search (the user's record rules); nothing here calls the provider.
        """
        kind = follow_up_kind(message) if context and context.get("questions") else None
        if kind is None or (kind == "short" and not self._weak(rows)):
            return rows
        questions = list(context["questions"])
        if len(questions) > 1 and follow_up_kind(questions[0]) is None:
            questions = questions[:1]  # the previous question stands on its own: no need to go further back
        combined, weights = combine_groups(groups, context_groups(questions, context.get("titles"), cfg.get("synonyms")),
                                           dominant=kind == "short")
        if len(combined) == len(groups):
            return rows
        more = self._search_rows(combined, sources, cfg, weights)
        if not more:
            return rows
        if kind == "dependent":
            return more
        return more if more[0][4] >= weights[0] and not self._weak(more) else rows

    @staticmethod
    def _weak(rows):
        return not rows or max(row[3] for row in rows) < WEAK_SCORE

    @staticmethod
    def _quality(rows, groups):
        """(share of the question's terms the best chunk has, best score): how good a search result is."""
        if not rows:
            return (0.0, 0.0)
        return (rows[0][4] / max(1, len(groups)), max(row[3] for row in rows))

    @api.model
    def _content_vocabulary(self, sources):
        """The most frequent words (letters only, 4 or more) of the text of the chunks of these folders.

        Read through the user's own record rules (the same visible chunks as the search), cached per set of folders and
        refreshed when their chunks change (count and newest id), and never fatal: without it typos are fixed against
        titles, folders and synonyms only, as before.
        """
        ids = tuple(sorted(sources.ids))
        if not ids:
            return frozenset()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute("SELECT count(*), coalesce(max(id), 0) FROM buddy_chunk WHERE source_id IN %s", (ids,))
                count, newest = self.env.cr.fetchone()
                key = (self.env.cr.dbname, ids, count, newest)
                with _CONTENT_VOCABULARIES_LOCK:
                    known = _CONTENT_VOCABULARIES.get(key)
                    if known is not None:
                        _CONTENT_VOCABULARIES.move_to_end(key)
                        return known
                visible = self.env["buddy.chunk"]._search([("source_id", "in", list(ids))])
                self.env.cr.execute(SQL(
                    "SELECT w FROM (SELECT lower(w) AS w, count(*) AS n FROM buddy_chunk c, "
                    "regexp_split_to_table(c.content, '[^[:alpha:]]+') AS w WHERE c.id IN %s AND length(w) >= 4 "
                    "GROUP BY 1 ORDER BY n DESC, 1 LIMIT %s) t", visible.subselect(), CONTENT_VOCABULARY_WORDS * 2))
                words = content_words((row[0] for row in self.env.cr.fetchall()), CONTENT_VOCABULARY_WORDS)
        except Exception as exc:  # noqa: BLE001 - a slow or failing query must not break the question
            _logger.warning("Buddy: no pude leer el vocabulario de los documentos (%s)", type(exc).__name__)
            return frozenset()
        with _CONTENT_VOCABULARIES_LOCK:
            _CONTENT_VOCABULARIES[key] = words
            while len(_CONTENT_VOCABULARIES) > CONTENT_VOCABULARY_CACHE:
                _CONTENT_VOCABULARIES.popitem(last=False)
        return words

    @api.model
    def _has_unknown_word(self, message, sources, cfg):
        """Whether the question has a long word that is in no document (nor a synonym): probably a typo. Cheap: the
        words of the documents are cached; a question with no long word does not even look."""
        words = unknown_words(message, ())  # long, non-filler words first (the vocabulary is only read if there are any)
        if not words:
            return False
        vocabulary = set(self._content_vocabulary(sources))
        for group in cfg.get("synonyms") or ():
            for member in group:
                vocabulary.update(member.split())
        return bool(unknown_words(message, vocabulary))

    @api.model
    def _widened_groups(self, message, groups, sources, cfg):
        """The question's groups after fixing typos against the vocabulary of titles, folders and synonyms, or None
        when nothing changed. Reads documents with the user's own rules: it can only learn words they may see."""
        try:
            documents = self.env["buddy.document"].search_read(
                [("source_id", "in", sources.ids), ("state", "=", "ready")], ["name", "path"], limit=VOCABULARY_DOCUMENTS)
        except AccessError:
            return None
        texts = [clean_title(document["name"]) + " " + (document["path"] or "") for document in documents]
        texts += sources.mapped("name")
        vocabulary = build_vocabulary(texts, cfg.get("synonyms")) | self._content_vocabulary(sources)
        corrected, changed = correct_message(message, vocabulary)
        if not changed:
            return None
        wider = query_groups(corrected, cfg.get("synonyms"))
        return wider if wider and wider != groups else None

    @api.model
    def _search_rows(self, groups, sources, cfg, weights=None):
        """The matching chunks as (id, document, sequence, score, matched) rows, best first, already trimmed to the ones
        that share enough of the best chunk's matches. `weights` (one whole number per group) makes some terms count
        more than others in `matched`; without it every term counts once."""
        queries = [group_tsquery(group) for group in groups]
        visible = self.env["buddy.chunk"]._search([("source_id", "in", sources.ids)])
        if weights:
            matched = SQL(" + ").join(SQL("%s * (c.buddy_fts @@ to_tsquery('spanish', %s))::int", int(weight), text)
                                      for weight, text in zip(weights, queries))
        else:
            matched = SQL(" + ").join(SQL("(c.buddy_fts @@ to_tsquery('spanish', %s))::int", text) for text in queries)
        rank = SQL("ts_rank_cd(%s::float4[], c.buddy_fts, q.query, 32)", TEXT_RANK_WEIGHTS)
        query = SQL(
            "SELECT c.id, c.document_id, c.sequence, %s AS score, %s AS matched "
            "FROM buddy_chunk c, to_tsquery('spanish', %s) AS q(query) "
            "WHERE c.id IN %s AND c.buddy_fts @@ q.query AND %s >= %s "
            "ORDER BY matched DESC, score DESC, c.id LIMIT %s",
            rank, matched, " | ".join(queries), visible.subselect(), rank,
            cfg.get("text_min_score", TEXT_MIN_SCORE), min(cfg["top_k"] * CANDIDATE_FACTOR, 60))
        try:
            with self.env.cr.savepoint():
                # Pending writes (a computed context, a fresh chunk) must be in the database before the raw query.
                self.env["buddy.chunk"].flush_model(["content", "context_text", "document_id", "sequence"])
                self.env.cr.execute(query)
                rows = self.env.cr.fetchall()
        except Exception as exc:
            _logger.warning("Buddy: búsqueda de texto fallida (%s)", type(exc).__name__)
            raise BuddyBusy("provider_error") from exc
        if rows:
            needed = math.ceil(rows[0][4] * TEXT_MATCH_SHARE)
            rows = [row for row in rows if row[4] >= needed]
        return rows

    @api.model
    def _chunk_dict(self, chunk, score, neighbor=False):
        document = chunk.document_id
        return {"id": chunk.id, "doc_id": document.id, "title": document.name, "sequence": chunk.sequence,
                "owner": document.source_id.name or "", "link": document.web_view_link or "",
                "updated": (document.modified_time or "")[:10], "text": chunk.content[:MAX_CHUNK_CHARS],
                "score": float(score), "neighbor": neighbor}

    @api.model
    def _assemble_context(self, best):
        """The chosen chunks within the context budget, plus the previous/next chunk of each one that still fits.

        Every read goes through the user's own environment (record rules apply to neighbours as to anything else).
        Result order: documents by their best chunk, and within a document by position, so the text reads in order.
        """
        chunk_model = self.env["buddy.chunk"]
        picked, used = [], 0
        for chunk_id, document_id, sequence, score, unused_matched in best:
            try:
                item = self._chunk_dict(chunk_model.browse(chunk_id), score)
            except AccessError:
                continue
            if picked and used + len(item["text"]) > CONTEXT_BUDGET:
                continue
            picked.append(item)
            used += len(item["text"])
        if not picked:
            return []
        have = {(item["doc_id"], item["sequence"]) for item in picked}
        wanted = {}
        for item in picked:
            for sequence in (item["sequence"] + 1, item["sequence"] - 1):
                if sequence >= 0 and (item["doc_id"], sequence) not in have:
                    wanted.setdefault(item["doc_id"], set()).add(sequence)
        neighbours = {}
        if wanted:
            domain = ["|"] * (len(wanted) - 1)
            for document_id, sequences in wanted.items():
                domain += ["&", ("document_id", "=", document_id), ("sequence", "in", sorted(sequences))]
            for chunk in chunk_model.search(domain):
                neighbours[(chunk.document_id.id, chunk.sequence)] = chunk
        result = list(picked)
        for item in picked:
            for sequence in (item["sequence"] + 1, item["sequence"] - 1):  # the next one usually continues the steps
                chunk = neighbours.get((item["doc_id"], sequence))
                if not chunk or (item["doc_id"], sequence) in have:
                    continue
                try:
                    extra = self._chunk_dict(chunk, item["score"], neighbor=True)
                except AccessError:
                    continue
                if used + len(extra["text"]) <= CONTEXT_BUDGET:
                    have.add((item["doc_id"], sequence))
                    result.append(extra)
                    used += len(extra["text"])
        order = {}
        for item in picked:
            order.setdefault(item["doc_id"], len(order))
        result.sort(key=lambda item: (order[item["doc_id"]], item["sequence"]))
        return result

    @api.model
    def _catalog(self):
        """Titles of the documents and folders this user may read, for greetings and 'what can you help me with?'.

        Names come from Drive: they are data. The prompt escapes them and bounds each one (see prompts.build_catalog).
        """
        sources = self.env["buddy.source"].search([("active", "=", True)], order="name")
        documents = self.env["buddy.document"]
        domain = [("source_id", "in", sources.ids), ("state", "=", "ready")]
        total = documents.search_count(domain)
        titles = documents.search(domain, order="source_id, path, name", limit=prompts.CATALOG_MAX_TITLES)
        return {"folders": [clean_title(name) for name in sources.mapped("name")][:prompts.CATALOG_MAX_FOLDERS],
                "titles": [clean_title(name) for name in titles.mapped("name")], "total": total}

    @api.model
    def _build_messages(self, message, history, profile, chunks, cfg, catalog=None):
        roles = self.env["buddy.contact"].sudo().search([]).mapped("code")
        messages = [{"role": "system", "content": prompts.build_system_prompt(
            cfg["buddy_name"], cfg["company"], roles, cfg.get("extra_instructions", ""), cfg.get("dialect"))}]
        return messages + history + [{"role": "user", "content": prompts.build_user_prompt(message, profile, chunks, catalog)}]

    @api.model
    def _ask_model(self, message, history, profile, chunks, cfg, catalog=None):
        messages = self._build_messages(message, history, profile, chunks, cfg, catalog)
        try:
            raw = self.env["buddy.provider"]._chat(cfg["chat_model"], messages, self._remaining(cfg, 30), usage=cfg.get("usage"))
            return prompts.parse_model_json(raw)
        except (ValueError, TypeError) as exc:
            raise BuddyBusy("invalid_response") from exc
        except TimeoutError as exc:
            raise BuddyBusy("provider_timeout") from exc
        except Exception as exc:
            _logger.warning("Buddy: proveedor falló (%s)", getattr(exc, "code", type(exc).__name__))
            raise BuddyBusy(self._provider_code(exc)) from exc

    @staticmethod
    def _provider_code(exc):
        """«provider_auth» when the provider refused the key or the permissions (nothing a retry can fix and the
        administrator must hear about it), «provider_error» for anything else."""
        return "provider_auth" if isinstance(exc, ProviderError) and exc.code == "invalid_key" else "provider_error"

    @api.model
    def _history(self, cfg, turns=6):
        rows = self.env["buddy.message"].sudo().search([("user_id", "=", self.env.uid)], order="id desc", limit=turns * 2)
        # Limit history tokens as well as turn count (each character is paid for with every question).
        history, size = [], 0
        for row in rows:
            if size + len(row.content) > HISTORY_CHARS:
                break
            history.append({"role": row.role, "content": row.content})
            size += len(row.content)
        return list(reversed(history))

    @api.model
    def _remember(self, role, content, generation=1, request_id=None, response=None, meta=None):
        """Store a message. `meta` is the statistics data of the answer (`_insight_data`): the documents it cited and the
        model are kept on the private message so that a vote can be written later without the browser sending them."""
        values = {"user_id": self.env.uid, "role": role, "content": content,
                  "generation": generation, "request_id": request_id, "response_json": response}
        token = (response or {}).get("feedback_token")
        if role == "assistant" and isinstance(token, str) and FEEDBACK_TOKEN.fullmatch(token):
            values["feedback_token"] = token
            values["feedback_context"] = {"docs": [int(item) for item in (meta or {}).get("document_ids") or []],
                                          "model": str((meta or {}).get("model") or "")[:200]}
        self.env["buddy.message"].sudo().create(values)

    @api.model
    def _is_new(self, cfg):
        return self.env["buddy.message"].sudo().search_count([("user_id", "=", self.env.uid), ("role", "=", "user")]) < cfg["new_turns"]

    @api.model
    def _normalize(self, raw, message):
        try:
            result = prompts.validate_model_output(raw)
        except (ValueError, TypeError) as exc:
            raise BuddyBusy("invalid_response") from exc
        if not is_greeting(message):
            result["answer"] = strip_greeting(result["answer"])
        return result

    @api.model
    def _used_sources(self, chunks, used):
        """The sources the model says it used (`used`: ids of the <doc> tags, numbered from 1 in the order they were
        sent), one per document. Without that list (None) or with ids that point at nothing, every retrieved
        document is listed, as before; an explicit empty list means it used none (small talk, off topic)."""
        return self._dedupe_sources(self._used_chunks(chunks, used))

    @api.model
    def _used_chunks(self, chunks, used):
        """The retrieved chunks the model says it used (same rule as `_used_sources`, before removing duplicates)."""
        if used is None:
            return list(chunks)
        picked = [chunks[number - 1] for number in sorted(set(used)) if 1 <= number <= len(chunks)]
        if used and not picked:
            return list(chunks)
        return picked

    @api.model
    def _dedupe_sources(self, chunks):
        seen, out = set(), []
        for chunk in chunks:
            if chunk["doc_id"] not in seen:
                seen.add(chunk["doc_id"])
                source = {key: chunk[key] for key in ("title", "owner", "updated")}
                source["link"] = safe_drive_link(chunk.get("link"))
                out.append(source)
        return out

    @api.model
    def _contact_for(self, role):
        contacts = self.env["buddy.contact"].sudo()
        code = (role or "general").strip().lower()
        contact = contacts.search([("code", "=", code)], limit=1) or contacts.search([("code", "=", "general")], limit=1)
        if not contact:
            return None
        # A sample address (…@ejemplo.test) goes nowhere: the card still says WHO to ask but offers no mail link.
        email = "" if is_placeholder_email(contact.email) else contact.email
        return {"role": contact.code, "label": contact.label, "name": contact.name, "contact": email}

    @api.model
    def _self_test(self):
        """The administrator's «Probar conexión»: (ok, readable lines). Never sends anything to the provider unless the
        key and the model are there, but LISTS the models the account can use as soon as there is a key, so an
        administrator who does not know the model's name finds it here."""
        self._require_internal()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Solo un administrador puede probar la conexión."))
        with self._fresh_engine(lock=True) as engine:
            usage, error = engine._reserve_usage({"rate_min": 2, "rate_day": 20, "scope": "diagnostic"}, 0, str(uuid.uuid4()))
            if not error:
                usage.write({"state": "done"})
        if error:
            raise BuddyBusy(error)
        cfg, lines, ok = self._cfg(), [], True
        cfg["deadline"] = time.monotonic() + 60
        parameters = self.env["ir.config_parameter"].sudo()
        # 1. The AI provider: key accepted, a model chosen, and that model is one this account can use.
        provider = self.env["buddy.provider"]
        model = cfg["chat_model"]
        if not parameters.get_param("buddy_ia.api_key"):
            ok = False
            lines.append(_("Falta la clave de API del proveedor de IA."))
        else:
            listed = None
            try:
                listed = provider._list_models(timeout=self._remaining(cfg, 15))
            except ProviderError as exc:
                ok = False
                lines.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                ok = False
                lines.append(_("No pude consultar al proveedor (%(error)s).", error=type(exc).__name__))
            shown = ", ".join(listed[:MODELS_SHOWN]) + (" …" if listed and len(listed) > MODELS_SHOWN else "") if listed else ""
            if listed is not None and not model:
                ok = False
                lines.append(_("Todavía no elegiste un modelo. Los que tu cuenta puede usar: %(models)s. "
                               "Copiá uno en «Modelo» y guardá.", models=shown) if listed else
                             _("Todavía no elegiste un modelo. Escribí en «Modelo» el nombre exacto que muestra tu proveedor."))
            elif listed is None and not model:
                ok = False
                lines.append(_("Todavía no elegiste un modelo. Escribí en «Modelo» el nombre exacto que muestra tu proveedor."))
            elif listed and model not in listed:
                ok = False
                lines.append(_("El modelo «%(model)s» no existe en tu cuenta. Disponibles: %(models)s.", model=model, models=shown))
            elif listed is not None:
                lines.append(_("Proveedor de IA OK (%(count)s modelos disponibles)", count=len(listed)) if listed
                             else _("Proveedor de IA OK"))
            if ok and model:
                try:
                    with self.env.cr.savepoint():
                        self._ask_model("Respondé una confirmación breve.", [], {"name": "prueba", "is_new": False},
                                        [{"title": "Prueba", "owner": "Diagnóstico", "updated": "", "text": "La conexión funciona."}], cfg)
                    lines.append(_("Respuesta de prueba OK (%(model)s)", model=model))
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    detail = exc.code if isinstance(exc, BuddyBusy) else type(exc).__name__
                    lines.append(_("El modelo no respondió con el formato esperado (%(detail)s).", detail=detail))
        # 1b. The search by meaning, only when an administrator turned it on: the provider must answer the embeddings request.
        embeddings = self.env["buddy.embedding"]
        if embeddings._semantic_requested():
            semantic_ok, line = embeddings._self_test_semantic(self._remaining(cfg, 15))
            ok = ok and semantic_ok
            lines.append(line)
        # 2. Google Drive: the key loads and every connected folder is reachable and indexed.
        if not parameters.get_param("buddy_ia.drive_key"):
            ok = False
            lines.append(_("Falta la clave de Google Drive."))
        sources = self.env["buddy.source"].sudo().search([("active", "=", True)])
        if not sources:
            ok = False
            lines.append(_("No hay carpetas de Drive conectadas."))
        self.env.cr.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'buddy_chunk' AND column_name = 'buddy_fts'")
        if not self.env.cr.fetchone():
            ok = False
            lines.append(_("Falta el índice de búsqueda: actualizá el módulo Buddy IA."))
        for source in sources:
            chunks = self.env["buddy.chunk"].sudo().search_count([("source_id", "=", source.id)])
            if source.state == "error":
                ok = False
                lines.append(_("Carpeta «%(name)s» con error: %(error)s", name=source.name, error=source.last_error or _("sin detalle")))
            elif not source.group_ids:
                ok = False
                lines.append(_("Carpeta «%(name)s»: elegí quiénes pueden consultarla.", name=source.name))
            elif not chunks:
                ok = False
                lines.append(_("Carpeta «%(name)s» todavía no tiene documentos indexados: sincronizala.", name=source.name))
            else:
                lines.append(_("Carpeta «%(name)s» OK (%(count)s fragmentos)", name=source.name, count=chunks))
        return ok, lines
