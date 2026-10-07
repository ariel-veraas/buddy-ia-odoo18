"""Statistics of the queries the buddy handled: one row per question, never editable, only for Buddy managers.

What is kept (and what is deliberately not):

* NEVER the name of the person. A row says what happened to a question, not who asked it. That includes the
  `create_uid` / `write_uid` Odoo adds to every row: they are always the superuser (see `_record`), never the employee.
* The TEXT of the question only when the buddy could not answer it (so the documentation can be completed) or when
  the administrator turned on «Guardar preguntas». Otherwise the row has numbers only.
* Rows are deleted after a retention period (90 days by default) by a daily cron.

Rows are written by the engine with `sudo()` (see `buddy.engine._store_insight`); no access right allows creating,
changing or deleting them through the interface.
"""
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models
from odoo.tools.sql import create_index

from .buddy_privacy import QUESTIONS_PARAM, flag  # noqa: F401 (flag is re-exported: the settings import it from here)
from .buddy_text import strip_control_chars

INSIGHT_DAYS_PARAM = "buddy_ia.insight_days"
PRICE_IN_PARAM = "buddy_ia.price_in_per_m"
PRICE_OUT_PARAM = "buddy_ia.price_out_per_m"
DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650
QUESTION_LIMIT = 500
TITLES_LIMIT = 500
PURGE_BATCH = 5000

ERRORS = [
    ("provider_error", "El proveedor de IA falló"),
    ("provider_auth", "Clave o permisos del proveedor"),
    ("provider_timeout", "El proveedor tardó demasiado"),
    ("invalid_response", "Respuesta del modelo inválida"),
    ("session_changed", "La conversación o los permisos cambiaron"),
    ("client_disconnected", "La persona cerró el chat"),
    ("not_configured", "Sin configurar"),
    ("other", "Otro error"),
]
KNOWN_ERRORS = {code for code, label in ERRORS}
SEMANTIC_STATES = [
    ("used", "Usada"),
    ("failed", "Falló (se usó solo el texto)"),
    ("paused", "En pausa (el proveedor venía fallando)"),
    ("too_big", "Demasiados fragmentos (se usó solo el texto)"),
    ("no_time", "Sin tiempo (se usó solo el texto)"),
]
KNOWN_SEMANTIC_STATES = {code for code, label in SEMANTIC_STATES}
CONFIDENCES = [("high", "Alta"), ("medium", "Media"), ("low", "Baja")]
KNOWN_CONFIDENCES = {code for code, label in CONFIDENCES}


def price(value):
    """A price per million tokens from a parameter: a finite number between 0 and a million, else 0."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if 0 <= number <= 1_000_000 else 0.0


class BuddyInsight(models.Model):
    _name = "buddy.insight"
    _inherit = ["buddy.guard"]
    _description = "Estadística de una consulta al Buddy"
    _order = "id desc"
    # Only administrators may search or group by the cited documents (buddy_guard.py): by name they would learn titles
    # of documents in folders they cannot see.
    _admin_only_fields = ("document_ids",)

    question = fields.Text(string="Pregunta", readonly=True,
                           help="Solo se guarda si el buddy no supo responder, o si activaste «Guardar preguntas» en Ajustes.")
    needs_human = fields.Boolean(string="Sin respuesta", readonly=True, index=True,
                                 help="El buddy no encontró la respuesta en la documentación y derivó a la persona.")
    retrieved_count = fields.Integer(string="Fragmentos encontrados", readonly=True,
                                     help="Cuántos pasajes de la documentación recuperó la búsqueda (sin contar los vecinos).")
    best_score = fields.Float(string="Mejor puntaje", readonly=True, digits=(4, 2),
                              help="Qué tan bien coincidió el mejor pasaje con la pregunta (de 0 a 1). Cerca de 0: la documentación casi no habla del tema.")
    document_ids = fields.Many2many("buddy.document", "buddy_insight_document_rel", "insight_id", "document_id",
                                    string="Documentos (registros)", readonly=True, groups="base.group_system")
    # Not stored: the titles are read when the screen opens and only the ones the person looking at it may read (the
    # same record rules as the documents themselves). A manager must not learn the title of a file in a folder they
    # cannot see just because somebody else's question cited it.
    document_titles = fields.Text(string="Documentos usados", compute="_compute_document_titles",
                                  search="_search_document_titles",
                                  help="Los documentos que el buddy citó en su respuesta. Solo se muestran los que podés leer.")
    model_name = fields.Char(string="Modelo", readonly=True)
    tokens_in = fields.Integer(string="Tokens de entrada", readonly=True,
                               help="Lo que informó el proveedor. 0 = el proveedor no lo informó.")
    tokens_out = fields.Integer(string="Tokens de salida", readonly=True,
                                help="Lo que informó el proveedor. 0 = el proveedor no lo informó.")
    cost = fields.Float(string="Costo estimado (USD)", readonly=True, digits=(12, 4),
                        help="Tokens por el precio por millón que cargaste en Ajustes, al momento de la consulta. 0 si no cargaste precios.")
    latency_ms = fields.Integer(string="Demora (ms)", readonly=True, help="Desde que se aceptó la consulta hasta que terminó.")
    error = fields.Selection(ERRORS, string="Error", readonly=True)
    # What the search by meaning did in this query (empty = it was off or not tried: greetings, small talk).
    semantic_state = fields.Selection(SEMANTIC_STATES, string="Búsqueda por significado", readonly=True,
                                      help="Si la búsqueda por significado se usó en esta consulta, falló o quedó en pausa. Vacío = no se probó.")
    semantic_ms = fields.Integer(string="Demora por significado (ms)", readonly=True,
                                 help="Lo que tardó la búsqueda por significado (la pregunta al proveedor y la comparación), incluida en la demora total.")
    # How sure the answer was (the model's own report: it is tolerant, so it may be empty) and whether the documents
    # contradicted each other: what the administrator should fix in Drive. No text of the question or the answer.
    confidence = fields.Selection(CONFIDENCES, string="Confianza", readonly=True,
                                  help="Qué tan respaldada estaba la respuesta por la documentación, según el propio modelo. Vacío = no lo dijo.")
    conflict = fields.Boolean(string="Documentos contradictorios", readonly=True, index=True,
                              help="Dos o más documentos decían cosas distintas sobre lo mismo: conviene unificarlos en Drive.")
    # What the charts group by (a boolean would be drawn as «true / false»).
    outcome = fields.Selection([("answered", "Respondida"), ("unanswered", "Sin respuesta"), ("failed", "Con error")],
                               string="Resultado", compute="_compute_outcome", store=True, index=True)

    @api.depends("needs_human", "error")
    def _compute_outcome(self):
        for record in self:
            record.outcome = "failed" if record.error else "unanswered" if record.needs_human else "answered"

    @api.depends("document_ids")
    @api.depends_context("uid")
    def _compute_document_titles(self):
        documents = self.env["buddy.document"]
        for record in self:
            ids = record.sudo().document_ids.ids
            readable = documents.search([("id", "in", ids)]) if ids else documents  # the user's own record rules
            record.document_titles = " · ".join(readable.mapped("name"))[:TITLES_LIMIT] or False

    def _search_document_titles(self, operator, value):
        documents = self.env["buddy.document"].search([("name", operator, value)])
        rows = self.sudo().search([("document_ids", "in", documents.ids)])
        return [("id", "in", rows.ids)]

    def init(self):
        create_index(self.env.cr, "buddy_insight_create_date_idx", self._table, ["create_date"])

    @api.model
    def _record(self, data):
        """Store one row from what the engine measured. The privacy rule lives here, in one place."""
        # The row is created BY THE SUPERUSER: `create_uid` and `write_uid` would otherwise be the employee who asked.
        self = self.with_user(SUPERUSER_ID)
        parameters = self.env["ir.config_parameter"].sudo()
        needs_human = bool(data.get("needs_human"))
        keep_question = needs_human or flag(parameters.get_param(QUESTIONS_PARAM))
        question = strip_control_chars(str(data.get("question") or "")).strip()[:QUESTION_LIMIT]
        tokens_in, tokens_out = int(data.get("tokens_in") or 0), int(data.get("tokens_out") or 0)
        cost = (tokens_in * price(parameters.get_param(PRICE_IN_PARAM))
                + tokens_out * price(parameters.get_param(PRICE_OUT_PARAM))) / 1_000_000
        documents = self.env["buddy.document"].sudo().browse([int(item) for item in data.get("document_ids") or []]).exists()
        error = data.get("error") or False
        return self.create({
            "question": question if keep_question and question else False,
            "needs_human": needs_human,
            "retrieved_count": int(data.get("retrieved_count") or 0),
            "best_score": min(1.0, max(0.0, float(data.get("best_score") or 0.0))),
            "document_ids": [(6, 0, documents.ids)],
            "model_name": str(data.get("model") or "")[:200] or False,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost": cost,
            "latency_ms": int(data.get("latency_ms") or 0),
            "error": (error if error in KNOWN_ERRORS else "other") if error else False,
            "semantic_state": data.get("semantic_state") if data.get("semantic_state") in KNOWN_SEMANTIC_STATES else False,
            "semantic_ms": max(0, int(data.get("semantic_ms") or 0)),
            "confidence": data.get("confidence") if data.get("confidence") in KNOWN_CONFIDENCES else False,
            "conflict": bool(data.get("conflict")),
        })

    @api.model
    def _spent_since(self, since):
        """Estimated cost of the queries after `since` (0 when no prices were ever set)."""
        total = self.sudo()._read_group([("create_date", ">=", since)], [], ["cost:sum"])
        return float(total[0][0] or 0.0) if total else 0.0

    @api.model
    def _usage_summary(self, since):
        """(queries, tokens in, tokens out, estimated cost) since `since`: the figures of the settings screen."""
        row = self.sudo()._read_group([("create_date", ">=", since)], [],
                                      ["__count", "tokens_in:sum", "tokens_out:sum", "cost:sum"])
        count, tokens_in, tokens_out, cost = row[0] if row else (0, 0, 0, 0.0)
        return count or 0, tokens_in or 0, tokens_out or 0, float(cost or 0.0)

    @api.model
    def _recent_failures(self, since):
        """(how many queries failed after `since`, the readable reason of the most recent one or '')."""
        failed = self.sudo().search([("create_date", ">=", since), ("error", "!=", False)], order="id desc")
        if not failed:
            return 0, ""
        labels = dict(self._fields["error"]._description_selection(self.env))
        return len(failed), labels.get(failed[0].error, failed[0].error)

    @api.model
    def _cron_purge(self):
        """Delete the rows older than the retention period (in batches: it can be many)."""
        try:
            days = int(self.env["ir.config_parameter"].sudo().get_param(INSIGHT_DAYS_PARAM) or DEFAULT_RETENTION_DAYS)
        except ValueError:
            days = DEFAULT_RETENTION_DAYS
        days = min(max(days, 1), MAX_RETENTION_DAYS)  # never "keep forever": a bad value falls back to something safe
        cutoff = fields.Datetime.now() - timedelta(days=days)
        while True:
            old = self.sudo().search([("create_date", "<", cutoff)], limit=PURGE_BATCH)
            if not old:
                break
            old.unlink()
        # The votes on answers (buddy.feedback) are statistics too: same retention, same daily run (and the votes of earlier
        # days become visible to the managers).
        self.env["buddy.feedback"]._cron_purge(days)
        self.env["buddy.feedback"]._reveal()
        # The weekly summaries are made from these statistics: they go away with the same retention.
        self.env["buddy.digest"]._cron_purge(days)

    @api.model
    def _delete_everything(self):
        """Administrator action (privacy): erase every statistics row and every stored question."""
        count = self.sudo().search_count([])
        self.env["buddy.feedback"]._delete_everything()  # the votes (and the questions some of them kept) go too
        self.env["buddy.digest"]._delete_everything()  # and the weekly summaries (they hold masked examples of questions)
        self.env["buddy.proactive"]._clear_cache()  # the topics suggested in the chat were counted from these rows
        while True:
            rows = self.sudo().search([], limit=PURGE_BATCH)
            if not rows:
                return count
            rows.unlink()
