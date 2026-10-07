from datetime import timedelta

from odoo import api, fields, models

# Hard ceiling, whatever the retention setting says (even 0 = "never delete by inactivity"): no message outlives it.
MAX_MESSAGE_AGE_DAYS = 180
PURGE_BATCH = 5000


class BuddyMessage(models.Model):
    """Memoria de conversación por usuario. Solo el motor del buddy accede (sudo): nadie la lee por la interfaz."""

    _name = "buddy.message"
    _description = "Mensaje del buddy"
    _order = "id"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    role = fields.Selection([("user", "Usuario"), ("assistant", "Buddy")], required=True)
    content = fields.Text(required=True)
    generation = fields.Integer(required=True, default=1, index=True)
    request_id = fields.Char(index=True)
    response_json = fields.Json(help="Respuesta validada para reintentos del mismo pedido.")
    # Feedback (👍/👎) of an answer. This table is private and has an owner, so it is the only place that ties a vote to a
    # person: the anonymous row of buddy.feedback never points back here (see buddy_feedback.py).
    feedback_token = fields.Char(index=True, copy=False,
                                 help="Código aleatorio de la respuesta: sin él (y sin ser su dueño) nadie puede votarla.")
    feedback_context = fields.Json(copy=False, help="Lo que el voto guarda de la respuesta: documentos citados y modelo.")
    feedback_vote = fields.Selection([("up", "👍"), ("down", "👎")], copy=False)
    feedback_id = fields.Many2one("buddy.feedback", ondelete="set null", copy=False,
                                  help="La fila anónima de su voto (se pierde el vínculo al borrar la conversación).")
    feedback_changes = fields.Integer(default=0, copy=False, help="Cuántas veces se votó o cambió el voto (tiene tope).")
    feedback_at = fields.Datetime(copy=False, index=True, help="Último voto: base del límite por minuto de cada persona.")

    @api.model
    def _cron_purge(self):
        """Retention: a conversation is kept until `retention_days` days after its LAST message (0 = keep).

        The clock is per user, not per message: someone who chats every week never loses the start of the
        conversation they are still in; one who stops for N days loses all of it at once. On top of that, no
        message is kept longer than MAX_MESSAGE_AGE_DAYS since it was written, even in an ongoing conversation.
        """
        days = int(self.env["ir.config_parameter"].sudo().get_param("buddy_ia.retention_days", "30") or 0)
        if days > 0:
            cutoff = fields.Datetime.now() - timedelta(days=days)
            last = self.sudo()._read_group([], ["user_id"], ["create_date:max"])
            stale = [user.id for user, newest in last if newest and newest < cutoff]
            if stale:
                self.sudo().search([("user_id", "in", stale)]).unlink()
        self._purge_older_than(MAX_MESSAGE_AGE_DAYS)
        self.env["buddy.usage"]._cron_purge()

    @api.model
    def _purge_older_than(self, days):
        """Delete every message written more than `days` days ago (in batches: it can be a lot of rows)."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        while True:
            old = self.sudo().search([("create_date", "<", cutoff)], limit=PURGE_BATCH)
            if not old:
                return
            old.unlink()
