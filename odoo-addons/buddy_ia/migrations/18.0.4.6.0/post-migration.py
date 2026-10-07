"""18.0.4.6.0: the default of queries per person per day drops from 150 to 40.

The settings screen saved the old default (150) as an explicit value whenever somebody pressed «Guardar», so an
installation that never touched the limit has "150" stored. Only that exact old default moves to the new one; a value
somebody chose on purpose (anything else) stays as it is. It is safe to run again.

Everything else new in this version needs no data step: the statistics table, the ticket chatter and the new settings
(company-wide daily limit, prices, retention) all have defaults in the code, and the «Gestor de Buddy» group is given to
administrators by the group itself (it is implied by «Settings / Administration»).
"""
from odoo import SUPERUSER_ID, api

OLD_RATE_DAY, NEW_RATE_DAY = "150", "40"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    parameters = env["ir.config_parameter"].sudo()
    if parameters.get_param("buddy_ia.rate_per_day") == OLD_RATE_DAY:
        parameters.set_param("buddy_ia.rate_per_day", NEW_RATE_DAY)
