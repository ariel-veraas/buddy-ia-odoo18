"""What the widget shows a person about their onboarding («Tus tareas de hoy») and the tick of a step.

Everything here runs with the rights of the person: the record rules of the journey models (security/buddy_security.xml)
are the access control, so the engine never needs `sudo` to read or to tick: a progress row, a step or a document that is
not the person's simply is not found. The public methods are `@api.private`: the only door is the controller
(`/buddy/journey`, `/buddy/journey/step`), which validates the request and the CSRF token first.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

from . import buddy_journey_logic as logic
from .buddy_journey_logic import RateWindow
from .buddy_engine import BuddyBusy, safe_drive_link
from .buddy_text import is_placeholder_email
from .buddy_journey_logic import STAGE_PARAM

_logger = logging.getLogger(__name__)

# A person ticks at most this many steps per minute (kept in this process, like the limit of the reads in the controller:
# a defense for the database against a script, not an accounting; a person ticks a handful of steps a day).
TOGGLES_PER_MINUTE = 30
_TICKS = RateWindow(TOGGLES_PER_MINUTE, 60)
MAX_ASSIGNMENTS_SHOWN = 5


class BuddyJourneyEngine(models.AbstractModel):
    _name = "buddy.journey.engine"
    _description = "Motor de las tareas de onboarding"

    @api.model
    def _today(self):
        """Today for the person (their time zone), never the server's."""
        return fields.Date.context_today(self)

    @api.model
    def _link_of(self, step, documents):
        """The link of a step as `{"kind", "label", "url"}`: a document the person can read, else a safe https address."""
        document = documents.get(step.document_id.id) if step.document_id else None
        if document:
            url = safe_drive_link(document.web_view_link)
            return {"kind": "doc", "label": logic.clean_text(document.name, 120), "url": url}
        url = logic.safe_https_url(step.url or "")
        return {"kind": "url", "label": logic.clean_text(step.url.split("//", 1)[-1].split("/", 1)[0], 80), "url": url} if url else None

    @api.model
    def _contact_of(self, step):
        contact = step.contact_id
        if not contact:
            return None
        # A sample address goes nowhere: the card still says WHO to ask but offers no mail link (same as in the chat).
        email = "" if is_placeholder_email(contact.email) else contact.email
        return {"label": logic.clean_text(contact.label, 80), "name": logic.clean_text(contact.name, 80), "contact": email}

    @api.model
    def _task(self, row, today, documents):
        step = row.step_id
        state = logic.due_state(row.due_date, today, row.done)
        kind = step.kind if step.kind in logic.STEP_KIND_CODES else "task"
        return {
            "id": row.id, "days_until": max((row.due_date - today).days, 0) if state == "upcoming" else 0, "title": logic.clean_text(step.name, logic.TITLE_CHARS), "detail": logic.clean_text(step.detail, logic.DETAIL_CHARS),
            "kind": kind, "state": state, "due": row.due_date.isoformat(), "days_late": max((today - row.due_date).days, 0) if state == "overdue" else 0,
            "required": bool(step.is_required), "done": bool(row.done), "link": self._link_of(step, documents),
            "question": logic.clean_text(step.question, logic.QUESTION_CHARS), "contact": self._contact_of(step),
        }

    @api.model
    @api.private
    def tasks(self):
        """The person's journeys for the widget: the steps due today or late (and the ones ticked today, so a mistaken tick
        can be undone), the next few and the progress. `{"journeys": [...], "pending": N}` where `pending` is the number of
        required steps due today or late (the little dot of the launcher)."""
        self.env["buddy.engine"]._require_internal()
        today = self._today()
        assignments = self.env["buddy.journey.assignment"].search(
            [("user_id", "=", self.env.uid), ("state", "!=", "cancelled"), ("start_date", "<=", today)],
            order="start_date desc, id desc", limit=MAX_ASSIGNMENTS_SHOWN).filtered(lambda assignment: assignment.journey_id.active)
        rows = assignments.mapped("progress_ids").sorted(lambda row: (row.due_date, row.step_id.sequence, row.id))
        # The documents come with the person's own rights: one they cannot read is not found, so it has no link.
        wanted = rows.mapped("step_id.document_id").ids
        documents = {document.id: document for document in self.env["buddy.document"].search([("id", "in", wanted)])} if wanted else {}
        journeys, pending = [], 0
        for assignment in assignments:
            mine = rows.filtered(lambda row: row.assignment_id == assignment)
            counted = mine.filtered(lambda row: row.step_id.is_required) or mine
            late = [row for row in mine if not row.done and row.due_date <= today and row.step_id.is_required]
            horizon = today + timedelta(days=logic.UPCOMING_DAYS)
            current = [row for row in mine if row.due_date <= today and (not row.done or row.done_date == today)]
            upcoming_all = [row for row in mine if not row.done and today < row.due_date <= horizon]
            upcoming = upcoming_all[:logic.UPCOMING_SHOWN]
            pending += len(late)
            journeys.append({
                "name": logic.clean_text(assignment.journey_id.name, logic.TITLE_CHARS),
                "description": logic.clean_text(assignment.journey_id.description, 300),
                "start": assignment.start_date.isoformat(), "day": (today - assignment.start_date).days + 1,
                "percent": logic.percent(len(counted.filtered("done")), len(counted)),
                "done": len(counted.filtered("done")), "total": len(counted), "pending": len(late),
                "state": assignment.state,
                "tasks": [self._task(row, today, documents) for row in current[:logic.MAX_TASKS_SHOWN]],
                "upcoming": [self._task(row, today, documents) for row in upcoming], "upcoming_total": len(upcoming_all),
            })
        return {"journeys": journeys, "pending": pending}

    @api.model
    @api.private
    def set_done(self, progress_id, done):
        """Tick or untick one of the person's own steps and return the updated `tasks()`.

        `done` says what the state must be (not «toggle»), so repeating a request after a lost reply changes nothing. A
        row that is not the person's (or does not exist, or belongs to a cancelled or archived journey) is
        «journey_unknown»: the record rule hides the others' rows, so guessing ids finds nothing.
        """
        self.env["buddy.engine"]._require_internal()
        if type(progress_id) is not int or progress_id < 1 or type(done) is not bool:
            raise BuddyBusy("invalid_request")
        if not _TICKS.allow(self.env.uid):
            raise BuddyBusy("rate_limited")
        Progress = self.env["buddy.journey.progress"]
        row = Progress.search([("id", "=", progress_id)], limit=1)
        if not row or row.assignment_id.state == "cancelled" or not row.journey_id.active:
            raise BuddyBusy("journey_unknown")
        if row.done != done:
            row.write({"done": done})
            # The reminder in the person's Odoo inbox is closed as soon as nothing due is left (they just did it).
            assignment = row.assignment_id.sudo()
            if assignment.state != "cancelled" and assignment._reminders_enabled():
                assignment._reminder_sync(create=False)
        return self.tasks()

    @api.model
    def _stage_note(self):
        """The line that tells the model where the person is in their journey, or an empty text.

        Off unless an administrator turned on «Contexto de etapa» in the settings. It names the journey and the stage («su
        semana 1»), never the person (see docs/SECURITY.md). Any failure is an empty text: the chat must never break for it.
        """
        try:
            if str(self.env["ir.config_parameter"].sudo().get_param(STAGE_PARAM) or "").strip().lower() not in ("1", "true"):
                return ""
            today = self._today()
            assignment = self.env["buddy.journey.assignment"].search(
                [("user_id", "=", self.env.uid), ("state", "=", "active"), ("start_date", "<=", today)],
                order="start_date desc, id desc", limit=1)
            if not assignment or not assignment.journey_id.active:
                return ""
            return logic.stage_note(assignment.journey_id.name, assignment.start_date, today)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Buddy onboarding: no pude armar el contexto de etapa (%s)", type(exc).__name__)
            return ""
