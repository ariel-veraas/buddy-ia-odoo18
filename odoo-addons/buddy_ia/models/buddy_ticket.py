import logging
from datetime import timedelta
from uuid import UUID, uuid4

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)

# The person (a user) who takes care of the tickets, chosen in Ajustes > Buddy IA.
TICKET_USER_PARAM = "buddy_ia.ticket_user_id"
# The user to whom BUDDY gave the «Gestor de Buddy» permission when choosing them: the one it may take away again when
# somebody else is chosen (a permission an administrator gave by hand is never touched).
TICKET_GRANTED_PARAM = "buddy_ia.ticket_user_granted"


def is_buddy_manager(user):
    """Administrators and «Gestor de Buddy» members: the people who handle every ticket and read the statistics."""
    return user.has_group("base.group_system") or user.has_group("buddy_ia.group_buddy_manager")


class BuddyTicket(models.Model):
    _name = "buddy.ticket"
    _description = "Ticket creado desde el Buddy"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    # The person who asked can read their own ticket and write in its chatter (to answer the responsible person)
    # without having write access to the ticket itself.
    _mail_post_access = "read"

    name = fields.Char(string="Asunto", required=True)
    description = fields.Text(string="Detalle")
    user_id = fields.Many2one("res.users", string="Solicitante", required=True,
                              default=lambda self: self.env.user, ondelete="restrict")
    # Who handles it: the person chosen in Ajustes when the ticket is created (they get a to-do and the chatter);
    # a Buddy manager can reassign it.
    assigned_user_id = fields.Many2one("res.users", string="Responsable", copy=False, index=True, ondelete="set null",
                                       tracking=True, domain=[("share", "=", False)])
    # No column default: an upgrade must leave legacy rows NULL, not assign a
    # single evaluated UUID to every existing ticket. create() assigns each key.
    request_key = fields.Char(copy=False, index=True)
    # For the form: the fields are read-only for whoever is not a manager (they could not save a change anyway).
    is_manager = fields.Boolean(compute="_compute_is_manager")
    state = fields.Selection([("new", "Nuevo"), ("progress", "En curso"), ("done", "Resuelto")],
                             string="Estado", default="new", required=True, tracking=True)

    _sql_constraints = [("unique_user_request", "unique(user_id, request_key)",
                         "Esta solicitud ya tiene un ticket.")]

    @api.depends_context("uid")
    def _compute_is_manager(self):
        manager = is_buddy_manager(self.env.user)
        for ticket in self:
            ticket.is_manager = manager

    @api.model
    def _buddy_responsible(self):
        """The user chosen as «Responsable de tickets» (False when none, or when that user is gone or not internal)."""
        try:
            user_id = int(self.env["ir.config_parameter"].sudo().get_param(TICKET_USER_PARAM) or 0)
        except ValueError:
            return False
        user = self.env["res.users"].sudo().browse(user_id).exists() if user_id else False
        return user if user and user.active and not user.share else False

    @api.model_create_multi
    def create(self, vals_list):
        is_admin = self.env.su or self.env.user.has_group("base.group_system")
        responsible = self._buddy_responsible()
        created = self.browse()
        for original in vals_list:
            vals = dict(original)
            if not is_admin:
                if vals.get("user_id", self.env.uid) != self.env.uid:
                    raise AccessError(_("Solo podés crear tus propios tickets."))
                if vals.get("state", "new") != "new":
                    raise AccessError(_("Los tickets nuevos deben comenzar en estado Nuevo."))
                vals["user_id"], vals["state"] = self.env.uid, "new"
                # Whoever asks never picks who answers: the responsible person is the one set in the settings.
                vals.pop("assigned_user_id", None)
            if responsible and not vals.get("assigned_user_id"):
                vals["assigned_user_id"] = responsible.id
            title, description = vals.get("name", ""), vals.get("description") or ""
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
                raise ValidationError(_("El asunto debe contener entre 1 y 120 caracteres."))
            if not isinstance(description, str) or len(description) > 2000:
                raise ValidationError(_("El detalle debe contener hasta 2000 caracteres."))
            vals["name"], vals["description"] = title.strip(), description
            try:
                vals["request_key"] = str(UUID(vals.get("request_key") or str(uuid4())))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError(_("Identificador de solicitud inválido.")) from exc
            user_id = vals.get("user_id", self.env.uid)
            existing = self.search([("user_id", "=", user_id), ("request_key", "=", vals["request_key"])], limit=1)
            if existing:
                if existing.name != vals["name"] or (existing.description or "") != description:
                    raise ValidationError(_("El identificador ya fue usado para otro ticket."))
                created |= existing
            else:
                if not is_admin:
                    self._check_daily_cap(user_id)
                new = super().create([vals])
                new._buddy_notify_responsible()
                created |= new
        return created

    def _buddy_notify_responsible(self):
        """A to-do for the responsible person (they also receive the usual notification) and a seat in the chatter.

        Done with sudo: the person who asked has no right to schedule activities for someone else.
        """
        for ticket in self.sudo():
            user = ticket.assigned_user_id
            if not user:
                continue
            try:
                # A responsible person who cannot be notified (no right to the record, a broken mail setup) must
                # never stop the ticket from being created: the person who asked comes first.
                with self.env.cr.savepoint():
                    ticket.message_subscribe(partner_ids=user.partner_id.ids)
                    detail = (ticket.description or "").strip()[:500]
                    ticket.activity_schedule(
                        "mail.mail_activity_data_todo", user_id=user.id,
                        summary=_("Ticket nuevo del Buddy: %(title)s", title=ticket.name[:80]),
                        note=Markup("<p>%s</p>") % detail if detail else False)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("Buddy: no pude avisarle al responsable (usuario %s) del ticket %s (%s)",
                                user.id, ticket.id, type(exc).__name__)

    @api.model
    def _check_daily_cap(self, user_id):
        """Tope por usuario y por día, aplicado al modelo (vale para la web y para el ORM directo)."""
        limit = int(self.env["ir.config_parameter"].sudo().get_param("buddy_ia.tickets_per_day") or 20)
        since = fields.Datetime.now() - timedelta(days=1)
        if self.sudo().search_count([("user_id", "=", user_id), ("create_date", ">=", since)]) >= limit:
            raise ValidationError(_("Llegaste al máximo de tickets por día. Probá mañana o escribí al contacto indicado."))

    @api.constrains("name", "description")
    def _check_content(self):
        for record in self:
            if not record.name.strip() or len(record.name.strip()) > 120 or len(record.description or "") > 2000:
                raise ValidationError(_("El ticket excede los límites de asunto o detalle."))
