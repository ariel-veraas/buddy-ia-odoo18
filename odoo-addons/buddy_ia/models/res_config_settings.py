import base64
import binascii
import json
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from . import buddy_vectors
from .buddy_embedding import DIMS_PARAM as EMBEDDING_DIMS_PARAM, MODEL_PARAM as EMBEDDING_MODEL_PARAM, SEMANTIC_PARAM
from .buddy_engine import MAX_DAILY_TOTAL, SYNONYMS_PARAM, BuddyBusy
from .buddy_insight import MAX_RETENTION_DAYS, flag
from .buddy_journey_logic import REMINDERS_PARAM as JOURNEY_REMINDERS_PARAM, STAGE_PARAM as JOURNEY_STAGE_PARAM
from .buddy_proactive_logic import (
    CHIPS_PARAM, DEFAULT_DIGEST_NOTIFY, DIGEST_NOTIFY, DIGEST_NOTIFY_PARAM, DIGEST_PARAM, NUDGE_PARAM, switch_on)
from .buddy_look import (
    ACCESSORIES, ACCESSORY_PARAM, CHARACTERS, CHARACTER_PARAM, FREQUENCIES, GESTURE_GROUPS, IDLE_ENABLED_PARAM,
    IDLE_FREQUENCY_PARAM, IDLE_GROUPS_PARAM, ids, parse_enabled, parse_groups, serialize_groups)
from .buddy_provider import (
    PROVIDERS, base_url, default_embedding_dims, default_embedding_model, default_model, supports_embeddings, valid_base_url)
from .buddy_source import DRIVE_KEY_PARAM, service_account_email
from .buddy_text import is_placeholder_email, validate_synonyms
from .drive_client import DriveError, parse_service_account
from .buddy_ticket import TICKET_GRANTED_PARAM, TICKET_USER_PARAM, is_buddy_manager

_logger = logging.getLogger(__name__)

HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LAST_TEST_PARAM = "buddy_ia.last_test"
# What the last «Probar conexión» said, line by line, so the checklist keeps showing it after the notification is gone.
LAST_TEST_DETAIL_PARAM = "buddy_ia.last_test_detail"
LAST_TEST_DETAIL_LIMIT = 3000
# A service-account key file is about 2.4 KB; anything much bigger is not one.
MAX_KEY_FILE_BYTES = 64 * 1024
API_KEY_PARAM = "buddy_ia.api_key"
# Free text appended to the engine's prompt by the engine itself; it is never sent to the browser.
EXTRA_INSTRUCTIONS_PARAM = "buddy_ia.extra_instructions"


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # --- AI model ---------------------------------------------------------------------------
    buddy_provider = fields.Selection(
        [(key, label) for key, (label, unused_url, unused_protocol) in PROVIDERS.items()], string="Proveedor de IA",
        config_parameter="buddy_ia.provider", default="anthropic")
    buddy_api_base = fields.Char(string="Dirección de la API", config_parameter="buddy_ia.api_base",
                                 help="Solo para «Otro»: la dirección base de una API compatible con OpenAI (Azure, Ollama, etc.).")
    # Write-only: the stored key is never sent back to the browser, only whether one is loaded.
    buddy_api_key_input = fields.Char(string="Clave de API", copy=False)
    buddy_api_key_loaded = fields.Boolean(string="Clave de API cargada", compute="_compute_buddy_secrets")
    buddy_chat_model = fields.Char(string="Modelo", config_parameter="buddy_ia.chat_model",
                                   help="El nombre exacto del modelo, como lo muestra tu proveedor. «Probar conexión» te lista los disponibles.")
    # What an empty "Modelo" means for the chosen provider (display only; the engine applies the default itself).
    buddy_model_hint = fields.Char(string="Modelo por defecto", compute="_compute_buddy_model_hint")

    # --- Search by meaning (embeddings) -----------------------------------------------------------
    # Off unless an administrator turns it on: computing the vectors sends the TEXT of every fragment of every connected
    # folder to the provider (see docs/SECURITY.md). An unchecked box deletes the parameter, and «no parameter» is off.
    buddy_semantic_enabled = fields.Boolean(
        string="Búsqueda por significado", config_parameter=SEMANTIC_PARAM,
        help="Buddy encuentra la respuesta aunque la pregunta use otras palabras que el documento. Para eso envía al proveedor de IA el texto de "
             "TODOS los documentos de TODAS las carpetas conectadas.")
    buddy_embedding_model = fields.Char(
        string="Modelo de embeddings", config_parameter=EMBEDDING_MODEL_PARAM,
        help="El modelo del proveedor que convierte el texto en números para comparar significados. Vacío = el que Buddy elige para tu proveedor.")
    buddy_embedding_model_hint = fields.Char(string="Modelo de embeddings por defecto", compute="_compute_buddy_embedding_hint")
    buddy_embedding_dims = fields.Integer(
        string="Tamaño del vector", config_parameter=EMBEDDING_DIMS_PARAM,
        help="Cuántos números describen cada fragmento. Más números son más precisos pero ocupan más espacio y son más lentos. "
             "0 = el que Buddy elige para tu proveedor.")
    buddy_semantic_status = fields.Text(string="Estado de la búsqueda por significado", compute="_compute_buddy_semantic_status",
                                        groups="base.group_system")

    # --- Google Drive -----------------------------------------------------------------------
    buddy_drive_key_input = fields.Text(string="Clave de la cuenta de servicio", copy=False)
    # The same key as a file (what Google gives you): easier than copying its content. Like the pasted text it is
    # write-only: it is read once on save, stored without the extra fields and cleared from this temporary record.
    # attachment=False: it must not become an attachment (a file in the file store) that outlives the temporary record.
    buddy_drive_key_file = fields.Binary(string="Archivo .json de la clave", attachment=False, copy=False)
    buddy_drive_key_filename = fields.Char(copy=False)
    buddy_drive_loaded = fields.Boolean(string="Clave de Drive cargada", compute="_compute_buddy_secrets")
    buddy_drive_email = fields.Char(string="Compartí tus carpetas con", compute="_compute_buddy_secrets")
    buddy_sync_hours = fields.Integer(string="Sincronizar cada (horas)", config_parameter="buddy_ia.sync_hours", default=24)

    # --- Identity and look & feel ---------------------------------------------------------
    buddy_name = fields.Char(string="Nombre del buddy", config_parameter="buddy_ia.buddy_name", default="Buddy")
    buddy_company = fields.Char(string="Nombre de la empresa", config_parameter="buddy_ia.company", default="la empresa")
    buddy_tagline = fields.Char(string="Subtítulo", config_parameter="buddy_ia.tagline",
                                help="Frase corta debajo del nombre, en la cabecera del chat. Vacío = «Tu guía en Odoo».")
    buddy_greeting = fields.Char(string="Saludo", config_parameter="buddy_ia.greeting",
                                 help="Lo que dice el globito la primera vez. Escribí {nombre} para incluir el nombre de la persona. "
                                      "Vacío = saludo estándar.")
    buddy_suggestion_1 = fields.Char(string="Sugerencia 1", config_parameter="buddy_ia.suggestion_1")
    buddy_suggestion_2 = fields.Char(string="Sugerencia 2", config_parameter="buddy_ia.suggestion_2")
    buddy_suggestion_3 = fields.Char(string="Sugerencia 3", config_parameter="buddy_ia.suggestion_3")
    buddy_suggestion_4 = fields.Char(string="Sugerencia 4", config_parameter="buddy_ia.suggestion_4")
    buddy_primary_color = fields.Char(string="Color principal", config_parameter="buddy_ia.primary_color",
                                      help="Color de los botones y de los globos del usuario, en formato #RRGGBB. Vacío = celeste original.")
    # Stored inverted on purpose: an unchecked box deletes the parameter, and "no parameter" must mean "visible".
    buddy_hidden = fields.Boolean(string="Ocultar el buddy a todos", config_parameter="buddy_ia.hidden")

    # --- Character, accessory and idle gestures (the whitelists live in buddy_look.py) -------------------
    buddy_character = fields.Selection(
        [(key, label) for key, label in CHARACTERS], string="Personaje", config_parameter=CHARACTER_PARAM, default="mochi",
        help="Quién es el buddy.")
    buddy_accessory = fields.Selection(
        [(key, label) for key, label in ACCESSORIES], string="Accesorio", config_parameter=ACCESSORY_PARAM, default="none",
        help="Un accesorio fijo que lleva siempre puesto, o ninguno.")
    buddy_idle_frequency = fields.Selection(
        [(key, label) for key, label in FREQUENCIES], string="Frecuencia de los gestos", config_parameter=IDLE_FREQUENCY_PARAM,
        default="normal")
    # The four checkboxes below are ON by default, but an unchecked box would delete a config_parameter (and "no
    # parameter" would bring them back on): they are read and written by hand as explicit "1"/"0" values.
    # --- Onboarding ----------------------------------------------------------------------------
    # Reminders are ON unless the parameter says "0" (written explicitly, never deleted: an unchecked box would bring the
    # default back). The stage context is OFF unless on: it adds one line about the person's stage to what the provider receives.
    buddy_journey_reminders = fields.Boolean(
        string="Recordatorios de onboarding",
        help="Cada día, si alguien tiene tareas de su recorrido para hoy o atrasadas, Buddy le crea una sola actividad en Odoo que las resume.")
    buddy_journey_stage = fields.Boolean(
        string="Adaptar las respuestas a la etapa de cada persona", config_parameter=JOURNEY_STAGE_PARAM,
        help="Buddy le avisa al servicio de IA en qué etapa de su recorrido está la persona («su semana 1 del recorrido X») para que explique con el "
             "detalle justo. No se envía su nombre ni ningún dato personal.")

    # --- Gentle proactivity (E4b) -------------------------------------------------------------------------
    # The three switches are ON unless the parameter says «0» (written explicitly, never deleted: an unchecked box would bring the
    # default back). None of them sends anything to the AI provider, and the weekly summary stays inside Odoo.
    buddy_proactive_chips = fields.Boolean(
        string="Sugerencias a medida en el chat",
        help="Los botones de sugerencia del chat pasan a ser relevantes: las preguntas de las tareas de onboarding de cada persona y los temas que más consulta la empresa "
             "(solo títulos de documentos que esa persona puede leer, y solo si hay bastante gente consultando).")
    buddy_proactive_nudge = fields.Boolean(
        string="Aviso discreto de tareas de onboarding",
        help="Si alguien tiene tareas para hoy o atrasadas, una vez por día (como mucho) y solo con el chat cerrado, el buddy muestra un globito breve que se puede cerrar. "
             "Usa los gestos del buddy: si apagás los gestos o la persona pidió «reducir movimiento», no avisa.")
    buddy_digest_enabled = fields.Boolean(
        string="Resumen semanal «Qué falta documentar»",
        help="Una vez por semana Buddy arma un resumen con las preguntas sin respuesta, los documentos con 👎, los que se contradicen o no se usan y las carpetas con problemas. "
             "Se arma sin inteligencia artificial y no envía correos.")
    buddy_digest_notify = fields.Selection(
        [(key, label) for key, label in DIGEST_NOTIFY], string="Avisar a", config_parameter=DIGEST_NOTIFY_PARAM, default=DEFAULT_DIGEST_NOTIFY,
        help="A quién le llega la actividad de Odoo que avisa que hay un resumen nuevo. Nunca se manda un correo.")

    buddy_idle_enabled = fields.Boolean(string="El buddy hace gestos cuando está inactivo")
    buddy_idle_expressions = fields.Boolean(string="Expresiones")
    buddy_idle_accessories = fields.Boolean(string="Accesorios")
    buddy_idle_sleep = fields.Boolean(string="Sueño")

    buddy_dialect = fields.Selection(
        [("rioplatense", "Español rioplatense (vos)"), ("neutral", "Español neutro (tú)")], string="Cómo habla",
        config_parameter="buddy_ia.dialect", default="rioplatense")

    # --- Company instructions (read by the engine) ------------------------------------------
    # Text fields cannot use config_parameter (Odoo only allows char/int/float/bool/selection/many2one/datetime there),
    # so this one is read and written by hand in get_values/set_values below.
    buddy_extra_instructions = fields.Text(
        string="Instrucciones de la empresa",
        help="Texto libre que se suma a las instrucciones del buddy: tono, vocabulario, a quién derivar, qué no responder.")

    # --- Company synonyms (read by the engine) ----------------------------------------------
    # One group per line, words separated by commas. Text field: read and written by hand like the instructions.
    buddy_synonyms = fields.Text(
        string="Sinónimos de la empresa",
        help="Un grupo por línea, con las palabras separadas por comas: Buddy las trata como la misma cosa al buscar. "
             "Ejemplo: vacaciones, licencia, días libres.")

    # --- Tickets --------------------------------------------------------------------------------
    buddy_ticket_user_id = fields.Many2one(
        "res.users", string="Responsable de tickets", config_parameter="buddy_ia.ticket_user_id",
        domain=[("share", "=", False)],
        help="Quien atiende los tickets que crea el buddy: al crearse uno, le llega una tarea y un aviso. Al guardar se le da el permiso "
             "«Gestor de Buddy» (ve todos los tickets y las estadísticas). Vacío = nadie recibe aviso.")

    # --- Statistics, costs and the company-wide daily limit ---------------------------------------
    buddy_insight_questions = fields.Boolean(
        string="Guardar preguntas", config_parameter="buddy_ia.insight_questions",
        help="Guarda el texto de TODAS las preguntas (sin el nombre de quien las hizo) para ver qué se consulta más; también la de cada voto 👎. "
             "Si está apagado, solo se guardan las que el buddy no supo responder y las de los votos 👎 donde la persona tildó «Incluir mi pregunta». "
             "Las preguntas pueden tener datos personales: encendelo solo si lo necesitás.")
    buddy_insight_days = fields.Integer(string="Conservar estadísticas (días)", config_parameter="buddy_ia.insight_days", default=90,
                                        help="Después de este tiempo se borran solas las estadísticas, los votos 👍/👎 y las preguntas guardadas. De 1 a 3650.")
    buddy_price_in = fields.Float(string="Precio por millón de tokens de entrada (USD)", config_parameter="buddy_ia.price_in_per_m",
                                  digits=(12, 4), default=0.0)
    buddy_price_out = fields.Float(string="Precio por millón de tokens de salida (USD)", config_parameter="buddy_ia.price_out_per_m",
                                   digits=(12, 4), default=0.0)
    buddy_max_daily_total = fields.Integer(
        string="Consultas por día (toda la empresa)", config_parameter="buddy_ia.max_daily_total", default=1000,
        help="Tope de consultas que Buddy atiende en 24 horas sumando a todas las personas. Al llegar, avisa con amabilidad y se reanuda solo.")
    buddy_max_daily_cost = fields.Float(
        string="Costo por día (USD, toda la empresa)", config_parameter="buddy_ia.max_daily_cost", digits=(12, 2), default=0.0,
        help="Opcional: tope de costo estimado en 24 horas. 0 = sin tope. Necesita que cargues los precios por millón de tokens.")
    buddy_usage_text = fields.Char(string="Uso de los últimos 30 días", compute="_compute_buddy_usage_text")
    buddy_failures_text = fields.Char(string="Consultas fallidas", compute="_compute_buddy_usage_text")

    # --- Privacy and limits ----------------------------------------------------------------
    buddy_retention_days = fields.Integer(string="Retención del historial (días)", config_parameter="buddy_ia.retention_days", default=30,
                                          help="Cada conversación se guarda hasta esta cantidad de días después de su último mensaje. 0 = no borrar por inactividad. "
                                               "En todos los casos se borra lo escrito hace más de 180 días.")
    buddy_rate_per_minute = fields.Integer(string="Consultas por minuto y por persona", config_parameter="buddy_ia.rate_per_minute", default=6)
    buddy_rate_per_day = fields.Integer(string="Consultas por día y por persona", config_parameter="buddy_ia.rate_per_day", default=40)
    buddy_new_user_turns = fields.Integer(string="Consultas para dejar de ser 'nuevo'", config_parameter="buddy_ia.new_user_turns", default=12)
    buddy_max_concurrent = fields.Integer(
        string="Consultas simultáneas (todos los usuarios)", config_parameter="buddy_ia.max_concurrent", default=3,
        help="Cuántas consultas puede estar respondiendo Buddy al mismo tiempo, sumando a todos. Cada una ocupa un proceso del "
             "servidor hasta 35 segundos: si se supera, la persona recibe un aviso amable y no gasta su cuota. De 1 a 100.")

    # --- Advanced (collapsed by default) ----------------------------------------------------
    buddy_top_k = fields.Integer(string="Fragmentos por consulta", config_parameter="buddy_ia.top_k", default=6)
    # Remembered (a parameter): the block must not fold itself back every time the administrator saves.
    buddy_show_advanced = fields.Boolean(string="Mostrar opciones avanzadas", config_parameter="buddy_ia.show_advanced")

    # --- "Para empezar" checklist (live, computed; never stored) ----------------------------
    buddy_ready_ai = fields.Boolean(string="IA configurada", compute="_compute_buddy_checklist")
    buddy_ready_drive = fields.Boolean(string="Google Drive conectado", compute="_compute_buddy_checklist")
    buddy_ready_source = fields.Boolean(string="Carpeta con grupo asignado", compute="_compute_buddy_checklist")
    buddy_ready_documents = fields.Boolean(string="Documentos leídos", compute="_compute_buddy_checklist")
    buddy_ready_contacts = fields.Boolean(string="Contactos con correo real", compute="_compute_buddy_checklist")
    buddy_contacts_text = fields.Char(string="Estado de los contactos", compute="_compute_buddy_checklist")
    buddy_ready_test = fields.Boolean(string="Última prueba correcta", compute="_compute_buddy_checklist")
    buddy_last_test_text = fields.Char(string="Última prueba", compute="_compute_buddy_checklist")
    buddy_last_test_detail = fields.Text(string="Resultado de la última prueba", compute="_compute_buddy_checklist")

    @api.model
    @api.private
    def get_values(self):
        values = super().get_values()
        values["buddy_extra_instructions"] = self.env["ir.config_parameter"].sudo().get_param(EXTRA_INSTRUCTIONS_PARAM) or ""
        values["buddy_synonyms"] = self.env["ir.config_parameter"].sudo().get_param(SYNONYMS_PARAM) or ""
        parameters = self.env["ir.config_parameter"].sudo()
        values["buddy_idle_enabled"] = parse_enabled(parameters.get_param(IDLE_ENABLED_PARAM) or None)
        values["buddy_journey_reminders"] = parse_enabled(parameters.get_param(JOURNEY_REMINDERS_PARAM) or None)
        values["buddy_proactive_chips"] = switch_on(parameters.get_param(CHIPS_PARAM))
        values["buddy_proactive_nudge"] = switch_on(parameters.get_param(NUDGE_PARAM))
        values["buddy_digest_enabled"] = switch_on(parameters.get_param(DIGEST_PARAM))
        groups = parse_groups(parameters.get_param(IDLE_GROUPS_PARAM))
        for group in ids(GESTURE_GROUPS):
            values["buddy_idle_" + group] = group in groups
        return values

    def set_values(self):
        self._buddy_require_admin()  # before anything is read or written (the parent checks it too, later)
        parameters = self.env["ir.config_parameter"].sudo()
        semantic_before = self._buddy_semantic_snapshot()
        super().set_values()
        self._buddy_semantic_changed(semantic_before)
        text = (self.buddy_extra_instructions or "").strip()
        # False deletes the parameter: an empty box means "no extra instructions".
        parameters.set_param(EXTRA_INSTRUCTIONS_PARAM, text or False)
        # Same for the synonyms; blank lines are dropped so the stored text is the list and nothing else.
        lines = [line.strip() for line in (self.buddy_synonyms or "").replace("\r", "").split("\n") if line.strip()]
        parameters.set_param(SYNONYMS_PARAM, "\n".join(lines) or False)
        # Explicit "1"/"0" (never False, which would delete the parameter and bring the default back).
        parameters.set_param(IDLE_ENABLED_PARAM, "1" if self.buddy_idle_enabled else "0")
        parameters.set_param(JOURNEY_REMINDERS_PARAM, "1" if self.buddy_journey_reminders else "0")
        parameters.set_param(CHIPS_PARAM, "1" if self.buddy_proactive_chips else "0")
        parameters.set_param(NUDGE_PARAM, "1" if self.buddy_proactive_nudge else "0")
        parameters.set_param(DIGEST_PARAM, "1" if self.buddy_digest_enabled else "0")
        if not self.buddy_digest_enabled:
            self.env["buddy.digest"]._clear_notices()  # the last notice would stay open for a summary nobody is making any more
        parameters.set_param(IDLE_GROUPS_PARAM, serialize_groups(
            [group for group in ids(GESTURE_GROUPS) if getattr(self, "buddy_idle_" + group)]))
        api_key = (self.buddy_api_key_input or "").strip()
        if api_key:
            parameters.set_param(API_KEY_PARAM, api_key)
        pasted = (self.buddy_drive_key_input or "").strip()
        uploaded = self._buddy_uploaded_key()
        if pasted and uploaded:
            raise ValidationError(_("Cargá la clave de Drive de una sola forma: o subís el archivo .json o pegás su contenido, no las dos."))
        drive_key = pasted or uploaded
        if drive_key:
            try:
                # Only the fields Buddy needs are kept; whatever else the file had is dropped.
                parameters.set_param(DRIVE_KEY_PARAM, json.dumps(parse_service_account(drive_key)))
            except DriveError as exc:
                raise ValidationError(str(exc)) from exc
        # The pasted or uploaded keys must not stay in the transient table until the vacuum runs.
        if api_key or drive_key:
            self.write({"buddy_api_key_input": False, "buddy_drive_key_input": False,
                        "buddy_drive_key_file": False, "buddy_drive_key_filename": False})
        self._buddy_sync_ticket_manager()

    def _buddy_semantic_snapshot(self):
        parameters = self.env["ir.config_parameter"].sudo()
        return (flag(parameters.get_param(SEMANTIC_PARAM)), (parameters.get_param(EMBEDDING_MODEL_PARAM) or "").strip(),
                str(parameters.get_param(EMBEDDING_DIMS_PARAM) or 0), parameters.get_param("buddy_ia.provider") or "",
                base_url(parameters.get_param("buddy_ia.provider") or "", parameters.get_param("buddy_ia.api_base") or ""))

    def _buddy_semantic_changed(self, before):
        """Leave a trace of who turned the search by meaning on or off (it sends every document's text to the provider) and
        start the indexing right away when it was just turned on or its model or size changed."""
        after = self._buddy_semantic_snapshot()
        if after == before:
            return
        if after[0] and not before[0]:
            if supports_embeddings(after[3]):
                _logger.warning("Buddy: el usuario con uid %s activó la búsqueda por significado: el texto de los documentos de las carpetas "
                                "conectadas se envía al proveedor de IA para calcular los vectores", self.env.uid)
            else:
                _logger.warning("Buddy: el usuario con uid %s activó la búsqueda por significado, pero el proveedor no ofrece embeddings: "
                                "no se envía nada", self.env.uid)
        elif before[0] and not after[0]:
            _logger.warning("Buddy: el usuario con uid %s apagó la búsqueda por significado", self.env.uid)
        elif after[0] and supports_embeddings(after[3]) and (before[3], before[4]) != (after[3], after[4]):
            # The function was already on and the destination of the documents' text changed.
            _logger.warning("Buddy: el usuario con uid %s cambió el proveedor de IA (ahora «%s») con la búsqueda por significado activa: "
                            "el texto de los documentos se vuelve a enviar al proveedor nuevo", self.env.uid, after[3])
        elif after[0] and supports_embeddings(after[3]) and before[1:3] != after[1:3]:
            _logger.warning("Buddy: el usuario con uid %s cambió el modelo o el tamaño de los vectores: se vuelve a enviar el texto "
                            "de los documentos al proveedor", self.env.uid)
        if after[0]:
            self.env["buddy.source"]._wake_sync_cron()

    @api.onchange("buddy_semantic_enabled", "buddy_provider", "buddy_api_base", "buddy_embedding_model")
    def _onchange_buddy_semantic_enabled(self):
        """The explicit warning when it is turned on, or when it is on and the provider (the destination of the text) changes:
        what leaves the server, and where to."""
        saved = self._buddy_semantic_snapshot()
        if (self.buddy_semantic_enabled and saved[0] and supports_embeddings(self.buddy_provider)
                and (saved[3], saved[4]) != (self.buddy_provider or "", base_url(self.buddy_provider or "", self.buddy_api_base or ""))):
            return {"warning": {
                "title": _("Cambiaste de proveedor con la búsqueda por significado activa"),
                "message": _("Al guardar, Buddy va a volver a calcular los vectores con el proveedor nuevo: el TEXTO de todos los documentos de "
                             "todas las carpetas conectadas (también las restringidas) se envía a ese proveedor. Si no querés eso, "
                             "apagá antes la búsqueda por significado.")}}
        if self.buddy_semantic_enabled and not supports_embeddings(self.buddy_provider):
            return {"warning": {
                "title": _("Tu proveedor no ofrece búsqueda por significado"),
                "message": _("Claude (Anthropic) no tiene búsqueda por significado: Buddy seguiría buscando solo por palabras. "
                             "Para usarla elegí Gemini, OpenAI u otro proveedor compatible.")}}
        if self.buddy_semantic_enabled and not saved[0]:
            return {"warning": {
                "title": _("Antes de activar la búsqueda por significado"),
                "message": _("Para buscar por significado, Buddy envía al proveedor de IA el TEXTO de todos los documentos de todas las carpetas "
                             "conectadas (también las de acceso restringido), no solo los que se usan en una respuesta. Activala solo si tu "
                             "empresa acepta que ese contenido pase por tu proveedor. Los permisos por grupo se siguen respetando al responder. "
                             "Podés apagarla cuando quieras y borrar los datos con el botón de la misma sección.")}}

    def _buddy_uploaded_key(self):
        """The text of the uploaded key file ('' when none). The file is base64 in the temporary record."""
        raw = self.buddy_drive_key_file
        if not raw:
            return ""
        try:
            # `bin_size` would make the ORM answer with a size label instead of the content.
            content = base64.b64decode(self.with_context(bin_size=False).buddy_drive_key_file or raw)
            if len(content) > MAX_KEY_FILE_BYTES:
                raise ValueError("size")
            return content.decode("utf-8-sig").strip()
        except (ValueError, binascii.Error):
            raise ValidationError(_("El archivo no parece una clave de cuenta de servicio de Google: tiene que ser el .json que descargaste "
                                    "de Google Cloud, de menos de 64 KB.")) from None

    def _buddy_sync_ticket_manager(self):
        """The person chosen to handle tickets must be able to open them: they get the «Gestor de Buddy» permission.

        Buddy remembers WHO it gave it to (`buddy_ia.ticket_user_granted`): when somebody else is chosen, or nobody, the
        permission it gave goes away again. One an administrator gave by hand is never touched. Both changes are
        logged with the administrator's id.
        """
        responsible = self.buddy_ticket_user_id
        parameters = self.env["ir.config_parameter"].sudo()
        try:
            granted_id = int(parameters.get_param(TICKET_GRANTED_PARAM) or 0)
        except ValueError:
            granted_id = 0
        if not granted_id and not responsible:
            return
        group = self.env.ref("buddy_ia.group_buddy_manager")
        if granted_id and (not responsible or responsible.id != granted_id):
            previous = self.env["res.users"].sudo().browse(granted_id).exists()
            if previous and group in previous.groups_id:
                previous.write({"groups_id": [(3, group.id)]})
                _logger.warning("Buddy: el usuario con uid %s le quitó el permiso «Gestor de Buddy» al usuario %s (ya no es el responsable de tickets)",
                                self.env.uid, previous.id)
            parameters.set_param(TICKET_GRANTED_PARAM, False)
            granted_id = 0
        if responsible and not granted_id and not is_buddy_manager(responsible):
            responsible.sudo().write({"groups_id": [(4, group.id)]})
            parameters.set_param(TICKET_GRANTED_PARAM, str(responsible.id))
            _logger.warning("Buddy: el usuario con uid %s le dio el permiso «Gestor de Buddy» al usuario %s (responsable de tickets)",
                            self.env.uid, responsible.id)

    @api.depends("buddy_provider", "buddy_chat_model")
    def _compute_buddy_secrets(self):
        parameters = self.env["ir.config_parameter"].sudo()
        api_key = bool(parameters.get_param(API_KEY_PARAM))
        email = service_account_email(parameters.get_param(DRIVE_KEY_PARAM))
        for record in self:
            record.buddy_api_key_loaded = api_key
            record.buddy_drive_loaded = bool(email)
            record.buddy_drive_email = email

    @api.depends("buddy_provider")
    def _compute_buddy_embedding_hint(self):
        for record in self:
            model, dims = default_embedding_model(record.buddy_provider), default_embedding_dims(record.buddy_provider)
            if not model:
                record.buddy_embedding_model_hint = (_("Este proveedor no tiene un modelo por defecto: escribí el nombre del modelo de embeddings de tu servidor.")
                                                     if supports_embeddings(record.buddy_provider) else _("Este proveedor no ofrece búsqueda por significado."))
            else:
                record.buddy_embedding_model_hint = (_("Si lo dejás vacío se usa «%(model)s» con vectores de %(dims)s números.", model=model, dims=dims)
                                                     if dims else _("Si lo dejás vacío se usa «%(model)s».", model=model))

    @api.depends("buddy_semantic_enabled", "buddy_provider", "buddy_embedding_model", "buddy_embedding_dims")
    def _compute_buddy_semantic_status(self):
        """What the administrator needs to know about the search by meaning, in plain words (live, never stored)."""
        embeddings = self.env["buddy.embedding"].sudo()
        parameters = self.env["ir.config_parameter"].sudo()
        saved_on, provider = flag(parameters.get_param(SEMANTIC_PARAM)), parameters.get_param("buddy_ia.provider") or "anthropic"
        cfg = embeddings._semantic_cfg()
        if not saved_on:
            text = ""
        elif not supports_embeddings(provider):
            text = _("Tu proveedor no ofrece búsqueda por significado: Buddy sigue buscando solo por palabras.")
        elif not parameters.get_param("buddy_ia.api_key"):
            text = _("Falta la clave de API del proveedor: Buddy sigue buscando solo por palabras.")
        elif not cfg:
            text = _("Falta el nombre del modelo de embeddings: Buddy sigue buscando solo por palabras.")
        else:
            coverage = embeddings._coverage()
            done, total = sum(item[0] for item in coverage.values()), sum(item[1] for item in coverage.values())
            lines = [_("%(done)s de %(total)s fragmentos con significado (modelo %(model)s).", done=done, total=total, model=cfg["model"])]
            state, when = embeddings._state()
            if embeddings._over_capacity(cfg, total):
                lines.append(_("Tu documentación supera el tope de la búsqueda por significado (unos %(max)s fragmentos con este tamaño de vector): "
                               "Buddy sigue buscando solo por palabras. Dividí o reducí la documentación, o elegí un vector más chico.",
                               max=buddy_vectors.scan_capacity(cfg["dims"] or 1536)))
            elif state.startswith("error:"):
                lines.append(self._buddy_semantic_error_text(state[6:]))
            elif done < total:
                lines.append(_("Indexando en segundo plano: se completa solo en los próximos minutos (mientras tanto Buddy busca por palabras)."))
            if when and not state.startswith("error:"):
                lines.append(_("Última pasada: %(when)s.", when=when))
            text = "\n".join(lines)
        for record in self:
            record.buddy_semantic_status = text

    @api.model
    def _buddy_semantic_error_text(self, code):
        return {
            "invalid_key": _("El proveedor rechazó la clave al calcular los vectores. Revisala en «Probar conexión»."),
            "not_found": _("El proveedor no encontró el modelo de embeddings. Revisá su nombre en «Avanzado»."),
            "rate_limited": _("El proveedor limitó los pedidos (cuota). Buddy reintenta solo en la próxima pasada."),
            "network": _("No pude conectarme al proveedor para calcular los vectores. Buddy reintenta solo."),
            "provider_timeout": _("El proveedor tardó demasiado. Buddy reintenta solo en la próxima pasada."),
            "dimensions": _("El proveedor no respeta el tamaño de vector pedido: poné 0 en «Tamaño del vector» para usar el suyo."),
            "invalid_request": _("El proveedor rechazó varios fragmentos. Revisá el modelo y el tamaño del vector en «Avanzado»."),
        }.get(code, _("El indexado por significado se detuvo por un error del proveedor (%(code)s). Buddy reintenta solo.", code=code))

    @api.depends("buddy_provider")
    def _compute_buddy_model_hint(self):
        for record in self:
            model = default_model(record.buddy_provider)
            record.buddy_model_hint = _("Si lo dejás vacío se usa «%(model)s».", model=model) if model else ""

    @api.depends("buddy_provider", "buddy_chat_model", "buddy_api_base")
    def _compute_buddy_checklist(self):
        parameters = self.env["ir.config_parameter"].sudo()
        sources = self.env["buddy.source"].sudo().search([("active", "=", True), ("group_ids", "!=", False)])
        documents = self.env["buddy.chunk"].sudo().search_count([("source_id", "in", sources.ids)], limit=1) if sources else 0
        ok, when = self._buddy_last_test()
        if when:
            text = _("Última prueba: %(when)s (correcta)", when=when) if ok else _("Última prueba: %(when)s (con problemas)", when=when)
        else:
            text = _("Todavía no probaste la conexión.")
        drive = bool(parameters.get_param(DRIVE_KEY_PARAM))
        # An empty model counts when the provider has a default (same rule as the engine's configuration).
        model = parameters.get_param("buddy_ia.chat_model") or default_model(parameters.get_param("buddy_ia.provider") or "anthropic")
        ai = bool(parameters.get_param(API_KEY_PARAM) and model)
        contacts = self.env["buddy.contact"].sudo().search([])
        sample = contacts.filtered(lambda contact: is_placeholder_email(contact.email))
        if not contacts:
            contacts_text = _("No hay contactos: el buddy no tiene a quién derivar.")
        elif sample:
            contacts_text = _("Faltan los correos reales de: %(labels)s.", labels=", ".join(sample.mapped("label")))
        else:
            contacts_text = _("Todos los contactos tienen un correo real.")
        detail = str(parameters.get_param(LAST_TEST_DETAIL_PARAM) or "")
        for record in self:
            record.buddy_ready_ai = ai
            record.buddy_ready_drive = drive
            record.buddy_ready_source = bool(sources)
            record.buddy_ready_documents = bool(documents)
            record.buddy_ready_contacts = bool(contacts) and not sample
            record.buddy_contacts_text = contacts_text
            record.buddy_ready_test = bool(ok)
            record.buddy_last_test_text = text
            record.buddy_last_test_detail = detail

    @api.depends("buddy_price_in", "buddy_price_out")
    def _compute_buddy_usage_text(self):
        insight = self.env["buddy.insight"]
        failed, last_error = insight._recent_failures(fields.Datetime.now() - timedelta(days=1))
        if failed == 1:
            failures = _("1 consulta falló en las últimas 24 horas (último error: %(error)s). Revisá la conexión con «Probar conexión».",
                         error=last_error)
        elif failed:
            failures = _("%(count)s consultas fallaron en las últimas 24 horas (último error: %(error)s). Revisá la conexión con «Probar conexión».",
                         count=failed, error=last_error)
        else:
            failures = ""
        count, tokens_in, tokens_out, cost = insight._usage_summary(fields.Datetime.now() - timedelta(days=30))
        if not count:
            text = _("Todavía no hay consultas registradas.")
        else:
            text = _("Consultas: %(count)s · tokens de entrada: %(tokens_in)s · tokens de salida: %(tokens_out)s", count=count,
                     tokens_in=tokens_in, tokens_out=tokens_out)
            parameters = self.env["ir.config_parameter"].sudo()
            if float(parameters.get_param("buddy_ia.price_in_per_m") or 0) or float(parameters.get_param("buddy_ia.price_out_per_m") or 0):
                text += " · " + _("costo estimado USD %(cost).2f", cost=cost)
        for record in self:
            record.buddy_usage_text = text
            record.buddy_failures_text = failures

    @api.model
    def _buddy_last_test(self):
        """(passed, timestamp) of the last diagnostic, or (False, '') when none was recorded."""
        raw = self.env["ir.config_parameter"].sudo().get_param(LAST_TEST_PARAM) or ""
        status, _sep, when = str(raw).partition("|")
        return status == "ok", when

    def _buddy_require_admin(self):
        if not (self.env.su or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el administrador puede probar la conexión de Buddy."))

    def action_buddy_test_connection(self):
        self._buddy_require_admin()
        recorded = True
        try:
            ok, lines = self.env["buddy.engine"]._self_test()
        except BuddyBusy as error:
            ok, recorded = False, False  # throttled or busy: nothing was actually tested
            lines = [_("Esperá un minuto antes de volver a probar.") if error.code == "rate_limited"
                     else _("Revisá y guardá la configuración antes de probar la conexión.")]
        if recorded:
            parameters = self.env["ir.config_parameter"].sudo()
            parameters.set_param(LAST_TEST_PARAM, "%s|%s" % ("ok" if ok else "fail", fields.Datetime.now()))
            parameters.set_param(LAST_TEST_DETAIL_PARAM, "\n".join(lines)[:LAST_TEST_DETAIL_LIMIT] or False)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Buddy IA: conexión correcta") if ok else _("Buddy IA: conexión con problemas"),
                       "message": " · ".join(lines), "type": "success" if ok else "danger", "sticky": not ok,
                       # Refresh the checklist without losing the notification.
                       "next": {"type": "ir.actions.client", "tag": "soft_reload"}},
        }

    def action_buddy_delete_all_conversations(self):
        """Privacy: erase every employee's conversation and start every session over (administrators only)."""
        if not (self.env.su or self.env.user.has_group("base.group_system")):
            raise AccessError(_("Solo el administrador puede borrar las conversaciones de Buddy."))
        count = self.env["buddy.engine"]._delete_all_conversations()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Buddy IA: conversaciones borradas"),
                       "message": _("Se borraron %(count)s mensajes. Cada persona empieza una conversación nueva.", count=count),
                       "type": "success", "sticky": False},
        }

    def action_buddy_purge_semantic(self):
        """Privacy: erase every vector (the fragments and the documents stay). Administrators only."""
        self._buddy_require_admin()
        count = self.env["buddy.embedding"].sudo()._purge_all()
        _logger.warning("Buddy: el usuario con uid %s borró los vectores de significado (%s)", self.env.uid, count)
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": _("Buddy IA: datos de significado borrados"),
                           "message": _("Se borraron %(count)s vectores. Si la búsqueda por significado sigue activa, se calculan de nuevo "
                                        "(y se vuelve a enviar el texto al proveedor).", count=count),
                           "type": "success", "sticky": False,
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    def action_buddy_open_sources(self):
        self._buddy_require_admin()
        return self.env["ir.actions.act_window"]._for_xml_id("buddy_ia.buddy_source_action")

    def action_buddy_open_contacts(self):
        self._buddy_require_admin()
        return self.env["ir.actions.act_window"]._for_xml_id("buddy_ia.buddy_contact_action")

    def action_buddy_open_unanswered(self):
        self._buddy_require_admin()
        return self.env["ir.actions.act_window"]._for_xml_id("buddy_ia.buddy_insight_unanswered_action")

    def action_buddy_open_digests(self):
        self._buddy_require_admin()
        return self.env["ir.actions.act_window"]._for_xml_id("buddy_ia.buddy_digest_action")

    def action_buddy_digest_now(self):
        """The weekly summary of today, right now (administrators only). It does not notify anybody: whoever asks is looking at it."""
        self._buddy_require_admin()
        digest = self.env["buddy.digest"]._generate_now()
        return {"type": "ir.actions.act_window", "res_model": "buddy.digest", "res_id": digest.id, "view_mode": "form", "target": "current",
                "name": _("Resumen semanal")}

    def action_buddy_delete_insights(self):
        """Privacy: erase every statistics row and every stored question (administrators only)."""
        self._buddy_require_admin()
        count = self.env["buddy.insight"]._delete_everything()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Buddy IA: estadísticas borradas"),
                       "message": _("Se borraron %(count)s registros de consultas, los votos 👍/👎, los resúmenes semanales y las preguntas guardadas.", count=count),
                       "type": "success", "sticky": False},
        }

    def _buddy_clear(self, key, title, message):
        self._buddy_require_admin()
        self.env["ir.config_parameter"].sudo().set_param(key, False)
        # Says what just happened (and what it means), then refreshes the checklist behind it.
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": title, "message": message, "type": "warning", "sticky": False,
                           "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    def action_buddy_clear_api_key(self):
        return self._buddy_clear(API_KEY_PARAM, _("Clave de API quitada"),
                                 _("El buddy no puede responder hasta que cargues otra clave de API."))

    def action_buddy_clear_drive_key(self):
        return self._buddy_clear(DRIVE_KEY_PARAM, _("Clave de Drive quitada"),
                                 _("Se dejaron de sincronizar las carpetas; lo ya leído se conserva hasta que cargues otra clave."))

    @api.constrains("buddy_name", "buddy_company", "buddy_tagline", "buddy_greeting", "buddy_primary_color",
                    "buddy_suggestion_1", "buddy_suggestion_2", "buddy_suggestion_3", "buddy_suggestion_4",
                    "buddy_extra_instructions", "buddy_chat_model", "buddy_api_base", "buddy_provider",
                    "buddy_api_key_input")
    def _validate_buddy_texts(self):
        for record in self:
            limits = (
                ("buddy_name", 80, _("El nombre del buddy admite hasta %(limit)s caracteres.")),
                ("buddy_company", 160, _("El nombre de la empresa admite hasta %(limit)s caracteres.")),
                ("buddy_tagline", 60, _("El subtítulo admite hasta %(limit)s caracteres.")),
                ("buddy_greeting", 140, _("El saludo admite hasta %(limit)s caracteres.")),
                ("buddy_suggestion_1", 80, _("Cada sugerencia admite hasta %(limit)s caracteres.")),
                ("buddy_suggestion_2", 80, _("Cada sugerencia admite hasta %(limit)s caracteres.")),
                ("buddy_suggestion_3", 80, _("Cada sugerencia admite hasta %(limit)s caracteres.")),
                ("buddy_suggestion_4", 80, _("Cada sugerencia admite hasta %(limit)s caracteres.")),
                ("buddy_extra_instructions", 1500, _("Las instrucciones de la empresa admiten hasta %(limit)s caracteres.")),
                ("buddy_chat_model", 200, _("El nombre del modelo admite hasta %(limit)s caracteres.")),
                ("buddy_api_key_input", 500, _("La clave de API admite hasta %(limit)s caracteres.")),
            )
            for name, limit, message in limits:
                if len(getattr(record, name) or "") > limit:
                    raise ValidationError(message % {"limit": limit})
            color = (record.buddy_primary_color or "").strip()
            if color and not HEX_COLOR.fullmatch(color):
                raise ValidationError(_("El color debe tener el formato #RRGGBB, por ejemplo #2f6df6."))
            if record.buddy_api_key_input and re.search(r"\s", record.buddy_api_key_input.strip()):
                raise ValidationError(_("La clave de API no puede tener espacios ni saltos de línea: copiala de nuevo completa."))
            if record.buddy_provider == "custom" and not valid_base_url((record.buddy_api_base or "").strip()):
                raise ValidationError(_("Para «Otro» cargá la dirección de la API completa, con http:// o https://. No se admiten direcciones internas de metadatos de la nube (169.254.x.x)."))

    @api.constrains("buddy_character", "buddy_accessory", "buddy_idle_frequency")
    def _validate_buddy_look(self):
        """Second wall behind the Selection fields: another module may add options, only Buddy's own ids are valid."""
        for record in self:
            for value, options, message in (
                    (record.buddy_character, CHARACTERS, _("Ese personaje no existe. Elegí uno de la lista.")),
                    (record.buddy_accessory, ACCESSORIES, _("Ese accesorio no existe. Elegí uno de la lista.")),
                    (record.buddy_idle_frequency, FREQUENCIES, _("Esa frecuencia de gestos no existe. Elegí Poco, Normal o Mucho."))):
                if value not in ids(options):
                    raise ValidationError(message)

    @api.constrains("buddy_insight_days", "buddy_price_in", "buddy_price_out", "buddy_max_daily_total", "buddy_max_daily_cost")
    def _validate_buddy_costs(self):
        for record in self:
            if not 1 <= record.buddy_insight_days <= MAX_RETENTION_DAYS:
                raise ValidationError(_("Las estadísticas se pueden conservar de 1 a %(max)s días.", max=MAX_RETENTION_DAYS))
            if not 1 <= record.buddy_max_daily_total <= MAX_DAILY_TOTAL:
                raise ValidationError(_("El tope diario de consultas de toda la empresa va de 1 a %(max)s.", max=MAX_DAILY_TOTAL))
            if any(not 0 <= value <= 1_000_000 for value in (record.buddy_price_in, record.buddy_price_out, record.buddy_max_daily_cost)):
                raise ValidationError(_("Los precios y el tope de costo no pueden ser negativos ni superar un millón."))
            if record.buddy_max_daily_cost and not (record.buddy_price_in or record.buddy_price_out):
                raise ValidationError(_("Para limitar por costo cargá antes los precios por millón de tokens: sin ellos el costo siempre da 0."))

    @api.constrains("buddy_embedding_model", "buddy_embedding_dims")
    def _validate_buddy_semantic(self):
        for record in self:
            model = (record.buddy_embedding_model or "").strip()
            if len(model) > 200 or re.search(r"\s", model):
                raise ValidationError(_("El modelo de embeddings es un nombre sin espacios, de hasta 200 caracteres."))
            dims = record.buddy_embedding_dims or 0
            if dims and not buddy_vectors.MIN_DIMS <= dims <= buddy_vectors.MAX_DIMS:
                raise ValidationError(_("El tamaño del vector es 0 (el de fábrica) o un número entre %(min)s y %(max)s.",
                                        min=buddy_vectors.MIN_DIMS, max=buddy_vectors.MAX_DIMS))

    @api.constrains("buddy_synonyms")
    def _validate_buddy_synonyms(self):
        for record in self:
            try:
                validate_synonyms(record.buddy_synonyms or "")
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

    @api.constrains("buddy_top_k", "buddy_retention_days", "buddy_rate_per_minute", "buddy_rate_per_day",
                    "buddy_new_user_turns", "buddy_sync_hours", "buddy_max_concurrent")
    def _validate_buddy_settings(self):
        for record in self:
            if not 1 <= record.buddy_max_concurrent <= 100:
                raise ValidationError(_("Las consultas simultáneas pueden ser de 1 a 100."))
            if not 1 <= record.buddy_top_k <= 10:
                raise ValidationError(_("Elegí entre 1 y 10 fragmentos por consulta."))
            if any(value < 0 for value in (record.buddy_retention_days, record.buddy_new_user_turns)):
                raise ValidationError(_("La retención y los turnos iniciales no pueden ser negativos."))
            # 0 desactivaba el límite sin avisar y dejaba el gasto del proveedor sin freno.
            if record.buddy_rate_per_minute < 1 or record.buddy_rate_per_day < 1:
                raise ValidationError(_("Los límites de consultas por minuto y por día deben ser de al menos 1."))
            if record.buddy_rate_per_minute > 1000 or record.buddy_rate_per_day > 100000 or record.buddy_new_user_turns > 10000:
                raise ValidationError(_("Máximos: 1000 consultas por minuto, 100000 por día y 10000 turnos iniciales."))
            if not 1 <= record.buddy_sync_hours <= 168:
                raise ValidationError(_("La sincronización con Drive puede repetirse cada 1 a 168 horas."))
