"""The suggestions of the widget chips that fit the person (E4b), without a single call to the AI provider.

Three sources, all of them text the widget sends as a question when it is tapped:

* the suggested questions of the person's onboarding steps that are due today or late, and the ones that come next (the
  record rules of the journey models decide what is theirs: nothing here uses `sudo` to read a step);
* the topics the company asks about most: the documents cited by at least `MIN_TOPIC_QUERIES` different queries of the last
  30 days. Counted anonymously from `buddy.insight` (which never says who asked), only the TITLE of a document is ever
  shown, only when the person can read it with their own rights, and only for documents whose folder at least
  `buddy_ia.feedback_min_audience` people can read (with fewer, the count itself would describe somebody);
* the document of a «read» step that has no question of its own.

Never the text of anybody's question, never a number of any kind, never a name. The public method is `@api.private`: the only
door is the controller (`/buddy/suggestions`), which validates the request and the CSRF token first.
"""
import logging
import time
from datetime import datetime, time as day_start, timedelta

from odoo import _, api, fields, models

from . import buddy_journey_logic as journey_logic
from . import buddy_proactive_logic as logic

_logger = logging.getLogger(__name__)

# The topics are the same for everybody: they are computed once per database every half hour (and again when the day changes: only
# closed days are counted) and each person's own rights are applied afterwards (a person sees the titles of the documents they may
# read, nothing else).
_TOPICS = {}  # database name -> (the monotonic time it stops being valid, the day it was computed, [(document id, number of queries)])
MAX_STEP_ROWS = 40


class BuddyProactive(models.AbstractModel):
    _name = "buddy.proactive"
    _description = "Sugerencias del chat según la persona y la empresa"

    @api.model
    def _switch(self, param):
        """One of the switches of the settings (on unless it says «0»)."""
        return logic.switch_on(self.env["ir.config_parameter"].sudo().get_param(param))

    @api.model
    def _clear_cache(self):
        _TOPICS.pop(self.env.cr.dbname, None)

    @api.model
    @api.private
    def suggestions(self):
        """`{"suggestions": [{"kind", "text", "label", "ask_id"?}]}` for the chips of the person who asks (at most POOL_LIMIT;
        the widget shows three or four of them and rotates). Empty when the administrator turned them off. Anything that fails
        here only means no suggestions: the chat never depends on this."""
        self.env["buddy.engine"]._require_internal()
        if not self._switch(logic.CHIPS_PARAM):
            return {"suggestions": []}
        today = fields.Date.context_today(self)
        due, upcoming, topics = [], [], []
        try:
            due, upcoming = self._step_questions(today)
        except Exception as exc:  # noqa: BLE001 - presentation only: never break the chat for it
            _logger.warning("Buddy: no pude armar las sugerencias del onboarding (%s)", type(exc).__name__)
        try:
            topics = self._topic_questions()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Buddy: no pude armar las sugerencias por temas (%s)", type(exc).__name__)
        return {"suggestions": logic.build_pool(due, topics, upcoming)}

    # ------------------------------------------------------------------ onboarding steps (the person's own rows)
    @api.model
    def _step_questions(self, today):
        """`(due, next)`: the suggested questions of the person's pending steps due today or before, and of the ones due in the
        next days, as texts or `(text, progress id)` pairs (the id only for a step of the kind «preguntarle al buddy»).

        Runs with the rights of the person: the record rules of `buddy.journey.progress` return their own rows and nobody else's.
        """
        horizon = today + timedelta(days=journey_logic.UPCOMING_DAYS)
        rows = self.env["buddy.journey.progress"].search([
            ("user_id", "=", self.env.uid), ("done", "=", False), ("assignment_id.state", "=", "active"),
            ("assignment_id.start_date", "<=", today), ("due_date", "<=", horizon)], order="due_date, id", limit=MAX_STEP_ROWS)
        rows = rows.filtered(lambda row: row.journey_id.active)
        reading = rows.filtered(lambda row: row.step_id.kind == "read" and not row.step_id.question and row.step_id.document_id)
        # A document the person cannot read is not found, so a step that points to it has nothing to offer.
        documents = {document.id: document for document in self.env["buddy.document"].search(
            [("id", "in", reading.mapped("step_id.document_id").ids)])} if reading else {}
        template = self._topic_template()
        due, upcoming = [], []
        for row in rows:
            step = row.step_id
            text = journey_logic.clean_text(step.question, journey_logic.QUESTION_CHARS)
            ask_id = row.id if text and step.kind == "ask" else None
            if not text and step.document_id.id in documents:
                text = logic.fit_title(template, documents[step.document_id.id].name, limit=logic.CHIP_TEXT_CHARS)
            if text:
                (due if row.due_date <= today else upcoming).append((text, ask_id))
        return due, upcoming

    @api.model
    def _topic_template(self):
        # One literal string (no concatenation): the translation exporter only extracts literals.
        return _("¿Qué dice «%(title)s»?")

    # ------------------------------------------------------------------ the topics the company asks about most
    @api.model
    def _topic_questions(self):
        """The questions about the documents most cited lately that THIS person may read (titles only, at most MAX_TOPIC_CHIPS)."""
        ranked = self._eligible_topics()
        if not ranked:
            return []
        # The person's own record rules: a document of a folder they cannot read is not found, so it is never offered.
        readable = {document.id: document for document in self.env["buddy.document"].search([("id", "in", [item[0] for item in ranked])])}
        template = self._topic_template()
        texts = []
        for document_id, _count in ranked:
            document = readable.get(document_id)
            # The question carries the whole title (a cut one would search for something else); only the button shows it shortened.
            text = logic.fit_title(template, document.name, limit=logic.CHIP_TEXT_CHARS) if document else ""
            if text:
                texts.append(text)
            if len(texts) >= logic.MAX_TOPIC_CHIPS:
                break
        return texts

    @api.model
    def _eligible_topics(self):
        """`[(document id, queries)]`, most cited first, of the documents that enough people can read: the same for everybody, so
        it is kept for half an hour (a few queries per hour, not one per page)."""
        key, now, today = self.env.cr.dbname, time.monotonic(), fields.Date.today()
        hit = _TOPICS.get(key)
        if hit and hit[0] > now and hit[1] == today:
            # Another worker may have had the statistics erased («Borrar estadísticas»): with none left there is nothing to offer.
            if not hit[2] or self._has_statistics():
                return hit[2]
        try:
            ranked, seconds = self._compute_topics(), logic.TOPIC_CACHE_SECONDS
        except Exception as exc:  # noqa: BLE001 - a slow or failing query is not retried by every request: «none» for a minute
            _logger.warning("Buddy: no pude calcular los temas más consultados (%s)", type(exc).__name__)
            ranked, seconds = [], logic.TOPIC_ERROR_SECONDS
        _TOPICS[key] = (now + seconds, today, ranked)
        return ranked

    @api.model
    def _has_statistics(self):
        self.env.cr.execute("SELECT EXISTS (SELECT 1 FROM buddy_insight)")
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def _compute_topics(self):
        """The documents cited by at least MIN_TOPIC_QUERIES answered queries on at least MIN_TOPIC_DAYS different closed days of the
        last TOPIC_DAYS days (today is not counted).

        Counts rows of `buddy.insight` (one per query, no person) through the relation table with an index per column, bounded by
        the retention of the statistics and by the daily limit of queries, and never longer than TOPIC_STATEMENT_MS. Only documents
        that are ready, in an active folder that at least MIN_TOPIC_AUDIENCE people can read (or the minimum chosen for the votes, when
        it is higher) are kept.
        """
        until = datetime.combine(fields.Date.today(), day_start.min)
        since = until - timedelta(days=logic.TOPIC_DAYS)
        self.env.flush_all()  # plain SQL below: pending writes of the ORM (the relation with the documents) must be in the database
        with logic.statement_timeout(self.env.cr, logic.TOPIC_STATEMENT_MS):
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    """SELECT rel.document_id, COUNT(DISTINCT rel.insight_id) AS queries
                         FROM buddy_insight_document_rel rel
                         JOIN buddy_insight insight ON insight.id = rel.insight_id
                        WHERE insight.create_date >= %s AND insight.create_date < %s
                          AND insight.needs_human IS NOT TRUE AND insight.error IS NULL
                        GROUP BY rel.document_id
                       HAVING COUNT(DISTINCT rel.insight_id) >= %s AND COUNT(DISTINCT insight.create_date::date) >= %s
                        ORDER BY queries DESC, rel.document_id
                        LIMIT %s""", (since, until, logic.MIN_TOPIC_QUERIES, logic.MIN_TOPIC_DAYS, logic.TOPIC_CANDIDATES * 3))
                rows = self.env.cr.fetchall()
        if not rows:
            return []
        documents = self.env["buddy.document"].sudo().browse([row[0] for row in rows]).exists()
        documents = documents.filtered(lambda document: document.state == "ready" and document.source_id.active)
        # The minimum chosen for the votes can be raised (never below the one of the topics: «1» there only means «votes keep their
        # documents always» and must not open the topics of a folder two people read).
        minimum = max(self.env["buddy.feedback"]._min_audience(), logic.MIN_TOPIC_AUDIENCE)
        audience = {}
        for source in documents.mapped("source_id"):
            audience[source.id] = self.env["res.users"].sudo().search_count(
                [("active", "=", True), ("share", "=", False), ("groups_id", "in", source.group_ids.ids)]) >= minimum
        ready = {document.id: document for document in documents if audience.get(document.source_id.id)}
        return [(document_id, queries) for document_id, queries in rows if document_id in ready][: logic.TOPIC_CANDIDATES]
