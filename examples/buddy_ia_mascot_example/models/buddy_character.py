from odoo import fields, models
from odoo.addons.buddy_ia.models import buddy_look

# 1. Server whitelist: /buddy/config only ever sends ids registered here (lowercase letters, digits and "_").
buddy_look.register_character("mascot_example", "Mascota de ejemplo")


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # 2. The Settings screen offers it. The drawing itself is registered in static/src/js/mascot_example.js.
    buddy_character = fields.Selection(selection_add=[("mascot_example", "Mascota de ejemplo")])
