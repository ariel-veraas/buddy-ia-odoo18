"""Private, content-free ledger. Conversation deletion never changes quotas."""
from datetime import timedelta
from odoo import api, fields, models


class BuddyUsage(models.Model):
    _name = "buddy.usage"
    _description = "Reserva privada de cuota del Buddy"
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    request_id = fields.Char(required=True, index=True)
    generation = fields.Integer(required=True)
    scope = fields.Selection([("chat", "Chat"), ("diagnostic", "Diagnóstico")], required=True, default="chat", index=True)
    state = fields.Selection([("reserved", "Reservada"), ("done", "Terminada"), ("failed", "Fallida")], required=True, default="reserved")
    error_code = fields.Char()
    _sql_constraints = [("request_unique", "unique(user_id, request_id)", "Pedido ya reservado.")]

    @api.model
    def _cron_purge(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=2))]).unlink()
