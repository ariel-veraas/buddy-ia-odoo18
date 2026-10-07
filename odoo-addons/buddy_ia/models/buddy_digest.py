"""The weekly summary «Qué falta documentar» for whoever looks after Buddy (E4b): what to write, fix or review in the documents.

Made once a week by a cron WITHOUT AI (counting, and a small grouping by words in common: see `buddy_digest_logic.py`), saved as a
row of this model and announced with an Odoo activity (never an e-mail). Six sections, each one with a plain recommendation:

1. questions nobody could answer, grouped by topic (the text only when `buddy.insight` already keeps it);
2. documents with the most 👎 votes;
3. documents cited in doubtful or contradictory answers;
4. documents nothing cited in the last 90 days (to check they are still valid);
5. folders with errors or files that were read only in part (administrators only);
6. onboarding steps people are late with, as figures (administrators only, and only for journeys with enough people).

Privacy is the point of this model, with the same rules as `buddy.insight` and `buddy.feedback`:

* Nothing here names a person: the sources are anonymous rows and aggregates. The only text that comes from people is a few
  masked examples of unanswered questions (the text `buddy.insight` already keeps, with e-mails, links and long numbers hidden).
* What is STORED is ids and counts (`data`, readable by administrators only, and guarded so nobody else can search by it
  either). The titles are read when the summary is OPENED, with the rights of whoever opens it (the same record rules as the
  documents and the folders): a manager never learns the title of a document of a folder they cannot read.
* The statistics a manager cannot see (status of a folder, onboarding progress) are sections for administrators only.
* Retention: the same as the statistics (the daily cron), and «Borrar estadísticas» deletes the summaries too.

Rows are written by the cron (or by an administrator who asks for one now) as the superuser; no access right allows creating or
changing them through the interface.
"""
import logging
import time
from datetime import timedelta

from markupsafe import Markup

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import format_date

from . import buddy_digest_logic as logic
from . import buddy_proactive_logic as proactive
from .buddy_insight import DEFAULT_RETENTION_DAYS, INSIGHT_DAYS_PARAM, MAX_RETENTION_DAYS
from .buddy_text import clean_title

_logger = logging.getLogger(__name__)

ACTIVITY_TYPE_XMLID = "buddy_ia.activity_type_digest"
MAX_RECIPIENTS = 20
PURGE_BATCH = 200
# The activity says «there is a new summary»: due in a week, when the next one comes (it never starts overdue and nagging).
ACTIVITY_DAYS = 7
UNUSED_STORED = 60  # ids of unused documents kept (the person who opens it sees the first UNUSED_LISTED they may read)
ITEMS_STORED = 20  # ids of the lists of documents kept (each person sees at most TOP_DOCUMENTS they may read)


class BuddyDigest(models.Model):
    _name = "buddy.digest"
    _inherit = ["buddy.guard", "mail.thread", "mail.activity.mixin"]
    _description = "Resumen semanal: qué falta documentar"
    _order = "date_to desc, id desc"
    # `data` holds titles of journeys and the like; the status of folders and the onboarding figures are the administrators'.
    # Odoo refuses to READ a field with `groups=` but lets anybody SEARCH or GROUP BY it: the guard closes that too.
    _admin_only_fields = ("data", "unused_count", "problem_count", "late_count")

    date_from = fields.Date(string="Desde", required=True, readonly=True, help="El primer día de la semana que cuenta el resumen.")
    date_to = fields.Date(string="Hasta", required=True, readonly=True, index=True,
                          help="El día siguiente al último que cuenta el resumen. El de cada lunes termina el domingo; el que pide un administrador, hoy.")
    date_last = fields.Date(string="Hasta (inclusive)", compute="_compute_date_last", help="El último día que cuenta el resumen.")
    manual = fields.Boolean(string="A pedido", readonly=True,
                            help="Lo generó un administrador con «Generar el resumen ahora» (cuenta hasta hoy). Los de cada lunes los arma Buddy solo.")
    data = fields.Text(string="Datos", readonly=True, groups="base.group_system", prefetch=False)
    partial = fields.Boolean(string="Incompleto", readonly=True,
                             help="Alguna parte no se pudo calcular esta vez (tardó demasiado). Se vuelve a intentar la semana que viene.")
    unanswered_count = fields.Integer(string="Preguntas sin respuesta", readonly=True)
    downvote_count = fields.Integer(string="Votos 👎", readonly=True)
    doubtful_count = fields.Integer(string="Respuestas dudosas", readonly=True)
    # How many documents nobody cited counts ALL the documents, also those of folders a manager cannot read: administrators only.
    unused_count = fields.Integer(string="Documentos sin uso", readonly=True, groups="base.group_system")
    problem_count = fields.Integer(string="Carpetas con problemas", readonly=True, groups="base.group_system")
    late_count = fields.Integer(string="Personas atrasadas en onboarding", readonly=True, groups="base.group_system")
    report = fields.Html(string="Resumen", compute="_compute_report", sanitize=True)

    _sql_constraints = [("date_to_manual_unique", "unique(date_to, manual)", "Ya hay un resumen para ese día.")]

    @api.depends("date_to")
    def _compute_date_last(self):
        for record in self:
            record.date_last = record.date_to - timedelta(days=1) if record.date_to else False

    @api.depends("date_from", "date_to")
    @api.depends_context("lang")
    def _compute_display_name(self):
        for record in self:
            if record.date_from and record.date_to:
                record.display_name = _("Resumen semanal: %(start)s al %(end)s", start=format_date(self.env, record.date_from),
                                        end=format_date(self.env, record.date_to - timedelta(days=1)))
            else:
                record.display_name = _("Resumen semanal")

    def _message_subscribe(self, partner_ids=None, subtype_ids=None, customer_ids=None):
        """Nobody follows a summary: the activity is the notice and a follower would only add an inbox full of nothing."""
        return self.env["mail.followers"]

    # ------------------------------------------------------------------ settings
    @api.model
    def _enabled(self):
        return proactive.switch_on(self.env["ir.config_parameter"].sudo().get_param(proactive.DIGEST_PARAM))

    @api.model
    def _retention_days(self):
        try:
            days = int(self.env["ir.config_parameter"].sudo().get_param(INSIGHT_DAYS_PARAM) or DEFAULT_RETENTION_DAYS)
        except ValueError:
            days = DEFAULT_RETENTION_DAYS
        return min(max(days, 1), MAX_RETENTION_DAYS)

    # ------------------------------------------------------------------ the weekly run
    @api.model
    def _cron_weekly(self):
        """Once a week: make the summary of the week that ended yesterday (one per day at most) and tell the people chosen in the
        settings. Does nothing when the administrator turned it off. Returns the new summary (or an empty recordset)."""
        if not self._enabled():
            return self.browse()
        digest, created = self._generate(fields.Date.today())
        if created:
            self._notify(digest)
            return digest
        return self.browse()

    @api.model
    def _generate(self, today=None, manual=False):
        """`(summary, created)`. The weekly one (`manual` false, the cron) is the week that ends the day before `today`: when one exists
        for that day it is returned as it is, so the cron running twice does not duplicate. The one an administrator asks for
        (`manual`, the button «Generar ahora») counts the last seven days INCLUDING today and rewrites the one of today. They are
        different rows: asking for one never takes the place of the weekly one. Never raises for a section: one that fails or does not
        fit the time budget is left out and the summary says so."""
        self = self.with_user(SUPERUSER_ID)
        today = today or fields.Date.today()
        date_from, date_to = logic.manual_window(today) if manual else logic.week_window(today)
        existing = self.search([("date_to", "=", date_to), ("manual", "=", bool(manual))], limit=1)
        if existing and not manual:
            return existing, False
        deadline = time.monotonic() + logic.BUDGET_SECONDS
        self.env.flush_all()  # the sections read with plain SQL: whatever is pending in the ORM must be in the database first
        sections, partial = {}, []
        for name in logic.SECTIONS:
            if time.monotonic() > deadline:
                partial.append(name)
                continue
            try:
                # The savepoint is INSIDE the timeout: when a query fails the savepoint is undone first, and only then the
                # setting is put back (in an aborted transaction nothing could be executed).
                with proactive.statement_timeout(self.env.cr, logic.STATEMENT_TIMEOUT_MS):
                    with self.env.cr.savepoint():
                        sections[name] = getattr(self, "_section_" + name)(date_from, date_to)
            except Exception as exc:  # noqa: BLE001 - one section never costs the rest
                _logger.warning("Buddy: el resumen semanal no pudo calcular «%s» (%s)", name, type(exc).__name__)
                partial.append(name)
        data = dict(sections, window={"from": date_from.isoformat(), "to": date_to.isoformat()}, partial=partial)
        values = {
            "date_from": date_from, "date_to": date_to, "manual": bool(manual), "partial": bool(partial), "data": logic.dump_data(data),
            "unanswered_count": logic.count(sections.get("unanswered", {}).get("total")),
            "downvote_count": logic.count(sections.get("downvotes", {}).get("down")),
            "doubtful_count": logic.count(sections.get("doubtful", {}).get("total")),
            "unused_count": logic.count(sections.get("unused", {}).get("total")),
            "problem_count": logic.count(sections.get("folders", {}).get("total")),
            "late_count": sum(logic.count(item.get("late_people")) for item in sections.get("onboarding", {}).get("items", [])),
        }
        if existing:
            existing.write(values)
            return existing, False
        # No «created» line, no tracking and no follower in a chatter nobody reads: the activity is the only notice.
        return self.with_context(mail_create_nolog=True, mail_notrack=True, mail_create_nosubscribe=True).create(values), True

    # ------------------------------------------------------------------ the sections (ids and counts only)
    @api.model
    def _datetimes(self, date_from, date_to):
        return fields.Datetime.to_datetime(date_from), fields.Datetime.to_datetime(date_to)

    @api.model
    def _section_unanswered(self, date_from, date_to):
        """Questions the buddy could not answer in the week, grouped by the words they share."""
        start, end = self._datetimes(date_from, date_to)
        insight = self.env["buddy.insight"].sudo()
        domain = [("needs_human", "=", True), ("error", "=", False), ("create_date", ">=", start), ("create_date", "<", end)]
        rows = insight.search_read(domain, ["question"], limit=logic.QUESTIONS_READ, order="id desc")
        questions = [row["question"] or "" for row in rows]
        groups, ungrouped = logic.cluster_questions(questions)
        return {
            "total": insight.search_count(domain), "read": len(rows), "ungrouped": ungrouped,
            "without_text": sum(1 for question in questions if not question.strip()),
            "groups": [{"terms": group["terms"], "count": group["count"], "examples": logic.example_questions(questions, group["members"])}
                       for group in groups],
        }

    @api.model
    def _section_downvotes(self, date_from, date_to):
        """The documents with the most 👎 of the week. A vote only has documents when enough people can read them, and the
        votes of today are never counted, whatever the day the summary stops at (the same rule that hides them from the managers
        until the day is over: two summaries made hours apart must not show when a vote was given)."""
        cr = self.env.cr
        date_to = min(date_to, fields.Date.today())
        cr.execute("SELECT value, COUNT(*) FROM buddy_feedback WHERE day >= %s AND day < %s GROUP BY value", (date_from, date_to))
        totals = dict(cr.fetchall())
        cr.execute(
            """SELECT rel.document_id, feedback.value, COUNT(*)
                 FROM buddy_feedback feedback
                 JOIN buddy_feedback_document_rel rel ON rel.feedback_id = feedback.id
                WHERE feedback.day >= %s AND feedback.day < %s
                GROUP BY rel.document_id, feedback.value""", (date_from, date_to))
        votes = {}
        for document_id, value, amount in cr.fetchall():
            votes.setdefault(document_id, {"up": 0, "down": 0})[value] = amount
        ranked = sorted(((document_id, item) for document_id, item in votes.items() if item["down"]),
                        key=lambda pair: (-pair[1]["down"], pair[1]["up"], pair[0]))
        return {"down": totals.get("down", 0), "up": totals.get("up", 0),
                "items": [{"doc": document_id, "down": item["down"], "up": item["up"]} for document_id, item in ranked[:ITEMS_STORED]]}

    @api.model
    def _section_doubtful(self, date_from, date_to):
        """The documents cited in the answers that were doubtful (weak evidence) or whose documents contradicted each other."""
        start, end = self._datetimes(date_from, date_to)
        insight = self.env["buddy.insight"].sudo()
        window = [("create_date", ">=", start), ("create_date", "<", end)]
        doubtful = ["|", ("conflict", "=", True), ("confidence", "=", "low")]
        cr = self.env.cr
        cr.execute(
            """SELECT rel.document_id, COUNT(DISTINCT insight.id), COUNT(DISTINCT insight.id) FILTER (WHERE insight.conflict)
                 FROM buddy_insight insight
                 JOIN buddy_insight_document_rel rel ON rel.insight_id = insight.id
                WHERE insight.create_date >= %s AND insight.create_date < %s AND (insight.conflict OR insight.confidence = 'low')
                GROUP BY rel.document_id
                ORDER BY 2 DESC, 3 DESC, rel.document_id
                LIMIT %s""", (start, end, ITEMS_STORED))
        rows = cr.fetchall()  # right away: the counts below use the same cursor
        return {"total": insight.search_count(window + doubtful), "conflicts": insight.search_count(window + [("conflict", "=", True)]),
                "items": [{"doc": document_id, "count": count, "conflicts": conflicts} for document_id, count, conflicts in rows]}

    @api.model
    def _section_unused(self, date_from, date_to):
        """Documents of the active folders that no answer cited in the last 90 days (or in all the history there is, when the
        statistics are younger or are kept for less): candidates to check that they are still valid. Not judged on less than 30 days."""
        cr = self.env.cr
        retention = self._retention_days()
        cr.execute("SELECT MIN(create_date) FROM buddy_insight")
        oldest = cr.fetchone()[0]
        now = fields.Datetime.now()
        history = (now - oldest).days if oldest else 0
        days = logic.unused_window(history, retention)
        result = {"days": days, "history_days": history, "retention_days": retention, "total": 0, "doc_ids": []}
        if not days:
            return result
        cutoff = now - timedelta(days=days)
        cr.execute(
            """SELECT document.id, COUNT(*) OVER ()
                 FROM buddy_document document
                 JOIN buddy_source source ON source.id = document.source_id
                WHERE source.active AND document.state = 'ready' AND document.create_date < %(cutoff)s
                  AND NOT EXISTS (SELECT 1 FROM buddy_insight_document_rel rel
                                    JOIN buddy_insight insight ON insight.id = rel.insight_id
                                   WHERE rel.document_id = document.id AND insight.create_date >= %(cutoff)s)
                ORDER BY document.modified_time NULLS LAST, document.id
                LIMIT %(limit)s""", {"cutoff": cutoff, "limit": logic.UNUSED_CANDIDATES})
        rows = cr.fetchall()
        result.update(total=rows[0][1] if rows else 0, doc_ids=[row[0] for row in rows[:UNUSED_STORED]])
        return result

    @api.model
    def _section_folders(self, date_from, date_to):
        """Active folders whose last synchronization failed, or with files that could not be read or were read only in part."""
        sources = self.env["buddy.source"].sudo().search([("active", "=", True)])
        documents = self.env["buddy.document"].sudo()

        def counted(domain):
            return {source.id: amount for source, amount in documents._read_group([("source_id", "in", sources.ids)] + domain, ["source_id"], ["__count"])}

        failed = counted([("state", "=", "error")])
        empty = counted([("state", "=", "empty")])
        truncated = counted([("state", "=", "ready"), ("error", "!=", False)])
        items = []
        for source in sources:
            item = {"source": source.id, "sync_error": source.state == "error", "error_docs": failed.get(source.id, 0),
                    "empty_docs": empty.get(source.id, 0), "truncated_docs": truncated.get(source.id, 0)}
            if item["sync_error"] or item["error_docs"] or item["empty_docs"] or item["truncated_docs"]:
                items.append(item)
        items.sort(key=lambda item: (not item["sync_error"], -item["error_docs"], -item["truncated_docs"], -item["empty_docs"], item["source"]))
        return {"total": len(items), "items": items[: logic.MAX_FOLDERS]}

    @api.model
    def _section_onboarding(self, date_from, date_to):
        """How many people are late with required steps, per journey followed by at least MIN_PEOPLE people, and the steps they are
        late with (named only when at least STEP_MIN_LATE people are). Figures only: never who."""
        assignments = self.env["buddy.journey.assignment"].sudo()
        progress = self.env["buddy.journey.progress"].sudo()
        rows = assignments._read_group([("state", "=", "active"), ("journey_id.active", "=", True)], ["journey_id"], ["__count", "progress_pct:avg"])
        big = sorted(((journey, people, average) for journey, people, average in rows if people >= logic.MIN_PEOPLE),
                     key=lambda row: (-row[1], row[0].id))[: logic.MAX_JOURNEYS]
        items = []
        for journey, people, average in big:
            late_domain = [("journey_id", "=", journey.id), ("overdue", "=", True), ("is_required", "=", True)]
            late_people = len(progress._read_group(late_domain, ["assignment_id"], ["__count"]))
            steps = sorted(((step, amount) for step, amount in progress._read_group(late_domain, ["step_id"], ["__count"]) if amount >= logic.STEP_MIN_LATE),
                           key=lambda pair: (-pair[1], pair[0].id))[: logic.TOP_STEPS]
            items.append({"journey": journey.id, "people": people, "late_people": late_people, "avg_pct": int(round(average or 0)),
                          "steps": [{"step": step.id, "late": amount} for step, amount in steps]})
        return {"items": items, "small": len(rows) - len(big), "journeys": len(rows)}

    # ------------------------------------------------------------------ the notice (an Odoo activity, never an e-mail)
    @api.model
    def _recipients(self):
        """The people the activity goes to: the person who looks after the tickets when the settings say so (and can open the
        summary), else the administrators (at most MAX_RECIPIENTS). Nobody when the administrator chose that."""
        mode = proactive.notify_mode(self.env["ir.config_parameter"].sudo().get_param(proactive.DIGEST_NOTIFY_PARAM))
        users = self.env["res.users"].sudo()
        if mode == "none":
            return users.browse()
        if mode == "responsible":
            try:
                user = users.browse(int(self.env["ir.config_parameter"].sudo().get_param("buddy_ia.ticket_user_id") or 0)).exists()
            except ValueError:
                user = users.browse()
            # Somebody who cannot open the summary would get a notice that leads nowhere.
            if user and user.active and not user.share and (user.has_group("buddy_ia.group_buddy_manager") or user.has_group("base.group_system")):
                return user
        return users.search([("groups_id", "in", self.env.ref("base.group_system").ids), ("active", "=", True), ("share", "=", False)],
                            order="id", limit=MAX_RECIPIENTS)

    @api.model
    def _notify(self, digest):
        """One activity per chosen person that says there is a new summary, in their language and with NO e-mail
        (`mail_activity_quick_update`: Odoo's own «assigned to you» notification is skipped; the activity shows in their bell and
        in the summary). The unread ones of earlier weeks are removed: there is only ever one open notice. Returns how many."""
        recipients = self._recipients()
        activity_type = self.env.ref(ACTIVITY_TYPE_XMLID, raise_if_not_found=False)
        if not recipients or not activity_type:
            return 0
        self.env["mail.activity"].sudo().search([
            ("res_model", "=", self._name), ("activity_type_id", "=", activity_type.id), ("res_id", "!=", digest.id)]).unlink()
        deadline = fields.Date.context_today(self) + timedelta(days=ACTIVITY_DAYS)
        count = 0
        for user in recipients:
            try:
                with self.env.cr.savepoint():
                    record = digest.with_user(SUPERUSER_ID).with_context(lang=user.lang or self.env.lang, mail_activity_quick_update=True)
                    summary, note = record._notice_texts()
                    record.activity_schedule(act_type_xmlid=ACTIVITY_TYPE_XMLID, date_deadline=deadline, user_id=user.id, summary=summary, note=note)
                count += 1
            except Exception as exc:  # noqa: BLE001 - one person never stops the rest
                _logger.warning("Buddy: no pude avisar del resumen semanal al usuario %s (%s)", user.id, type(exc).__name__)
        return count

    def _notice_texts(self):
        """The summary and the note of the activity (call it on a record that carries the language of the person)."""
        self.ensure_one()
        summary = _("Resumen semanal de Buddy: qué falta documentar")
        note = Markup("<p>%s</p>") % _("Ya está el resumen de la semana del %(start)s al %(end)s. Abrilo para ver qué conviene documentar, corregir o revisar.",
                                       start=format_date(self.env, self.date_from), end=format_date(self.env, self.date_to - timedelta(days=1)))
        return summary, note

    @api.model
    def _clear_notices(self):
        """Remove the open «there is a new summary» activities (the administrator turned the weekly summary off)."""
        activity_type = self.env.ref(ACTIVITY_TYPE_XMLID, raise_if_not_found=False)
        if activity_type:
            self.env["mail.activity"].sudo().search([("res_model", "=", self._name), ("activity_type_id", "=", activity_type.id)]).unlink()

    # ------------------------------------------------------------------ retention
    @api.model
    def _cron_purge(self, days=None):
        """Delete the summaries older than the retention period of the statistics (the same setting, in batches)."""
        days = self._retention_days() if days is None else min(max(int(days), 1), MAX_RETENTION_DAYS)
        # By the FIRST day of the week it counts (the whole week has to be past the limit): the examples of questions in it were
        # written up to a week before the summary was made, and must not outlive the statistics they come from.
        cutoff = fields.Date.today() - timedelta(days=days)
        while True:
            old = self.sudo().search([("date_from", "<", cutoff)], limit=PURGE_BATCH)
            if not old:
                return
            old.unlink()

    @api.model
    def _delete_everything(self):
        """Administrator action (privacy): erase every summary (they are made from the statistics and the stored questions)."""
        count = 0
        while True:
            rows = self.sudo().search([], limit=PURGE_BATCH)
            if not rows:
                return count
            count += len(rows)
            rows.unlink()

    # ------------------------------------------------------------------ what the person who opens it sees
    @api.depends("data", "partial")
    @api.depends_context("uid", "lang")
    def _compute_report(self):
        admin = self.env.user.has_group("base.group_system")
        for record in self:
            record.report = record._render(admin)

    def _titles(self, ids, limit=80):
        """`{document id: title}` of the documents among `ids` that the PERSON WHO OPENS THE SUMMARY may read (their own record rules)."""
        documents = self.env["buddy.document"].search([("id", "in", logic.int_list(ids, 500))])
        return {document.id: clean_title(document.name, limit) for document in documents}

    @staticmethod
    def _lines(items):
        return Markup("<ul>%s</ul>") % Markup("").join(items) if items else Markup("")

    def _section(self, title, *blocks):
        return Markup('<h2 class="h4 mt-4">%s</h2>%s') % (title, Markup("").join(block for block in blocks if block))

    def _advice(self, text):
        return Markup('<div class="alert alert-info" role="note"><strong>%s</strong> %s</div>') % (_("Qué hacer:"), text) if text else Markup("")

    def _muted(self, text):
        return Markup('<p class="text-muted">%s</p>') % text if text else Markup("")

    def _render(self, admin):
        """The summary as HTML for the person who opens it: first the overview in one line, then only the sections that have something
        to say (the rest are named together in one last line), each with what to do. Everything comes from the stored ids and counts
        plus text written here; the titles are read now, with the rights of whoever is looking. Sections for administrators are left
        out for the rest."""
        self.ensure_one()
        data = logic.load_data(self.sudo().data)
        if not data:
            return Markup("<p>%s</p>") % _("Este resumen está vacío.")
        blocks = []
        partial = [name for name in (data.get("partial") if isinstance(data.get("partial"), list) else []) if name in logic.SECTIONS
                   and (admin or name not in logic.ADMIN_SECTIONS)]
        if partial:
            labels = self._section_labels()
            blocks.append(Markup('<div class="alert alert-warning">%s</div>') % _(
                "Algunas partes no se pudieron calcular esta vez (%(sections)s). Se vuelve a intentar la semana que viene, o un administrador puede "
                "volver a armarlo desde Ajustes → Buddy IA.", sections=", ".join(labels[name] for name in partial)))
        shown, quiet, notes = [], [], []
        for name in logic.RENDER_ORDER:
            if name in data and (admin or name not in logic.ADMIN_SECTIONS) and isinstance(data[name], dict):
                title, markup, label, note = getattr(self, "_render_" + name)(data[name])
                if markup:
                    shown.append((title, markup))
                elif label:
                    quiet.append(label)
                if note:
                    notes.append(note)
        blocks.append(self._overview(data, admin, [title for title, markup in shown]))
        blocks.extend(markup for title, markup in shown)
        if quiet:
            blocks.append(self._muted(_("Sin novedades en: %(sections)s.", sections=", ".join(quiet))))
        blocks.extend(self._muted(note) for note in notes)
        blocks.append(self._muted(_("Este resumen se arma sin inteligencia artificial, contando consultas anónimas: nadie sabe quién preguntó ni quién votó.")))
        if not admin:
            blocks.append(self._muted(_("Las carpetas con problemas y el avance de onboarding los ven solo los administradores.")))
        return Markup("").join(blocks)

    def _section_labels(self):
        return {"unanswered": _("preguntas sin respuesta"), "downvotes": _("votos 👎"), "doubtful": _("respuestas dudosas"),
                "unused": _("documentos sin uso"), "folders": _("carpetas con problemas"), "onboarding": _("onboarding atrasado")}

    def _unused_total(self, section, admin):
        """How many unused documents THIS person is told about: all of them for an administrator; for the rest only those they may read
        (among the oldest ones kept). The total of every folder would tell a manager how many documents sit in folders they cannot see."""
        if not isinstance(section, dict) or not logic.count(section.get("days")):
            return 0
        return logic.count(section.get("total")) if admin else len(self._titles(section.get("doc_ids")))

    def _overview(self, data, admin, titles):
        """One line with the figures of the week and where to start (what the person reads first)."""
        def figure(name, key, one, many):
            amount = logic.count((data.get(name) or {}).get(key)) if isinstance(data.get(name), dict) else 0
            return (one if amount == 1 else many % {"count": amount}) if amount else ""

        late = sum(logic.count(item.get("late_people")) for item in ((data.get("onboarding") or {}).get("items") or []) if isinstance(item, dict)) \
            if admin and isinstance(data.get("onboarding"), dict) else 0
        unused = self._unused_total(data.get("unused"), admin)
        parts = [
            figure("unanswered", "total", _("1 pregunta sin respuesta"), _("%(count)s preguntas sin respuesta")),
            figure("downvotes", "down", _("1 voto 👎"), _("%(count)s votos 👎")),
            figure("doubtful", "total", _("1 respuesta dudosa"), _("%(count)s respuestas dudosas")),
            (_("1 documento sin uso") if unused == 1 else _("%(count)s documentos sin uso", count=unused)) if unused else "",
            figure("folders", "total", _("1 carpeta con problemas"), _("%(count)s carpetas con problemas")) if admin else "",
            (_("1 persona atrasada en onboarding") if late == 1 else _("%(count)s personas atrasadas en onboarding", count=late)) if late else "",
        ]
        parts = [part for part in parts if part]
        if not parts:
            return Markup("<p><strong>%s</strong></p>") % _("Esta semana no hay nada que documentar ni corregir.")
        text = Markup("<p><strong>%s</strong> %s</p>") % (_("Esta semana:"), " · ".join(parts))
        if len(titles) > 1:
            text += Markup("<p>%s</p>") % _("Empezá por «%(section)s».", section=titles[0])
        return text

    def _topic(self, terms):
        """The words that name a group of questions as a phrase: «backup», «backup y restaurar», «backup, restaurar y copia»."""
        terms = [logic.mask_personal(term, 40) for term in (terms if isinstance(terms, list) else [])[: logic.TERMS_PER_GROUP] if isinstance(term, str)]
        terms = [term for term in terms if term]
        if len(terms) > 1:
            return _("%(first)s y %(last)s", first=", ".join(terms[:-1]), last=terms[-1])
        return terms[0] if terms else ""

    def _render_unanswered(self, section):
        title = _("Preguntas sin respuesta")
        total = logic.count(section.get("total"))
        if not total:
            return title, "", _("preguntas sin respuesta"), ""
        items, top = [], ""
        for group in (section.get("groups") if isinstance(section.get("groups"), list) else [])[: logic.MAX_GROUPS]:
            if not isinstance(group, dict):
                continue
            topic = self._topic(group.get("terms"))
            if not topic:
                continue
            top = top or topic
            amount = logic.count(group.get("count"))
            examples = [logic.mask_personal(example, logic.EXAMPLE_CHARS) for example in (group.get("examples") if isinstance(group.get("examples"), list) else [])
                        [: logic.EXAMPLES_PER_GROUP] if isinstance(example, str)]
            items.append(Markup("<li><strong>%s</strong> — %s%s</li>") % (
                topic, _("1 pregunta") if amount == 1 else _("%(count)s preguntas", count=amount),
                Markup("").join(Markup("<br/><em class='text-muted'>«%s»</em>") % example for example in examples if example)))
        lead = Markup("<p>%s</p>") % (_("1 pregunta no tuvo respuesta esta semana.") if total == 1 else _("%(count)s preguntas no tuvieron respuesta esta semana.", count=total))
        extra = []
        ungrouped, without_text = logic.count(section.get("ungrouped")), logic.count(section.get("without_text"))
        if ungrouped:
            extra.append(Markup("<p>%s</p>") % (_("Otra pregunta suelta, sin un tema en común.") if ungrouped == 1
                                                else _("Otras %(count)s preguntas sueltas, sin un tema en común.", count=ungrouped)))
        if without_text:
            extra.append(Markup("<p>%s</p>") % (_("En una de ellas no se guardó el texto, así que no se pudo agrupar.") if without_text == 1
                                                else _("En %(count)s de ellas no se guardó el texto, así que no se pudieron agrupar.", count=without_text)))
        advice = (_("Documentá cómo se hace «%(topic)s»: es lo que más se preguntó sin que haya una respuesta. Escribilo en un documento de la carpeta "
                    "de Drive que corresponda y, en la próxima sincronización, Buddy ya lo va a usar.", topic=top)
                  if top else _("Mirá las preguntas una por una en Buddy → Uso y preguntas → Preguntas sin respuesta y documentá lo que se repita."))
        return title, self._section(title, lead, self._lines(items), *extra, self._advice(advice)), "", ""

    def _render_downvotes(self, section):
        title = _("Documentos con más votos 👎")
        down, up = logic.count(section.get("down")), logic.count(section.get("up"))
        if not down:
            return title, "", _("votos 👎"), ""
        items_raw = [item for item in (section.get("items") if isinstance(section.get("items"), list) else []) if isinstance(item, dict)]
        names = self._titles([item.get("doc") for item in items_raw])
        items, top = [], ""
        for item in items_raw:
            name = names.get(item.get("doc"))
            if name and len(items) < logic.TOP_DOCUMENTS:
                top = top or name
                items.append(Markup("<li><strong>%s</strong> — %s</li>") % (name, _("%(down)s 👎 · %(up)s 👍", down=logic.count(item.get("down")), up=logic.count(item.get("up")))))
        lead = Markup("<p>%s</p>") % _("Votos de la semana: %(up)s 👍 y %(down)s 👎.", up=up, down=down)
        if items:
            advice = _("Revisá «%(title)s». Fijate que esté actualizado, que responda lo que la gente pregunta y que no le falte ningún paso.", title=top)
        else:
            # The same words whether the votes are about documents this person cannot read or carry no document at all: telling them
            # apart would say that there are votes on documents of folders they cannot see.
            advice = _("No hay documentos para nombrar entre los votos 👎: no traen documento (cuando la carpeta la lee poca gente, así nadie queda identificado) o son de carpetas que no podés ver. "
                       "Mirá los motivos en Buddy → Uso y preguntas → Calidad de respuestas.")
        return title, self._section(title, lead, self._lines(items), self._advice(advice)), "", ""

    def _render_doubtful(self, section):
        title = _("Respuestas dudosas o contradictorias")
        total, conflicts = logic.count(section.get("total")), logic.count(section.get("conflicts"))
        if not total:
            return title, "", _("respuestas dudosas"), ""
        items_raw = [item for item in (section.get("items") if isinstance(section.get("items"), list) else []) if isinstance(item, dict)]
        names = self._titles([item.get("doc") for item in items_raw])
        items, top = [], ""
        for item in items_raw:
            name = names.get(item.get("doc"))
            if name and len(items) < logic.TOP_DOCUMENTS:
                top = top or name
                items.append(Markup("<li><strong>%s</strong> — %s</li>") % (name, _("dudosas: %(count)s · contradictorias: %(conflicts)s",
                                                                                     count=logic.count(item.get("count")), conflicts=logic.count(item.get("conflicts")))))
        text = _("1 respuesta salió dudosa esta semana.") if total == 1 else _("%(total)s respuestas salieron dudosas esta semana.", total=total)
        if conflicts:
            text += " " + (_("En 1 los documentos se contradecían.") if conflicts == 1 else _("En %(conflicts)s los documentos se contradecían.", conflicts=conflicts))
        if items:
            advice = _("Revisá «%(title)s»: Buddy encontró información distinta o poco respaldo. Unificá lo que dicen los documentos o completá el dato que falta.", title=top)
        else:
            advice = _("Mirá las respuestas dudosas en Buddy → Uso y preguntas → Respuestas dudosas y unificá o completá los documentos de los que hablan.")
        return title, self._section(title, Markup("<p>%s</p>") % text, self._lines(items), self._advice(advice)), "", ""

    def _render_unused(self, section):
        title = _("Documentos sin uso")
        days = logic.count(section.get("days"))
        label = _("documentos sin uso")
        if not days:
            have, keep = logic.count(section.get("history_days")), logic.count(section.get("retention_days"))
            if keep < logic.MIN_UNUSED_DAYS:
                note = _("Documentos sin uso: las estadísticas se conservan solo %(keep)s días y hacen falta al menos %(minimum)s para saber qué documentos no se citan. "
                         "Subí «Conservar estadísticas» en Ajustes → Buddy IA.", keep=keep, minimum=logic.MIN_UNUSED_DAYS)
            else:
                note = _("Documentos sin uso: Buddy lleva %(have)s días juntando datos y necesita al menos %(minimum)s para decir qué documentos no se citan. Más adelante lo vas a ver acá.",
                         have=have, minimum=logic.MIN_UNUSED_DAYS)
            return title, "", "", note
        total = self._unused_total(section, self.env.user.has_group("base.group_system"))
        if not total:
            return title, "", label, ""
        names = self._titles(section.get("doc_ids"))
        order = logic.int_list(section.get("doc_ids"), UNUSED_STORED)
        listed = [names[document_id] for document_id in order if document_id in names][: logic.UNUSED_LISTED]
        items = [Markup("<li>%s</li>") % name for name in listed]
        lead = Markup("<p>%s</p>") % (_("1 documento no se citó en ninguna respuesta en los últimos %(days)s días.", days=days) if total == 1
                                      else _("%(total)s documentos no se citaron en ninguna respuesta en los últimos %(days)s días.", total=total, days=days))
        note = self._muted(_("Los más viejos primero; solo se nombran los que tenés permiso para ver. Mostrando %(shown)s de %(total)s.", shown=len(listed), total=total)) if items else Markup("")
        advice = _("Fijate si siguen vigentes. Si ya no sirven, sacalos de la carpeta de Drive; si sirven, revisá que el título diga de qué trata para que Buddy los encuentre.")
        return title, self._section(title, lead, self._lines(items), note, self._advice(advice)), "", ""

    def _render_folders(self, section):
        title = _("Carpetas con problemas")
        items_raw = [item for item in (section.get("items") if isinstance(section.get("items"), list) else []) if isinstance(item, dict)]
        if not items_raw:
            return title, "", _("carpetas"), ""
        sources = {source.id: source.name for source in self.env["buddy.source"].search([("id", "in", logic.int_list([item.get("source") for item in items_raw], 100))])}
        items = []
        for item in items_raw:
            name = sources.get(item.get("source"))
            if not name:
                continue
            problems = []
            if item.get("sync_error") is True:
                problems.append(_("la última sincronización falló"))
            if logic.count(item.get("error_docs")):
                problems.append(_("archivos con error: %(count)s", count=logic.count(item.get("error_docs"))))
            if logic.count(item.get("truncated_docs")):
                problems.append(_("archivos leídos solo en parte: %(count)s", count=logic.count(item.get("truncated_docs"))))
            if logic.count(item.get("empty_docs")):
                problems.append(_("archivos sin texto o de un tipo que Buddy no lee: %(count)s", count=logic.count(item.get("empty_docs"))))
            items.append(Markup("<li><strong>%s</strong>: %s</li>") % (clean_title(name, 80), "; ".join(problems)))
        advice = _("Abrí Buddy → Carpetas de Drive y mirá el detalle de cada una. Los archivos escaneados, protegidos con clave o muy grandes pueden no leerse: "
                   "reemplazalos por una versión con texto.")
        lead = Markup("<p>%s</p>") % _("Carpetas con algo para revisar: %(count)s.", count=logic.count(section.get("total")))
        return title, self._section(title, lead, self._lines(items), self._advice(advice)), "", ""

    def _render_onboarding(self, section):
        title = _("Onboarding atrasado")
        items_raw = [item for item in (section.get("items") if isinstance(section.get("items"), list) else []) if isinstance(item, dict)]
        small = logic.count(section.get("small"))
        note = _("Los recorridos con menos de %(minimum)s personas no se muestran, para no señalar a nadie. Miralos en Buddy → Onboarding → Avance.", minimum=logic.MIN_PEOPLE) if small else ""
        if not items_raw:
            if not logic.count(section.get("journeys")):
                return title, "", "", ""
            return title, "", "", _("Ningún recorrido de onboarding tiene todavía %(minimum)s personas o más en curso: no se muestran cifras.", minimum=logic.MIN_PEOPLE)
        journeys = {journey.id: journey.name for journey in self.env["buddy.journey"].search([("id", "in", logic.int_list([item.get("journey") for item in items_raw], 50))])}
        steps = {step.id: step.name for step in self.env["buddy.journey.step"].search(
            [("id", "in", logic.int_list([row.get("step") for item in items_raw for row in (item.get("steps") if isinstance(item.get("steps"), list) else []) if isinstance(row, dict)], 200))])}
        blocks, advice, anybody = [], "", False
        for item in items_raw:
            name = journeys.get(item.get("journey"))
            late = logic.count(item.get("late_people"))
            if not name or not late:
                continue
            anybody = True
            people = logic.count(item.get("people"))
            rows = []
            for row in (item.get("steps") if isinstance(item.get("steps"), list) else [])[: logic.TOP_STEPS]:
                step = steps.get(row.get("step")) if isinstance(row, dict) else None
                if step:
                    rows.append(Markup("<li>%s</li>") % _("Paso «%(step)s»: %(late)s personas atrasadas", step=clean_title(step, 80), late=logic.count(row.get("late"))))
                    advice = advice or _("Mirá el paso «%(step)s» de «%(journey)s»: %(late)s de %(people)s personas lo tienen atrasado. Quizás falta explicarlo mejor, "
                                         "el plazo es corto o necesita un documento.", step=clean_title(step, 80), journey=clean_title(name, 80), late=logic.count(row.get("late")), people=people)
            blocks.append(Markup("<li><strong>%s</strong>: %s%s</li>") % (
                clean_title(name, 80), _("con tareas obligatorias atrasadas, %(late)s de %(people)s personas · avance promedio: %(pct)s%%", late=late, people=people,
                                         pct=logic.count(item.get("avg_pct"), 100)), self._lines(rows)))
        if not anybody:
            return title, "", _("onboarding"), note
        advice = advice or _("Hay personas atrasadas, pero no en un mismo paso. Revisalas en Buddy → Onboarding → Avance.")
        return title, self._section(title, self._lines(blocks), self._advice(advice)), "", note

    # ------------------------------------------------------------------ the button «Generar ahora» of the settings
    @api.model
    def _generate_now(self):
        """Administrators only: make (or remake) the summary of today. It does not notify anybody: whoever asks is looking at it."""
        if not (self.env.su or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el administrador puede generar el resumen."))
        digest, _created = self._generate(manual=True)
        if not digest:
            raise UserError(_("No pude armar el resumen. Probá de nuevo en un rato."))
        return digest
