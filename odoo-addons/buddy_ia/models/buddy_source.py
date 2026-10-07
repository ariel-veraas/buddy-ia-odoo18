import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from .buddy_chunker import MAX_CHUNKS_PER_DOCUMENT, split_text_ex, strip_toc
from .buddy_text import strip_control_chars
from .drive_client import DriveClient, DriveError, parse_folder_id, parse_service_account

_logger = logging.getLogger(__name__)

DRIVE_KEY_PARAM = "buddy_ia.drive_key"
SYNC_HOURS_PARAM = "buddy_ia.sync_hours"
# One cron run stops starting new files after this long; the folder is marked "queued" and the next run continues.
SYNC_BUDGET_SECONDS = 100
# A folder is never given less than this share of the budget (listing a big folder alone takes a few seconds).
MIN_FOLDER_SECONDS = 15
# The semantic indexing that follows the folders in the same cron run: it gets what is left of the budget, never less
# than the minimum (a request in flight at the limit is the only overshoot) and never more than the maximum.
EMBED_MIN_SECONDS = 15
EMBED_MAX_SECONDS = 60
RETRY_AFTER_ERROR = timedelta(hours=1)
STUCK_AFTER = timedelta(minutes=30)
# Errors that make every other file fail too: stop the whole synchronization instead of marking each file.
FATAL_CODES = {"invalid_key", "auth_failed", "api_disabled", "rate_limited", "network"}


def service_account_email(raw):
    """The `client_email` of a stored service-account key ('' when there is none or it is unreadable).

    The e-mail is what the administrator shares each folder with; it is public information. Nothing else of the key
    (above all the private key) is ever read by this helper.
    """
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    email = data.get("client_email") if isinstance(data, dict) else ""
    return email if isinstance(email, str) else ""


class BuddySource(models.Model):
    _name = "buddy.source"
    _description = "Carpeta de Google Drive"
    _order = "name"

    name = fields.Char(string="Nombre", required=True, help="Cómo se llama esta carpeta para vos. Ej.: «RR.HH.», «Procesos de Ventas».")
    folder_url = fields.Char(string="Carpeta de Drive", required=True, groups="base.group_system",
                             help="Pegá el enlace de la carpeta (el que da «Compartir > Copiar enlace»).")
    folder_id = fields.Char(compute="_compute_folder_id", store=True, index=True, groups="base.group_system")
    group_ids = fields.Many2many(
        "res.groups", "buddy_source_group_rel", "source_id", "group_id", string="Quiénes pueden consultarla",
        help="Solo los usuarios de estos grupos reciben respuestas con los documentos de esta carpeta. "
             "Para que lo vea cualquier empleado, elegí «Usuario interno».")
    active = fields.Boolean(default=True)
    auto_sync = fields.Boolean(string="Sincronizar automáticamente", default=True,
                               help="Una vez por día (configurable). Siempre podés usar «Sincronizar ahora».")
    state = fields.Selection([
        ("never", "Sin sincronizar"), ("queued", "En cola"), ("syncing", "Sincronizando"), ("ok", "Al día"),
        ("error", "Con error")], string="Estado", default="never", required=True, groups="base.group_system")
    last_sync = fields.Datetime(string="Última sincronización", readonly=True, groups="base.group_system")
    last_error = fields.Text(string="Último error", readonly=True, groups="base.group_system")
    last_summary = fields.Char(string="Resultado", readonly=True, groups="base.group_system")
    # The file being processed, saved BEFORE it is processed: when a pass dies (killed by the time limit, out of memory)
    # the next pass finds it here and gives up on that file instead of dying on it forever.
    processing_id = fields.Char(copy=False, readonly=True, groups="base.group_system")
    processing_stamp = fields.Char(copy=False, readonly=True, groups="base.group_system")
    document_ids = fields.One2many("buddy.document", "source_id", string="Documentos")
    document_count = fields.Integer(string="Cantidad de documentos", compute="_compute_document_count")
    # «N de M fragmentos con significado» while the semantic search is on (empty otherwise).
    semantic_summary = fields.Char(string="Búsqueda por significado", compute="_compute_semantic_summary", groups="base.group_system")
    # Shown in the form with a copy button: it is what the folder has to be shared with in Google Drive.
    service_account_email = fields.Char(string="Compartí la carpeta con este correo", compute="_compute_service_account_email",
                                        groups="base.group_system")

    def _compute_service_account_email(self):
        email = service_account_email(self.env["ir.config_parameter"].sudo().get_param(DRIVE_KEY_PARAM))
        for record in self:
            record.service_account_email = email

    def _compute_semantic_summary(self):
        embeddings = self.env["buddy.embedding"].sudo()
        on = bool(embeddings._semantic_cfg())
        coverage = embeddings._coverage([record.id for record in self if isinstance(record.id, int)]) if on else {}
        for record in self:
            done, total = coverage.get(record.id, (0, 0))
            if not on:
                record.semantic_summary = False
            elif not total:
                record.semantic_summary = _("Todavía no hay fragmentos para indexar.")
            else:
                record.semantic_summary = _("%(done)s de %(total)s fragmentos con significado", done=done, total=total)

    @api.depends("folder_url")
    def _compute_folder_id(self):
        for record in self:
            try:
                record.folder_id = parse_folder_id(record.folder_url or "")
            except ValueError:
                record.folder_id = False

    @api.depends("document_ids")
    def _compute_document_count(self):
        counts = dict(self.env["buddy.document"]._read_group([("source_id", "in", self.ids)], ["source_id"], ["__count"]))
        for record in self:
            record.document_count = counts.get(record, 0)

    @api.constrains("folder_url", "group_ids")
    def _check_source(self):
        for record in self.sudo():
            try:
                parse_folder_id(record.folder_url or "")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            if not record.group_ids:
                raise ValidationError(_("Elegí quiénes pueden consultar la carpeta «%(name)s». Sin grupos, nadie recibe respuestas con sus documentos.",
                                        name=record.name))

    @api.model_create_multi
    def create(self, vals_list):
        """A folder created once the Drive key exists starts synchronizing by itself (no need to find the button)."""
        records = super().create(vals_list)
        active = records.filtered("active")
        if active and self.env["ir.config_parameter"].sudo().get_param(DRIVE_KEY_PARAM):
            active.sudo().write({"state": "queued"})
            self._wake_sync_cron()
        return records

    @api.model
    def _wake_sync_cron(self):
        cron = self.env.ref("buddy_ia.cron_sync", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    # ---- Google Drive access -------------------------------------------------------------------

    @api.model
    def _drive_client(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(DRIVE_KEY_PARAM)
        if not raw:
            raise UserError(_("Primero cargá la clave de Google Drive en Ajustes > Buddy IA."))
        try:
            return DriveClient(parse_service_account(raw))
        except DriveError as exc:
            raise UserError(str(exc)) from exc

    # ---- Synchronization -------------------------------------------------------------------------

    def action_sync(self):
        """Queue the folder; the cron picks it up right away (a request must not wait for a long download)."""
        if not (self.env.su or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el administrador puede sincronizar las carpetas."))
        if not self.env["ir.config_parameter"].sudo().get_param(DRIVE_KEY_PARAM):
            raise UserError(_("Primero cargá la clave de Google Drive en Ajustes > Buddy IA."))
        self.write({"state": "queued"})
        self._wake_sync_cron()
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Sincronización en marcha"), "type": "info", "sticky": False,
            "message": _("Va a tardar unos minutos según la cantidad de documentos. Actualizá la pantalla para ver el avance.")}}

    def action_refresh(self):
        """Reload the screen: the synchronization runs in the background and this shows how far it got."""
        if not (self.env.su or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el administrador puede ver el estado de la sincronización."))
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def _sync(self, client=None, commit=False, deadline=None):
        """Bring this folder up to date with Drive. `commit` is only for the cron: each file is saved on its own.

        Returns `{"pending": bool, "progress": int}`: whether files are left for the next pass and how many files
        (or removals) this pass got through, so the cron knows whether calling itself again is worth it.
        """
        self.ensure_one()
        deadline = deadline or time.monotonic() + SYNC_BUDGET_SECONDS
        stats, pending = Counter(), False
        # A file left in the marker means a previous pass died while processing it (the marker is cleared as soon as
        # a file is done): that file is not retried, unless it changed in Drive since.
        crashed = (self.processing_id, self.processing_stamp)
        self.write({"state": "syncing", "last_error": False, "processing_id": False, "processing_stamp": False})
        if commit:
            self.env.cr.commit()
        clear_marker = True
        try:
            client = client or self._drive_client()
            files = client.list_folder(self.folder_id)
            if getattr(client, "truncated", False):
                stats["truncated"] = len(files)
            known = {document.drive_id: document for document in self.document_ids}
            seen = set()
            for drive_file in files:
                seen.add(drive_file.id)
                document = known.get(drive_file.id)
                same = bool(document) and document.modified_time == drive_file.modified_time and (
                    document.md5 or "") == (drive_file.md5 or "")
                if crashed[0] == drive_file.id and crashed[1] == self._file_stamp(drive_file):
                    stats[self._mark_interrupted(drive_file, document)] += 1
                    stats["worked"] += 1
                    if commit:
                        self.env.cr.commit()
                    continue
                if same and (document.state != "error" or document.interrupted):
                    stats["failed" if document.state == "error" else "unchanged"] += 1
                    continue
                if time.monotonic() > deadline:
                    pending = True
                    break
                # Persist what is about to be processed BEFORE doing it: if this pass is killed halfway, the next one
                # knows which file to blame.
                self.write({"processing_id": drive_file.id, "processing_stamp": self._file_stamp(drive_file)})
                if commit:
                    self.env.cr.commit()
                clear_marker = False
                stats[self._index_file(client, drive_file, document, stats)] += 1
                stats["worked"] += 1
                self.write({"processing_id": False, "processing_stamp": False})
                clear_marker = True
                if commit:
                    self.env.cr.commit()
            # A listing cut short by the client's file limit is not the whole folder: never delete what it left out.
            if not pending and not getattr(client, "truncated", False):
                stale = self.document_ids.filtered(lambda document: document.drive_id not in seen)
                stats["removed"] = len(stale)
                stale.unlink()
        except (DriveError, UserError) as exc:
            _logger.warning("Buddy: sincronización de la carpeta %s falló (%s)", self.id, getattr(exc, "code", "usuario"))
            return self._finish(error=str(exc), stats=stats, clear_marker=True)
        except Exception as exc:
            _logger.warning("Buddy: sincronización de la carpeta %s falló (%s)", self.id, type(exc).__name__)
            # The marker stays: if the file being processed is the cause, the next pass skips it instead of repeating.
            return self._finish(error=_("Falló la sincronización por un error inesperado. Revisá el registro del servidor."),
                                stats=stats, clear_marker=clear_marker)
        return self._finish(pending=pending, stats=stats)

    @api.model
    def _file_stamp(self, drive_file):
        """Identity of one version of a Drive file (it changes when the file is edited)."""
        return "%s|%s" % (drive_file.modified_time or "", drive_file.md5 or "")

    def _file_values(self, drive_file):
        # Everything that comes from Drive is untrusted text: a NUL (or any control character) in a name would be a
        # PostgreSQL error that stops the whole synchronization.
        def clean(value):
            return strip_control_chars(value) if isinstance(value, str) else value

        return {"source_id": self.id, "drive_id": drive_file.id, "name": clean(drive_file.name), "path": clean(drive_file.path),
                "web_view_link": clean(drive_file.web_view_link), "mime_type": clean(drive_file.mime_type),
                "modified_time": clean(drive_file.modified_time), "md5": clean(drive_file.md5)}

    def _save_document(self, document, values):
        if document:
            document.write(values)
        else:
            document = self.env["buddy.document"].create(values)
        document.chunk_ids.unlink()
        return document

    def _mark_interrupted(self, drive_file, document):
        """The previous pass died on this file: record it as an error and do not retry until it changes in Drive."""
        values = self._file_values(drive_file)
        values.update({"state": "error", "interrupted": True,
                       "error": _("Se interrumpió el procesamiento de este archivo (puede estar dañado o ser demasiado pesado). "
                                  "No se vuelve a intentar hasta que cambie en Drive.")})
        self._save_document(document, values)
        return "failed"

    def _index_file(self, client, drive_file, document, stats=None):
        """Download one file and replace its chunks. Returns the counter key for the summary."""
        values = self._file_values(drive_file)
        state, error, pieces = "ready", False, []
        try:
            text = client.fetch_text(drive_file)
            text = strip_control_chars(text) if isinstance(text, str) else text
            # The table of contents is dropped: every title would match the question and none would answer it.
            pieces, cut = split_text_ex(strip_toc(text)) if text else ([], False)
            if not pieces:
                state, error = "empty", False
            elif cut:
                error = _("Documento truncado a %(n)s fragmentos: se indexó solo el comienzo.", n=MAX_CHUNKS_PER_DOCUMENT)
            elif getattr(client, "last_fetch_truncated", False):
                error = _("Documento truncado: se leyó solo la primera parte (límite de páginas o de tamaño).")
            if stats is not None and error and state == "ready":
                stats["cut"] += 1
        except DriveError as exc:
            if exc.code in FATAL_CODES:
                raise
            state, error = "error", strip_control_chars(str(exc))[:240]
        values.update({"state": state, "error": error, "interrupted": False})
        embeddings = self.env["buddy.embedding"]
        old_vectors = embeddings._snapshot(document) if document else {}
        document = self._save_document(document, values)
        if pieces:
            chunks = self.env["buddy.chunk"].create([
                {"document_id": document.id, "sequence": index, "content": piece} for index, piece in enumerate(pieces)])
            # The pieces of text that did not change keep their vectors: only what is new is sent to the provider.
            embeddings._carry_over(chunks, old_vectors)
        return {"ready": "updated", "empty": "empty", "error": "failed"}[state]

    def _finish(self, error=None, pending=False, stats=None, clear_marker=True):
        stats = stats or Counter()
        parts = [(_("%(n)s nuevos o actualizados", n=stats["updated"]), stats["updated"]),
                 (_("%(n)s sin cambios", n=stats["unchanged"]), stats["unchanged"]),
                 (_("%(n)s sin texto o no compatibles", n=stats["empty"]), stats["empty"]),
                 (_("%(n)s con error", n=stats["failed"]), stats["failed"]),
                 (_("%(n)s eliminados", n=stats["removed"]), stats["removed"])]
        summary = ", ".join(text for text, count in parts if count) or _("Sin documentos compatibles")
        if stats["cut"]:
            summary += " · " + _("%(n)s documentos truncados (el detalle de cada uno está en la lista)", n=stats["cut"])
        if stats["truncated"]:
            summary += " · " + _("tope de archivos alcanzado (se leyeron los primeros %(n)s): dividí la carpeta en varias", n=stats["truncated"])
        if pending:
            summary += " · " + _("continúa en la próxima pasada")
        values = {"state": "error" if error else "queued" if pending else "ok", "last_sync": fields.Datetime.now(),
                  "last_error": error or False, "last_summary": summary}
        if clear_marker:
            values.update({"processing_id": False, "processing_stamp": False})
        self.write(values)
        return {"pending": bool(pending), "progress": stats["worked"] + stats["removed"]}

    def _is_due(self, now, interval):
        """Whether the cron should synchronize this folder now (see the states in the architecture notes)."""
        self.ensure_one()
        if self.state == "queued":
            return True
        if self.state == "syncing":
            return (self.write_date or now) + STUCK_AFTER <= now
        if self.state == "error":
            return bool(self.auto_sync) and (self.last_sync or now) + RETRY_AFTER_ERROR <= now
        return bool(self.auto_sync) and self.state in ("never", "ok") and (
            not self.last_sync or self.last_sync + interval <= now)

    @api.model
    def _cron_sync(self):
        parameters = self.env["ir.config_parameter"].sudo()
        deadline = time.monotonic() + SYNC_BUDGET_SECONDS
        progress, pending = 0, False
        if parameters.get_param(DRIVE_KEY_PARAM):
            progress, pending = self._sync_due_folders(parameters, deadline)
        # The semantic indexing (if the administrator turned it on) follows, with what is left of the time: the vectors of
        # what was just read, or the backlog of an older folder. Each request is saved on its own, so a pass that is cut
        # short resumes where it stopped.
        indexed = self.env["buddy.embedding"]._cron_index(
            min(max(deadline - time.monotonic(), EMBED_MIN_SECONDS), EMBED_MAX_SECONDS)) or {}
        if indexed.get("state") == "ok" and indexed.get("left") and indexed.get("done"):
            progress, pending = progress + indexed["done"], True
        # Work is left and this pass moved forward: call the cron again at once instead of waiting 30 minutes. With no
        # progress (an error, a folder that cannot advance) it just waits for its normal turn.
        if pending and progress:
            self._wake_sync_cron()

    @api.model
    def _sync_due_folders(self, parameters, deadline):
        """Synchronize the folders that are due within the budget: (files or removals handled, whether work is left)."""
        interval = timedelta(hours=max(1, int(parameters.get_param(SYNC_HOURS_PARAM) or 24)))
        now = fields.Datetime.now()
        # Queued folders first, then the ones that went longest without a pass: nobody is starved by a big folder
        # that keeps taking the whole budget.
        due = self.sudo().search([("active", "=", True)]).filtered(lambda source: source._is_due(now, interval))
        due = due.sorted(key=lambda source: (source.state != "queued", source.last_sync or datetime.min, source.id))
        progress, pending = 0, False
        for position, source in enumerate(due):
            left = deadline - time.monotonic()
            if left <= 0:
                pending = True  # no time for the rest: they are still due, the next run takes them
                break
            # A fair share of what is left (dynamic: a folder that finishes early leaves its time to the others).
            share = min(left, max(left / (len(due) - position), MIN_FOLDER_SECONDS))
            result = source._sync(commit=True, deadline=time.monotonic() + share) or {}
            self.env.cr.commit()
            progress += result.get("progress", 0)
            pending = pending or bool(result.get("pending"))
        return progress, pending
