"""Onboarding journeys (E4): a journey (a list of steps relative to the joining date), who has it (assignments) and how far
each person got (progress, one row per person and step).

Who sees what is decided by record rules (security/buddy_security.xml), deny by default:
- every person reads and ticks ONLY their own assignment and progress (and only the journeys they were given);
- the «Responsable de onboarding» creates journeys, assigns them and reads the progress of the people THEY assigned;
- administrators see everything.
Nothing here knows about the chat: no conversation, question or statistic is read or written.
"""
import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from . import buddy_journey_logic as logic
from .buddy_journey_logic import REMINDERS_PARAM, STAGE_PARAM  # noqa: F401 (also read by the settings, the engine and the tests)

_logger = logging.getLogger(__name__)

ACTIVITY_TYPE_XMLID = "buddy_ia.activity_type_journey"
MANAGER_GROUP = "buddy_ia.group_journey_manager"
# The most a daily run assigns or reminds in one go (a company of thousands is handled over several days, never in one
# long transaction).
BATCH = 200
REMINDER_TITLES = 8


def is_journey_manager(user):
    """Administrators and «Responsable de onboarding» members: the people who build journeys and assign them."""
    return user.has_group("base.group_system") or user.has_group(MANAGER_GROUP)


class BuddyJourney(models.Model):
    _name = "buddy.journey"
    _description = "Recorrido de onboarding"
    _inherit = ["buddy.guard"]
    _order = "name, id"
    # Searching or grouping a journey by who has it would tell one person who else is on it: only the people who manage
    # journeys may (see models/buddy_guard.py).
    _admin_only_fields = ("assignment_ids", "group_ids", "auto_assign", "auto_since")
    _guard_groups = ("base.group_system", MANAGER_GROUP)

    name = fields.Char(string="Nombre", required=True, help="Cómo lo ve la persona: por ejemplo «Primeros 30 días de ventas».")
    description = fields.Text(string="Descripción", help="Para quién es y qué busca lograr (la ve la persona).")
    active = fields.Boolean(default=True, help="Archivarlo lo pausa para todos: nadie ve sus tareas ni recibe recordatorios.")
    group_ids = fields.Many2many(
        "res.groups", "buddy_journey_group_rel", "journey_id", "group_id", string="Aplica a estos grupos",
        groups="base.group_system,buddy_ia.group_journey_manager",
        help="El grupo de Odoo de las personas a las que corresponde este recorrido. Se usa solo para la asignación automática "
             "(la manual sirve para cualquiera).")
    auto_assign = fields.Boolean(
        string="Asignar solo a las personas nuevas", default=False, copy=False,
        groups="base.group_system,buddy_ia.group_journey_manager",
        help="Si lo activás, las personas que se creen en Odoo DESPUÉS de este momento y pertenezcan a alguno de los grupos de arriba "
             "reciben este recorrido solas, con su fecha de ingreso. Nunca se asigna a quienes ya existen.")
    auto_since = fields.Datetime(string="Asignación automática desde", readonly=True, copy=False,
                                 groups="base.group_system,buddy_ia.group_journey_manager")
    step_ids = fields.One2many("buddy.journey.step", "journey_id", string="Pasos", copy=True)
    assignment_ids = fields.One2many("buddy.journey.assignment", "journey_id", string="Asignaciones",
                                     groups="base.group_system,buddy_ia.group_journey_manager")
    step_count = fields.Integer(compute="_compute_counts")
    assignment_count = fields.Integer(compute="_compute_counts", groups="base.group_system,buddy_ia.group_journey_manager")

    @api.depends("step_ids", "assignment_ids")
    @api.depends_context("uid")
    def _compute_counts(self):
        for journey in self:
            journey.step_count = len(journey.sudo().step_ids)
            # With the caller's own rights: a Responsable counts the people THEY assigned, not everybody's.
            journey.assignment_count = len(journey.assignment_ids) if is_journey_manager(self.env.user) else 0

    @api.constrains("name", "description")
    def _check_text(self):
        for journey in self:
            if not (journey.name or "").strip() or len(journey.name) > logic.TITLE_CHARS:
                raise ValidationError(_("El nombre del recorrido debe tener entre 1 y %(max)s caracteres.", max=logic.TITLE_CHARS))
            if len(journey.description or "") > 1000:
                raise ValidationError(_("La descripción del recorrido puede tener hasta 1000 caracteres."))

    @api.constrains("auto_assign", "group_ids")
    def _check_auto_assign(self):
        for journey in self.sudo():
            if journey.auto_assign and not journey.group_ids:
                raise ValidationError(_("Para asignarlo solo a las personas nuevas, elegí al menos un grupo en «Aplica a estos grupos»."))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for original in vals_list:
            vals = dict(original)
            vals.pop("auto_since", None)  # only Odoo sets it, when the option is switched on
            if vals.get("auto_assign"):
                vals["auto_since"] = fields.Datetime.now()
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        if "auto_since" in vals and not self.env.su:
            raise AccessError(_("La fecha desde la que se asigna sola no se edita: la fija Odoo al activar la opción."))
        if "auto_assign" in vals:
            if not vals["auto_assign"]:
                vals = dict(vals, auto_since=False)
            else:
                # The moment it is switched on is the line the automatic assignment never crosses back: nobody who already
                # exists is given the journey. Switching it off and on again moves the line forward.
                turning_on = self.filtered(lambda journey: not journey.sudo().auto_assign)
                if turning_on:
                    super(BuddyJourney, turning_on).write(dict(vals, auto_since=fields.Datetime.now()))
                rest = self - turning_on
                return super(BuddyJourney, rest).write(vals) if rest else True
        return super().write(vals)

    def copy(self, default=None):
        """A copy starts active, with the automatic assignment off and a name that says it is a copy."""
        default = dict(default or {})
        default.setdefault("name", _("%(name)s (copia)", name=self.name))
        default.setdefault("active", True)
        return super().copy(default)

    def action_duplicate_activate(self):
        """«Duplicar y activar»: the way to start from the archived example (or from any journey)."""
        self.ensure_one()
        if not is_journey_manager(self.env.user):
            raise AccessError(_("Solo quien administra el onboarding puede duplicar un recorrido."))
        copy = self.copy()
        return {"type": "ir.actions.act_window", "res_model": "buddy.journey", "res_id": copy.id, "view_mode": "form", "target": "current"}

    def action_open_assignments(self):
        """The people who have this journey (the record rules decide which ones the caller may see)."""
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "buddy.journey.assignment", "view_mode": "list,form",
                "name": _("Asignaciones de onboarding"), "domain": [("journey_id", "=", self.id)]}

    def action_assign(self):
        """Open the «Asignar recorrido» window for this journey."""
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "buddy.journey.assign", "view_mode": "form", "target": "new",
                "name": _("Asignar recorrido"), "context": {"default_journey_id": self.id}}

    # ------------------------------------------------------------------ automatic assignment (people created later)
    @api.model
    def _cron_daily(self):
        """The daily run: first the journeys that ask to be given to new people, then the reminders (so a person created today
        gets the reminder of their first day today)."""
        self._cron_auto_assign()
        self.env["buddy.journey.assignment"]._cron_reminders()

    @api.model
    def _cron_auto_assign(self):
        """Give the journeys that asked for it to the people created AFTER the option was switched on.

        Never retroactive: only users whose creation date is later than `auto_since` are considered, and a person who ever
        had that journey (even cancelled) is never given it again.
        """
        today = fields.Date.context_today(self)
        created = 0
        for journey in self.sudo().search([("auto_assign", "=", True), ("active", "=", True)]):
            if not journey.group_ids or not journey.auto_since or not journey.step_ids:
                continue
            # Whoever ever had this journey (even cancelled) is left out of the search itself, so a long list of them never
            # starves the people still waiting.
            had = self.env["buddy.journey.assignment"].sudo().search([("journey_id", "=", journey.id)]).mapped("user_id").ids
            users = self.env["res.users"].sudo().search([
                ("share", "=", False), ("active", "=", True), ("create_date", ">=", journey.auto_since),
                ("groups_id", "in", journey.group_ids.ids), ("id", "not in", had)], order="id", limit=BATCH)
            for user in users:
                joined = fields.Datetime.to_datetime(user.create_date).date()
                # Found long after joining (they were added to the group later): count from today, not from a date that
                # would make every step overdue.
                start = joined if 0 <= (today - joined).days <= 7 else today
                try:
                    with self.env.cr.savepoint():
                        self.env["buddy.journey.assignment"].sudo().create(
                            {"user_id": user.id, "journey_id": journey.id, "start_date": start, "assigner_id": False})
                    created += 1
                except Exception as exc:  # noqa: BLE001 - one person never stops the rest
                    _logger.warning("Buddy onboarding: no pude asignar el recorrido %s al usuario %s (%s)", journey.id, user.id, type(exc).__name__)
        return created


class BuddyJourneyStep(models.Model):
    _name = "buddy.journey.step"
    _description = "Paso de un recorrido de onboarding"
    _order = "day_offset, sequence, id"

    journey_id = fields.Many2one("buddy.journey", string="Recorrido", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Título", required=True)
    detail = fields.Text(string="Detalle", help="Una o dos frases: qué tiene que hacer la persona.")
    day_offset = fields.Integer(
        string="Días desde el ingreso", default=0, required=True,
        help="Cuántos días después de la fecha de ingreso toca este paso: 0 es el mismo día del ingreso, 7 una semana después, 30 el primer mes. "
             "Los pasos se muestran ordenados por este número.")
    kind = fields.Selection(logic.STEP_KINDS, string="Tipo", default="task", required=True)
    is_required = fields.Boolean(string="Obligatorio", default=True,
                                 help="Los pasos opcionales no cuentan para completar el recorrido ni generan recordatorios.")
    document_id = fields.Many2one(
        "buddy.document", string="Documento", ondelete="set null",
        help="Un documento de las carpetas de Buddy. La persona lo abre solo si tiene permiso para leerlo.")
    url = fields.Char(string="Enlace", help="Una dirección https (sin usuario ni contraseña). Se usa si no elegís un documento.")
    question = fields.Char(string="Pregunta sugerida", help="Si la completás, el paso muestra «Preguntarle al buddy» y abre el chat con esta pregunta.")
    contact_id = fields.Many2one("buddy.contact", string="Con quién hablar", ondelete="set null",
                                 help="Opcional: la persona o el equipo al que puede consultar por este paso.")

    @api.constrains("name", "detail", "question", "day_offset", "url", "kind")
    def _check_values(self):
        for step in self:
            title = (step.name or "").strip()[:60] or _("(sin título)")
            if not (step.name or "").strip() or len(step.name) > logic.TITLE_CHARS:
                raise ValidationError(_("El título de un paso debe tener entre 1 y %(max)s caracteres.", max=logic.TITLE_CHARS))
            if len(step.detail or "") > logic.DETAIL_CHARS:
                raise ValidationError(_("En el paso «%(step)s»: el detalle puede tener hasta %(max)s caracteres.", step=title, max=logic.DETAIL_CHARS))
            if len(step.question or "") > logic.QUESTION_CHARS:
                raise ValidationError(_("En el paso «%(step)s»: la pregunta sugerida puede tener hasta %(max)s caracteres.", step=title, max=logic.QUESTION_CHARS))
            if not 0 <= step.day_offset <= logic.MAX_DAY_OFFSET:
                raise ValidationError(_("En el paso «%(step)s»: los días desde el ingreso van de 0 (el mismo día) a %(max)s.", step=title, max=logic.MAX_DAY_OFFSET))
            if step.url and not logic.safe_https_url(step.url):
                raise ValidationError(_("En el paso «%(step)s»: pegá la dirección completa, que empiece con https:// (por ejemplo https://miempresa.com/guia), sin espacios ni usuario y contraseña.", step=title))

    @api.constrains("document_id")
    def _check_document(self):
        """A step can only point to a document its author can read (the person who follows the link needs the same right
        and, anyway, the author should not be able to learn the name of a document they cannot see)."""
        if self.env.su:
            return
        for step in self.filtered("document_id"):
            if not self.env["buddy.document"].search_count([("id", "=", step.document_id.id)]):
                raise ValidationError(_("En el paso «%(step)s»: no tenés permiso para leer ese documento. Elegí otro.", step=(step.name or "")[:60]))

    @api.constrains("journey_id")
    def _check_step_count(self):
        for journey in self.mapped("journey_id"):
            if len(journey.sudo().step_ids) > logic.MAX_STEPS:
                raise ValidationError(_("Un recorrido puede tener hasta %(max)s pasos.", max=logic.MAX_STEPS))

    def write(self, vals):
        # A step stays in its journey: the progress rows of the people who follow it point to both.
        if "journey_id" in vals and not self.env.su and any(step.journey_id.id != vals["journey_id"] for step in self):
            raise ValidationError(_("Un paso no se puede mover a otro recorrido: borralo y creá uno nuevo."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        steps = super().create(vals_list)
        # Steps added to a journey that people already follow reach them (the rows are made by the system, not by the author).
        steps._add_to_running_assignments()
        return steps

    def _add_to_running_assignments(self):
        Progress = self.env["buddy.journey.progress"].sudo()
        for step in self:
            assignments = self.env["buddy.journey.assignment"].sudo().search(
                [("journey_id", "=", step.journey_id.id), ("state", "!=", "cancelled")])
            existing = set(Progress.search([("step_id", "=", step.id)]).mapped("assignment_id").ids)
            Progress.create([{"assignment_id": assignment.id, "step_id": step.id} for assignment in assignments if assignment.id not in existing])
            assignments._refresh_state()

    def unlink(self):
        assignments = self.env["buddy.journey.progress"].sudo().search([("step_id", "in", self.ids)]).mapped("assignment_id")
        result = super().unlink()
        assignments._refresh_state()
        return result


class BuddyJourneyAssignment(models.Model):
    _name = "buddy.journey.assignment"
    _description = "Recorrido de onboarding asignado a una persona"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "start_date desc, id desc"
    # No `_mail_post_access`: the person who got the journey reads and finishes their own reminder (the activity rule of Odoo
    # lets the assignee write their own activity) but cannot post in the chatter nor schedule activities on the record.

    user_id = fields.Many2one("res.users", string="Persona", required=True, ondelete="cascade", index=True,
                              domain=[("share", "=", False)], readonly=True)
    journey_id = fields.Many2one("buddy.journey", string="Recorrido", required=True, ondelete="restrict", index=True, readonly=True)
    start_date = fields.Date(string="Fecha de ingreso", required=True, default=fields.Date.context_today,
                             help="El día 0 del recorrido: todos los días de los pasos se cuentan desde acá.")
    state = fields.Selection(logic.ASSIGNMENT_STATES, string="Estado", default="active", required=True, index=True)
    assigner_id = fields.Many2one("res.users", string="Asignado por", readonly=True, copy=False, ondelete="set null", index=True)
    progress_ids = fields.One2many("buddy.journey.progress", "assignment_id", string="Avance")
    total_count = fields.Integer(string="Pasos", compute="_compute_progress", store=True)
    done_count = fields.Integer(string="Hechos", compute="_compute_progress", store=True)
    progress_pct = fields.Integer(string="Avance (%)", compute="_compute_progress", store=True, aggregator="avg")
    overdue_count = fields.Integer(string="Atrasados", compute="_compute_overdue")
    # For the form: only the people who manage journeys can change the joining date (the rest could not save it anyway).
    can_manage = fields.Boolean(compute="_compute_can_manage")

    _sql_constraints = [("user_journey_unique", "unique(user_id, journey_id)", "Esta persona ya tiene ese recorrido.")]

    @api.depends("user_id", "journey_id")
    def _compute_display_name(self):
        for assignment in self:
            assignment.display_name = "%s — %s" % (assignment.user_id.name or "", assignment.journey_id.name or "")

    @api.depends("progress_ids.done", "progress_ids.step_id.is_required")
    def _compute_progress(self):
        for assignment in self:
            rows = assignment.progress_ids
            # Only the required steps count (all of them when none is required): an optional extra never holds a person back.
            counted = rows.filtered(lambda row: row.step_id.is_required) or rows
            done = len(counted.filtered("done"))
            assignment.total_count, assignment.done_count = len(counted), done
            assignment.progress_pct = logic.percent(done, len(counted))

    @api.depends_context("uid")
    def _compute_can_manage(self):
        manager = is_journey_manager(self.env.user)
        for assignment in self:
            assignment.can_manage = manager

    @api.depends("progress_ids.done", "progress_ids.due_date", "progress_ids.step_id.is_required")
    @api.depends_context("uid")
    def _compute_overdue(self):
        today = fields.Date.context_today(self)
        for assignment in self:
            assignment.overdue_count = len(assignment.progress_ids.filtered(
                lambda row: row.step_id.is_required and not row.done and row.due_date and row.due_date < today))

    @api.constrains("start_date")
    def _check_start_date(self):
        today = fields.Date.context_today(self)
        for assignment in self:
            if assignment.start_date and not today - timedelta(days=5 * 366) <= assignment.start_date <= today + timedelta(days=2 * 366):
                raise ValidationError(_("La fecha de ingreso tiene que estar entre hace 5 años y dentro de 2 años."))

    @api.model_create_multi
    def create(self, vals_list):
        if not is_journey_manager(self.env.user) and not self.env.su:
            raise AccessError(_("Solo quien administra el onboarding puede asignar recorridos."))
        prepared = []
        for original in vals_list:
            vals = dict(original)
            if not self.env.su and not self.env.user.has_group("base.group_system"):
                vals["assigner_id"] = self.env.uid  # nobody hands out journeys in somebody else's name
            elif "assigner_id" not in vals:
                vals["assigner_id"] = self.env.uid if not self.env.su else False
            user = self.env["res.users"].sudo().browse(vals.get("user_id")).exists()
            if not user or user.share or not user.active:
                raise ValidationError(_("El recorrido se asigna solo a personas internas y activas."))
            journey = self.env["buddy.journey"].sudo().browse(vals.get("journey_id")).exists()
            if not journey or not journey.active:
                raise ValidationError(_("Ese recorrido no existe o está archivado. Activalo antes de asignarlo."))
            if not journey.step_ids:
                raise ValidationError(_("El recorrido no tiene pasos todavía: agregá al menos uno antes de asignarlo."))
            vals["state"] = "active"
            prepared.append(vals)
        # No «created» line in a chatter nobody reads, no tracking and no follower: the reminder is the only mail thing here.
        assignments = super(BuddyJourneyAssignment, self.with_context(mail_create_nolog=True, mail_notrack=True, mail_create_nosubscribe=True)).create(prepared)
        Progress = self.env["buddy.journey.progress"].sudo()
        Progress.create([{"assignment_id": assignment.id, "step_id": step.id}
                         for assignment in assignments for step in assignment.sudo().journey_id.step_ids])
        assignments.sudo()._refresh_state()
        return assignments

    def write(self, vals):
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            if not is_journey_manager(self.env.user):
                raise AccessError(_("Solo quien administra el onboarding puede cambiar una asignación."))
            if set(vals) - {"start_date", "state"}:
                raise AccessError(_("De una asignación solo se puede cambiar la fecha de ingreso y el estado."))
        if vals.get("state") not in (None, "active", "done", "cancelled"):
            raise ValidationError(_("Estado de asignación inválido."))
        result = super().write(vals)
        if "state" in vals or "start_date" in vals:
            self.sudo()._refresh_state()
        if vals.get("state") == "cancelled":
            self.sudo()._reminder_close()
        return result

    def _message_subscribe(self, partner_ids=None, subtype_ids=None, customer_ids=None):
        """Nobody follows an assignment. Odoo subscribes the person of an activity to its record, and `mail.followers` can be
        read by every employee: it would list who is in onboarding. The reminder needs no follower."""
        return self.env["mail.followers"]

    def action_cancel(self):
        self.write({"state": "cancelled"})
        self.sudo()._reminder_close()
        return True

    def action_reactivate(self):
        self.write({"state": "active"})
        return True

    def _refresh_state(self):
        """«Completado» when every counted step is done, «En curso» otherwise; a cancelled assignment stays cancelled.

        Runs with the rights of the system on purpose: the person who ticks the last step has no right to write the
        assignment, and the state is a consequence of their own progress, never something they choose.
        """
        for assignment in self.sudo().filtered(lambda row: row.state != "cancelled"):
            new = "done" if assignment.total_count and assignment.done_count >= assignment.total_count else "active"
            if assignment.state != new:
                assignment.write({"state": new})
            if new == "done":
                assignment._reminder_close()  # nothing left to remind
        return True

    # ------------------------------------------------------------------ reminders (Odoo activities)
    @api.model
    def _reminders_enabled(self):
        return str(self.env["ir.config_parameter"].sudo().get_param(REMINDERS_PARAM) or "1").strip().lower() not in ("0", "false", "no", "off")

    @api.model
    def _cron_reminders(self):
        """Once a day: a single summary activity per person and journey with what is due today or late (and none when
        nothing is, closing the one that was left open). Never one activity per step, never a second one for the same
        assignment."""
        if not self._reminders_enabled():
            return 0
        today = fields.Date.context_today(self)
        count = 0
        assignments = self.sudo().search([("state", "=", "active"), ("journey_id.active", "=", True), ("start_date", "<=", today)],
                                         order="id", limit=BATCH * 50)
        for assignment in assignments:
            if not assignment.user_id.active or assignment.user_id.share:
                continue
            try:
                with self.env.cr.savepoint():
                    count += 1 if assignment._reminder_sync(today) else 0
            except Exception as exc:  # noqa: BLE001 - one broken person never stops the rest
                _logger.warning("Buddy onboarding: no pude armar el recordatorio de la asignación %s (%s)", assignment.id, type(exc).__name__)
        return count

    def _reminder_activities(self):
        """The open reminder activities of this assignment (there is at most one; more would be a leftover to clean up)."""
        self.ensure_one()
        activity_type = self.env.ref(ACTIVITY_TYPE_XMLID, raise_if_not_found=False)
        if not activity_type:
            return self.env["mail.activity"]
        return self.env["mail.activity"].sudo().search([
            ("res_model", "=", self._name), ("res_id", "=", self.id), ("activity_type_id", "=", activity_type.id),
            ("user_id", "=", self.user_id.id)], order="id")

    def _reminder_close(self):
        for assignment in self:
            assignment._reminder_activities().unlink()

    def _due_rows(self, today):
        """The required steps still undone that are due today or before."""
        self.ensure_one()
        rows = self.progress_ids.filtered(lambda row: row.step_id.is_required and not row.done and row.due_date and row.due_date <= today)
        return rows.sorted(lambda row: (row.due_date, row.step_id.sequence, row.id))

    def _reminder_sync(self, today=None, create=True):
        """Create (only when `create`), refresh or close the reminder of this assignment. True when an activity is left open."""
        self.ensure_one()
        today = today or fields.Date.context_today(self)
        due = self._due_rows(today) if self.state == "active" and self.journey_id.active else self.env["buddy.journey.progress"]
        activities = self._reminder_activities()
        if not due:
            activities.unlink()
            return False
        if not activities and not create:
            return False
        user = self.user_id
        # In the person's language, not the one of the server's cron user.
        summary, note = self.with_context(lang=user.lang or self.env.lang)._reminder_texts(due)
        if activities:
            first = activities[0]
            (activities - first).unlink()  # leftovers: there is only one reminder per assignment
            first.write({"summary": summary, "note": note})  # the deadline is not moved: an ignored reminder stays overdue
            return True
        self.activity_schedule(act_type_xmlid=ACTIVITY_TYPE_XMLID, date_deadline=today, user_id=user.id, summary=summary, note=note)
        return True

    def _reminder_texts(self, due):
        """The summary and the note of the reminder (call it on a record that carries the person's language)."""
        self.ensure_one()
        titles = [escape(row.step_id.name) for row in due[:REMINDER_TITLES]]
        items = Markup("").join(Markup("<li>%s</li>") % title for title in titles)
        more = len(due) - len(titles)
        extra = Markup("<p>%s</p>") % _("…y %(count)s más.", count=more) if more > 0 else Markup("")
        note = Markup("<p>%s</p><ul>%s</ul>%s<p>%s</p>") % (
            _("Tus tareas de «%(journey)s» para hoy o atrasadas:", journey=self.journey_id.name), items, extra,
            _("Abrí el chat con Buddy para verlas, tildarlas o preguntarle."))
        summary = _("Onboarding: 1 tarea pendiente") if len(due) == 1 else _("Onboarding: %(count)s tareas pendientes", count=len(due))
        return summary[:120], note


class BuddyJourneyProgress(models.Model):
    _name = "buddy.journey.progress"
    _description = "Avance de una persona en un paso de su recorrido"
    _order = "due_date, id"

    assignment_id = fields.Many2one("buddy.journey.assignment", string="Asignación", required=True, ondelete="cascade", index=True)
    step_id = fields.Many2one("buddy.journey.step", string="Paso", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", string="Persona", related="assignment_id.user_id", store=True, index=True)
    journey_id = fields.Many2one("buddy.journey", string="Recorrido", related="assignment_id.journey_id", store=True, index=True)
    assigner_id = fields.Many2one("res.users", string="Asignado por", related="assignment_id.assigner_id", store=True, index=True)
    is_required = fields.Boolean(string="Obligatorio", related="step_id.is_required", store=True)
    due_date = fields.Date(string="Vence", compute="_compute_due_date", store=True, index=True)
    done = fields.Boolean(string="Hecho", default=False)
    done_date = fields.Date(string="Hecho el", readonly=True)
    # 100 or 0: its average over a group of rows is the percentage completed (pivot and graph of Avance).
    done_pct = fields.Float(string="% completado", compute="_compute_done_pct", store=True, aggregator="avg", digits=(6, 1))
    overdue = fields.Boolean(string="Atrasado", compute="_compute_overdue", search="_search_overdue")
    late_days = fields.Integer(string="Días de atraso", compute="_compute_overdue")

    _sql_constraints = [("assignment_step_unique", "unique(assignment_id, step_id)", "Ese paso ya está en esta asignación.")]

    @api.depends("assignment_id.start_date", "step_id.day_offset")
    def _compute_due_date(self):
        for row in self:
            row.due_date = logic.due_date(row.assignment_id.start_date, row.step_id.day_offset) if row.assignment_id.start_date else False

    @api.depends("done")
    def _compute_done_pct(self):
        for row in self:
            row.done_pct = 100.0 if row.done else 0.0

    @api.depends("done", "due_date", "assignment_id.state", "journey_id.active")
    @api.depends_context("uid")
    def _compute_overdue(self):
        today = fields.Date.context_today(self)
        for row in self:
            row.overdue = bool(not row.done and row.due_date and row.due_date < today
                               and row.assignment_id.state == "active" and row.journey_id.active)
            row.late_days = (today - row.due_date).days if row.overdue else 0

    def _search_overdue(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            raise UserError(_("Operación de búsqueda no admitida."))
        today = fields.Date.context_today(self)
        late = ["&", "&", "&", ("done", "=", False), ("due_date", "<", today), ("assignment_id.state", "=", "active"), ("journey_id.active", "=", True)]
        return late if (operator == "=") == value else ["!"] + late

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("El avance lo crea Buddy al asignar el recorrido."))
        return super().create(vals_list)

    def write(self, vals):
        """A person can only tick or untick their own steps (the record rule says which ones are theirs); the date comes
        from the tick, never from the caller. Administrators and the system may fix anything."""
        privileged = self.env.su or self.env.user.has_group("base.group_system")
        if not privileged:
            if set(vals) - {"done"} or not isinstance(vals.get("done"), bool):
                raise AccessError(_("Del avance solo se puede tildar o destildar un paso."))
            if any(row.user_id != self.env.user or row.assignment_id.state == "cancelled" or not row.journey_id.active for row in self):
                raise AccessError(_("Ese recorrido ya no está disponible para tildar pasos."))
        if "done" in vals:
            vals = dict(vals, done_date=fields.Date.context_today(self) if vals["done"] else False)
        result = super().write(vals)
        if "done" in vals:
            self.mapped("assignment_id")._refresh_state()
        return result


class BuddyJourneyAssign(models.TransientModel):
    _name = "buddy.journey.assign"
    _description = "Asignar un recorrido de onboarding a varias personas"

    journey_id = fields.Many2one("buddy.journey", string="Recorrido", required=True, domain=[("active", "=", True)])
    user_ids = fields.Many2many("res.users", "buddy_journey_assign_user_rel", "wizard_id", "user_id", string="Personas",
                                domain=[("share", "=", False)], required=True)
    start_date = fields.Date(string="Fecha de ingreso", required=True, default=fields.Date.context_today,
                             help="Cuándo empezó (o empieza) a trabajar. El día 0 del recorrido es esta fecha. Puede ser pasada o futura.")

    start_warning = fields.Char(compute="_compute_start_warning")

    @api.depends("start_date")
    def _compute_start_warning(self):
        today = fields.Date.context_today(self)
        for wizard in self:
            days = (today - wizard.start_date).days if wizard.start_date else 0
            if days > logic.LATE_ASSIGNMENT_DAYS:
                wizard.start_warning = _("Esta fecha es de hace %(days)s días: los pasos de esos primeros días van a aparecer como atrasados desde el primer momento.", days=days)
            elif days < 0:
                wizard.start_warning = _("Esta fecha todavía no llegó: hasta ese día las personas no van a ver sus tareas ni a recibir recordatorios.")
            else:
                wizard.start_warning = False

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        # Launched from the list of users: the selected ones come already chosen.
        if "user_ids" in fields_list and self.env.context.get("active_model") == "res.users" and self.env.context.get("active_ids"):
            users = self.env["res.users"].browse(self.env.context["active_ids"]).filtered(lambda user: user.active and not user.share)
            values["user_ids"] = [(6, 0, users.ids[:500])]
        return values

    def action_assign(self):
        self.ensure_one()
        if not is_journey_manager(self.env.user):
            raise AccessError(_("Solo quien administra el onboarding puede asignar recorridos."))
        if not self.user_ids:
            raise UserError(_("Elegí al menos una persona."))
        if len(self.user_ids) > 500:
            raise UserError(_("Asigná de a 500 personas como máximo por vez."))
        Assignment = self.env["buddy.journey.assignment"]
        already = Assignment.sudo().search([("journey_id", "=", self.journey_id.id), ("user_id", "in", self.user_ids.ids)]).mapped("user_id")
        new = self.user_ids - already
        Assignment.create([{"user_id": user.id, "journey_id": self.journey_id.id, "start_date": self.start_date} for user in new])
        if not new:
            message = _("Esas personas ya tenían el recorrido: no se cambió nada.")
        elif already:
            message = _("Se asignó el recorrido a %(count)s persona(s); las otras %(already)s ya lo tenían y no se tocaron.", count=len(new), already=len(already))
        else:
            message = _("Se asignó el recorrido a %(count)s persona(s).", count=len(new))
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": _("Recorrido asignado") if new else _("Sin cambios"), "message": message,
                           "type": "success" if new else "warning", "sticky": False,
                           "next": {"type": "ir.actions.act_window_close"}}}
