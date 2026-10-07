"""18.0.4.7.0: statistics that really never say who asked, and who got the «Gestor de Buddy» permission from Buddy.

* `buddy_insight.create_uid` / `write_uid` are the employee who asked in rows written before this version: they all
  become the superuser (id 1), the way new rows are written. The stored document titles (they could name files in
  folders a manager cannot read) are dropped: titles are now read when the screen opens, with the viewer's rights.
* 18.0.4.6.0 made «Settings / Administration» imply the «Gestor de Buddy» group. The module's data no longer says
  so, but the link already written in the database stays unless it is taken away here: it is. (Administrators who were
  given the group by that link keep their membership: they are administrators anyway; nobody is added or removed.)
* If an administrator already chose a responsible person for tickets in 18.0.4.6.0 (the only way, besides doing it by
  hand, to become a manager without being an administrator), that person is remembered as the one Buddy gave the
  permission to, so choosing somebody else takes it back.

It is safe to run again. Nothing here can rotate a secret: anybody who ran 18.0.4.4.0 to 18.0.4.6.0 must rotate the AI
provider's API key (see docs/SECURITY.md): those versions let any employee read it through the generic RPC.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("UPDATE buddy_insight SET create_uid = %s, write_uid = %s WHERE create_uid IS DISTINCT FROM %s OR write_uid IS DISTINCT FROM %s",
               (SUPERUSER_ID,) * 4)
    cr.execute("ALTER TABLE buddy_insight DROP COLUMN IF EXISTS document_titles")
    env = api.Environment(cr, SUPERUSER_ID, {})
    parameters = env["ir.config_parameter"].sudo()
    group = env.ref("buddy_ia.group_buddy_manager", raise_if_not_found=False)
    administration = env.ref("base.group_system")
    if group and group in administration.implied_ids:
        administration.write({"implied_ids": [(3, group.id)]})
    try:
        responsible_id = int(parameters.get_param("buddy_ia.ticket_user_id") or 0)
    except ValueError:
        responsible_id = 0
    if group and responsible_id and not parameters.get_param("buddy_ia.ticket_user_granted"):
        user = env["res.users"].sudo().browse(responsible_id).exists()
        if user and group in user.groups_id and not user.has_group("base.group_system"):
            parameters.set_param("buddy_ia.ticket_user_granted", str(user.id))
    _logger.warning("Buddy 18.0.4.7.0: si esta base tuvo instaladas las versiones 18.0.4.4.0 a 18.0.4.6.0, rotá la clave de API del proveedor de IA.")
