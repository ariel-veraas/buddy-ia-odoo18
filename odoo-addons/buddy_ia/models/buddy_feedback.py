"""The 👍 / 👎 a person gives to one answer of the buddy: an ANONYMOUS row for the quality panel.

Privacy is the point of this model, so the rules are in one place and as strict as the ones of `buddy.insight`:

* A row never says who voted. No user, partner or login field, and not even the `create_uid` / `write_uid` / dates Odoo adds
  to every row (`_log_access` is off): the only time it keeps is `day`, a plain date.
* It does not point at the answer or at the conversation either: the private `buddy.message` row (which does have an owner)
  holds the opaque token of the answer and a pointer to the row of its vote, never the other way round. When the
  conversation is deleted (new conversation, retention) the pointer disappears and the vote is left with no link at all.
* It keeps the value, the reason (a closed list), the day, the model and the documents the answer cited. The TEXT of the
  question only when the person ticked «Incluir mi pregunta para mejorar a Buddy» or the administrator turned on
  «Guardar preguntas» (the same setting that governs `buddy.insight`).
* Rows are deleted after the same retention period as the other statistics (90 days by default).
* The documents of a vote are kept only when the folder they live in is readable by at least `MIN_AUDIENCE` people
  (parameter `buddy_ia.feedback_min_audience`, 5 by default): a 👎 on a document that only two people can read would
  name the voter. The vote itself still counts (value, reason, day), just without that document.
* Managers see a vote only after the day it was given is over (`visible`, set by the daily cron, and a record rule): looking
  again every minute would show the time of each vote. (Not a date in the rule itself: Odoo caches the evaluated domain of
  a rule until the registry reloads, so the date would freeze.)

Rows are written by the engine (`buddy.engine.feedback`) as the superuser; no access right allows creating, changing or
deleting them through the interface.
"""
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models

from .buddy_privacy import REASON_CODES, REASONS, VOTE_CODES, VOTES
from .buddy_text import strip_control_chars

QUESTION_LIMIT = 500
MIN_AUDIENCE_PARAM = "buddy_ia.feedback_min_audience"
MIN_AUDIENCE = 5
TITLES_LIMIT = 500
PURGE_BATCH = 5000
DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650


class BuddyFeedback(models.Model):
    _name = "buddy.feedback"
    _inherit = ["buddy.guard"]
    _description = "Opinión anónima sobre una respuesta del Buddy"
    _order = "id desc"
    # Only administrators may search or group by the cited documents (see buddy_guard.py).
    _admin_only_fields = ("document_ids",)
    # No create_uid / write_uid / create_date / write_date: nothing may say who voted (or exactly when).
    _log_access = False

    value = fields.Selection(VOTES, string="Voto", required=True, readonly=True, index=True)
    reason = fields.Selection(REASONS, string="Motivo", readonly=True,
                              help="Solo para 👎, y solo si la persona lo eligió.")
    day = fields.Date(string="Día", required=True, readonly=True, index=True,
                      help="El día del voto (sin la hora, a propósito: no hace falta más para el panel). Los gestores ven los votos desde el día siguiente.")
    # Managers only read the rows that are visible; the daily cron (`_reveal`) turns on those of earlier days. Administrators
    # read all of them. A vote that is rewritten (the person changed their mind) is hidden again until the next day.
    visible = fields.Boolean(string="Visible para gestores", default=False, readonly=True, index=True,
                             help="Los gestores ven los votos desde el día siguiente (así no se puede deducir la hora de cada voto mirando la lista a cada rato).")
    # Always 1: the pivot adds it up to count the votes (a measure the pivot can show next to the percentage).
    votes = fields.Integer(string="Votos", default=1, readonly=True, aggregator="sum")
    # 1 for 👍 and 0 for 👎: its average is the share of positive votes (the panel shows it as a percentage).
    positive = fields.Float(string="% positivos", readonly=True, aggregator="avg", digits=(3, 2))
    question = fields.Text(string="Pregunta", readonly=True,
                           help="Solo si la persona tildó «Incluir mi pregunta para mejorar a Buddy» o si activaste «Guardar preguntas» en Ajustes.")
    model_name = fields.Char(string="Modelo", readonly=True)
    document_ids = fields.Many2many("buddy.document", "buddy_feedback_document_rel", "feedback_id", "document_id",
                                    string="Documentos (registros)", readonly=True, groups="base.group_system")
    # Not stored: read when the screen opens, only the titles the person looking at it may read (as in buddy.insight).
    document_titles = fields.Text(string="Documentos citados", compute="_compute_document_titles",
                                  help="Los documentos que citó la respuesta. Solo se muestran los que podés leer.")

    @api.depends("document_ids")
    @api.depends_context("uid")
    def _compute_document_titles(self):
        documents = self.env["buddy.document"]
        for record in self:
            ids = record.sudo().document_ids.ids
            readable = documents.search([("id", "in", ids)]) if ids else documents  # the user's own record rules
            record.document_titles = " · ".join(readable.mapped("name"))[:TITLES_LIMIT] or False

    @api.model
    def _min_audience(self):
        """How many people must be able to read a folder for its documents to be kept in a vote (1 = always)."""
        try:
            value = int(self.env["ir.config_parameter"].sudo().get_param(MIN_AUDIENCE_PARAM) or MIN_AUDIENCE)
        except ValueError:
            return MIN_AUDIENCE
        return value if 1 <= value <= 10000 else MIN_AUDIENCE

    @api.model
    def _anonymous_documents(self, documents):
        """The documents of a vote, or none when too few people could have given it.

        Whoever voted can read EVERY folder the answer cited, so the set of possible voters is the people who can read all
        of them (the intersection, not each folder on its own): two folders of ten readers each can still leave one
        person who reads both. Counts active internal users in the groups of the folders (the same rule that lets them read
        the folder). It is the number of people who COULD have voted, not of those who use Buddy: with a single active user
        in the company nothing helps, which is why the minimum is a technical parameter and not a setting.
        """
        minimum = self._min_audience()
        if minimum <= 1 or not documents:
            return documents
        domain = [("active", "=", True), ("share", "=", False)]
        for source in documents.sudo().mapped("source_id"):
            domain.append(("groups_id", "in", source.group_ids.ids))
        return documents if self.env["res.users"].sudo().search_count(domain) >= minimum else documents.browse()

    @api.model
    def _store(self, existing, data):
        """Create the row of a vote, or rewrite the one of the same answer when the person changed their mind.

        `data`: value, reason, keep_question, question, model, document_ids (all plain data, already validated by the
        engine; the closed lists are checked again here, the privacy rule on the question lives here only).
        """
        self = self.with_user(SUPERUSER_ID)
        value = data.get("value")
        if value not in VOTE_CODES:
            raise ValueError("value")
        reason = data.get("reason") if value == "down" and data.get("reason") in REASON_CODES else False
        question = strip_control_chars(str(data.get("question") or "")).strip()[:QUESTION_LIMIT]
        documents = self.env["buddy.document"].sudo().browse([int(item) for item in data.get("document_ids") or []]).exists()
        documents = self._anonymous_documents(documents)
        values = {
            "value": value, "reason": reason, "day": fields.Date.today(), "visible": False, "positive": 1.0 if value == "up" else 0.0,
            "question": question if data.get("keep_question") and question else False,
            "model_name": str(data.get("model") or "")[:200] or False,
            "document_ids": [(6, 0, documents.ids)],
        }
        if existing:
            existing = existing.with_user(SUPERUSER_ID)
            existing.write(values)
            return existing
        return self.create(values)

    @api.model
    def _reveal(self):
        """Daily: the votes of earlier days become visible to managers (administrators always see everything)."""
        rows = self.sudo().search([("visible", "=", False), ("day", "<", fields.Date.today())])
        rows.write({"visible": True})
        return len(rows)

    @api.model
    def _cron_purge(self, days=None):
        """Delete the rows older than the retention period of the statistics (the same setting, in batches)."""
        if days is None:
            try:
                days = int(self.env["ir.config_parameter"].sudo().get_param("buddy_ia.insight_days") or DEFAULT_RETENTION_DAYS)
            except ValueError:
                days = DEFAULT_RETENTION_DAYS
        days = min(max(days, 1), MAX_RETENTION_DAYS)  # never "keep forever"
        cutoff = fields.Date.today() - timedelta(days=days)
        while True:
            old = self.sudo().search([("day", "<", cutoff)], limit=PURGE_BATCH)
            if not old:
                return
            old.unlink()

    @api.model
    def _delete_everything(self):
        """Administrator action (privacy): erase every vote and every stored question, and the vote that the private
        messages remember (the pointer and the time: the person's own chat keeps showing nothing)."""
        count = self.sudo().search_count([])
        self.env["buddy.message"].flush_model()  # pending writes first: they must not land after the UPDATE and undo it
        self.env.cr.execute("UPDATE buddy_message SET feedback_vote = NULL, feedback_id = NULL, feedback_at = NULL "
                            "WHERE feedback_vote IS NOT NULL OR feedback_id IS NOT NULL OR feedback_at IS NOT NULL")
        self.env["buddy.message"].invalidate_model(["feedback_vote", "feedback_id", "feedback_at"])
        while True:
            rows = self.sudo().search([], limit=PURGE_BATCH)
            if not rows:
                return count
            rows.unlink()
