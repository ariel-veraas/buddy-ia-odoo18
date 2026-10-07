"""Private generation fence for authorization changes and deletion."""
from odoo import fields, models


class BuddySession(models.Model):
    _name = "buddy.session"
    _description = "Estado privado de sesión del Buddy"
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    generation = fields.Integer(required=True, default=1)
    authorization_hash = fields.Char(required=True)
    inflight_id = fields.Many2one("buddy.usage", ondelete="set null")
    inflight_until = fields.Datetime()
    _sql_constraints = [("user_unique", "unique(user_id)", "Una sesión por usuario.")]
