"""Authenticated, typed bridge between the widget and Buddy's private engine."""
import json
import logging
import re
from uuid import UUID

from odoo import http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import Response, request

from ..models.buddy_engine import BuddyBusy
from ..models.buddy_privacy import QUESTIONS_PARAM, flag
from ..models.buddy_journey_logic import RateWindow
from ..models.buddy_look import public_look
from ..models.buddy_proactive_logic import CHIPS_PARAM, NUDGE_PARAM, switch_on
from ..models.buddy_text import strip_control_chars

_logger = logging.getLogger(__name__)

# Public (non secret) appearance settings, with the same limits the settings screen enforces.
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
NAME_LIMIT, TAGLINE_LIMIT, GREETING_LIMIT, SUGGESTION_LIMIT = 80, 60, 140, 80
SUGGESTION_SLOTS = 4


def _assistant_and_contact():
    """The configured name of the assistant and the label of the «general» contact, for messages that name them."""
    env = request.env
    name = _clean(env["ir.config_parameter"].sudo().get_param("buddy_ia.buddy_name"), NAME_LIMIT) or "Buddy"
    contact = env["buddy.contact"].sudo().search([("code", "=", "general")], limit=1)
    return name, (contact.label if contact else "") or env._("la mesa de ayuda")


def _friendly_messages():
    """Readable, translated messages for every engine error code (built per call: needs the user's language).

    A streamed answer is produced after the web request ended, when `request` no longer exists: the streaming
    route builds this table up front and the generator only looks things up.
    """
    _ = request.env._
    name, contact = _assistant_and_contact()
    admin = request.env.user.has_group("base.group_system")  # an administrator is told where to fix it, not to ask themselves
    return {
        "rate_limited": _("Estás consultando muy seguido. Esperá un momento y probá de nuevo."),
        # Waiting a minute does not help with these two: the messages say what to do instead (and the widget offers no retry).
        "daily_limit": _("%(name)s llegó al máximo de consultas que podés hacer por hoy. En unas horas se va a liberar y vas a poder volver a preguntar. "
                         "Si es urgente, escribile a %(contact)s o creá un ticket desde el menú Buddy → Tickets.", name=name, contact=contact),
        "system_limit": _("%(name)s alcanzó el máximo de consultas de toda la empresa por hoy. Se va a liberar en unas horas (el tope se cuenta en una ventana móvil de 24 horas). "
                          "Si es urgente, escribile a %(contact)s o creá un ticket desde el menú Buddy → Tickets.", name=name, contact=contact),
        "provider_error": _("No pude consultar la documentación ahora. Probá de nuevo en un minuto."),
        "provider_timeout": _("La consulta tardó demasiado. Probá de nuevo en un minuto."),
        # The provider refused the key or the permissions: retrying does not help, and only the administrator can fix it.
        "provider_auth": (_("El proveedor de IA rechazó la clave o los permisos: revisalos en Buddy → Configuración.") if admin
                          else _("Ahora no puedo responder. Avisale a quien administra Odoo.")),
        "invalid_message": _("La consulta debe contener entre 1 y 2000 caracteres."),
        "invalid_response": _("No pude validar la respuesta. Probá de nuevo o consultá a la mesa de ayuda."),
        "not_configured": (_("Todavía no estoy configurado: terminá la configuración en Buddy → Configuración.") if admin
                           else _("Todavía no estoy listo. Avisale a quien administra Odoo.")),
        "invalid_request": _("No pude entender el pedido (puede que la página esté desactualizada). Recargá la página y probá de nuevo."),
        "session_changed": _("La conversación o tus permisos cambiaron. Volvé a enviar tu consulta."),
        "busy": _("Estoy atendiendo muchas consultas a la vez (o todavía sigo con otra tuya). Esperá unos segundos y probá de nuevo."),
        "request_pending": _("Esa consulta ya está en proceso. Esperá a que termine."),
        "feedback_rate": _("Estás votando muy seguido. Esperá un momento y probá de nuevo."),
        "feedback_unknown": _("No pude guardar tu opinión: esa respuesta ya no está (la conversación cambió). Probá con una respuesta nueva."),
        "journey_rate": _("Estás tocando muy seguido. Esperá un momento y probá de nuevo."),
        "journey_unknown": _("Esa tarea ya no está disponible (puede que tu recorrido haya cambiado). Ya actualicé tu lista."),
        "suggestions_rate": _("Estás pidiendo sugerencias muy seguido. Esperá un momento y probá de nuevo."),
    }


def _friendly(code, messages=None):
    messages = messages or _friendly_messages()
    return messages.get(code) or messages["provider_error"]


def _engine():
    if not request.env.user.has_group("base.group_user"):
        raise AccessError(request.env._("Buddy está disponible para usuarios internos."))
    return request.env["buddy.engine"]


def _text(value, limit, required=False):
    if not isinstance(value, str):
        raise ValueError("text")
    # NUL and other control characters would be a server error in PostgreSQL (or noise in the prompt).
    value = strip_control_chars(value).strip()
    if (required and not value) or len(value) > limit:
        raise ValueError("text")
    return value


def _generation(value):
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError("generation")
    return value


def _key(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 36:
        raise ValueError("key")
    return str(UUID(value))


def _clean(value, limit):
    """Single-line text capped to `limit`; anything that is not text is dropped."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def public_config(get_param):
    """Appearance values the widget may know. Never include secrets or internal prompts here."""
    suggestions = [text for text in (_clean(get_param("buddy_ia.suggestion_%d" % slot), SUGGESTION_LIMIT)
                                     for slot in range(1, SUGGESTION_SLOTS + 1)) if text]
    color = get_param("buddy_ia.primary_color")
    hidden = str(get_param("buddy_ia.hidden") or "").strip().lower() in ("true", "1")
    result = {
        "buddy_name": _clean(get_param("buddy_ia.buddy_name"), NAME_LIMIT) or "Buddy",
        "enabled": not hidden,
        "greeting": _clean(get_param("buddy_ia.greeting"), GREETING_LIMIT),
        "tagline": _clean(get_param("buddy_ia.tagline"), TAGLINE_LIMIT),
        "suggestions": suggestions,
        "company": _clean(get_param("buddy_ia.company"), 160),
        "primary_color": color.lower() if isinstance(color, str) and HEX_COLOR.fullmatch(color) else "",
        # Whether the administrator keeps every question: the vote form then does not offer «include my question».
        "keep_questions": flag(get_param(QUESTIONS_PARAM)),
        # The two switches of the gentle proactivity (on unless the administrator turned them off): whether the widget asks for
        # suggestions that fit the person and whether it may tell them about their tasks with a small bubble.
        "proactive": {"suggestions": switch_on(get_param(CHIPS_PARAM)), "nudge": switch_on(get_param(NUDGE_PARAM))},
    }
    # Character, accessory and idle gestures: only ids from the whitelists of models/buddy_look.py, never free text.
    result.update(public_look(get_param))
    return result


def _bad_request_key(error):
    """The message for a request refused by the validation: a text of the wrong length, or an invalid id or generation."""
    return "invalid_message" if error.args and error.args[0] == "text" else "invalid_request"


def _failure(code, engine=None, messages=None, generation=None, text=None):
    result = {"error": code, "answer": _friendly(text or code, messages),
              "expression": "doubt", "ring": "doubt", "sources": [], "ticket": None,
              "contact": None, "provider": "llm"}
    if generation is not None:
        result["session_generation"] = generation
    elif code == "session_changed" and engine is not None:
        try:
            result["session_generation"] = engine._session_generation()
        except BuddyBusy:
            # The widget refreshes config when no generation is available.
            pass
    return result


def sse_frame(event, payload):
    """One Server-Sent Event. ASCII-only JSON: no raw newline can end the frame early and no lone surrogate can
    break the UTF-8 encoding."""
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))).encode("ascii")


MAX_STREAM_BODY = 8192
# The JSON-RPC routes carry at most a 2000-character question (or a ticket) in an envelope: 32 KiB is far above any real
# request and far below the 128 MiB that Odoo accepts by default before the controller runs.
MAX_JSON_BODY = 32768
# The body of a vote is a token, a word and two small fields: anything bigger is not a vote.
MAX_FEEDBACK_BODY = 1024
FEEDBACK_KEYS = frozenset({"token", "vote", "reason", "include_question"})
# Onboarding: asking for the tasks carries nothing at all («{}»); ticking a step carries the id of the row and the wanted state.
MAX_JOURNEY_BODY = 256
JOURNEY_KEYS = frozenset()
JOURNEY_STEP_KEYS = frozenset({"id", "done"})
# At most this many requests for the list per person and minute in each process (the widget asks once when it opens and
# once more when the panel does; the engine also limits the ticks with the database).
_JOURNEY_READS = RateWindow(30, 60)
# The chips: asking for the suggestions carries nothing at all («{}»); at most 20 requests per person and minute in each process
# (the widget asks once per page and again, at most every half hour, when the panel opens).
MAX_SUGGESTIONS_BODY = 256
SUGGESTION_KEYS = frozenset()
_SUGGESTION_READS = RateWindow(20, 60)


def _read_body(httprequest, limit=MAX_STREAM_BODY):
    """The request body, never more than `limit` + 1 bytes (the extra byte tells the caller it was too long).

    A chunked upload has no Content-Length to check beforehand. Odoo's request wrapper does not expose `stream`, but it
    does let `max_content_length` through: werkzeug then cuts the body at that size (and refuses a declared length above
    it), so `get_data()` cannot be made to buffer a huge body.
    """
    httprequest.max_content_length = limit + 1
    try:
        return httprequest.get_data()
    except Exception as exc:  # werkzeug's RequestEntityTooLarge (413), without importing werkzeug here
        if getattr(exc, "code", None) == 413:
            return b"x" * (limit + 1)
        raise


class _StreamBody:
    """The body of the SSE response. Runs after the route returned: it must not touch `request`.

    A class and not a generator on purpose: when the browser leaves before the server starts reading the body, a
    generator that was never started has no `finally` to run, and the question's reservation would stay blocked until
    its lease ran out. `close()` (which the web server always calls) releases it in that case, and closes the engine's
    generator (which releases it itself) when it did start.
    """

    def __init__(self, engine, plan, messages):
        self.engine, self.plan, self.messages = engine, plan, messages
        self.events = engine.stream_events(plan)
        self.opened = self.started = self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        if not self.opened:
            self.opened = True
            return b": open\n\n"  # sends the headers at once and proves to the browser (and any proxy) that bytes flow
        self.started = True
        event, payload = next(self.events)
        if event == "error":
            payload = _failure(payload["error"], messages=self.messages, generation=payload.get("session_generation"))
        return sse_frame(event, payload)

    def close(self):
        if self.closed:
            return
        self.closed = True
        close = getattr(self.events, "close", None)
        if close:
            close()  # a generator that did start releases its own reservation (GeneratorExit)
        if not self.started:
            self.engine._release_unstarted(self.plan)


def _stream_frames(engine, plan, messages):
    return _StreamBody(engine, plan, messages)


class BuddyController(http.Controller):
    @http.route("/buddy/config", type="json", auth="user", methods=["POST"], max_content_length=MAX_JSON_BODY)
    def config(self):
        engine = _engine()
        name = request.env.user.name or ""
        result = public_config(request.env["ir.config_parameter"].sudo().get_param)
        result["first_name"] = name.split(" ")[0] if name else ""
        # Whether the AI side is set up (key and model): the widget then says so clearly instead of failing on the first question.
        result["configured"] = bool(engine._configured())
        # Only so the widget can offer the administrator the button that finishes the setup (it is not a permission:
        # the settings screen and every action behind it check the right again).
        result["is_admin"] = bool(request.env.user.has_group("base.group_system"))
        try:
            result["session_generation"] = engine._session_generation()
        except BuddyBusy as busy:
            # Keep the appearance values: the widget still renders and shows the friendly message on send.
            result.update(_failure(busy.code))
        return result

    @http.route("/buddy/chat", type="json", auth="user", methods=["POST"], max_content_length=MAX_JSON_BODY)
    def chat(self, message="", session_generation=None, request_id=None, **kwargs):
        engine = _engine()
        try:
            message = _text(message, 2000, required=True)
            session_generation, request_id = _generation(session_generation), _key(request_id)
        except ValueError as refusal:
            return {"error": "invalid_request", "answer": _friendly(_bad_request_key(refusal)),
                    "expression": "doubt", "ring": "doubt", "sources": [], "ticket": None, "contact": None}
        try:
            return engine.chat(message, session_generation=session_generation, request_id=request_id)
        except BuddyBusy as busy:
            return _failure(busy.code, engine)

    @http.route("/buddy/chat_stream", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_STREAM_BODY)
    def chat_stream(self, **kwargs):
        """Same as /buddy/chat but the answer is streamed as text/event-stream (events delta, done and error).

        CSRF: this is a plain HTTP route with a JSON body. A cross-site page can only send "simple" requests
        (forms: urlencoded, multipart or text/plain), so (1) Content-Type must be application/json, which forces a
        CORS preflight that Odoo never grants, and (2) Odoo's own CSRF token must come in the X-CSRF-Token header
        (a header, so it never lands in URLs or logs). Odoo's automatic check looks at form/query parameters
        only, hence csrf=False here and the explicit validation below.
        """
        engine = _engine()  # internal users only (403 otherwise)
        httprequest = request.httprequest
        if httprequest.mimetype != "application/json":
            return Response("Content-Type must be application/json", status=415, content_type="text/plain")
        if not request.validate_csrf(httprequest.headers.get("X-CSRF-Token")):
            return Response("Invalid CSRF token", status=400, content_type="text/plain")
        messages = _friendly_messages()
        if (httprequest.content_length or 0) > MAX_STREAM_BODY:
            return self._sse_response(iter([("error", _failure("invalid_message", messages=messages))]))
        try:
            raw = _read_body(httprequest)
            if len(raw) > MAX_STREAM_BODY:
                raise ValueError("body")
            data = json.loads(raw.decode("utf-8") or "null")
            if not isinstance(data, dict):
                raise ValueError("body")
            message = _text(data.get("message", ""), 2000, required=True)
            session_generation, request_id = _generation(data.get("session_generation")), _key(data.get("request_id"))
        except (ValueError, RecursionError) as refusal:  # not JSON, not UTF-8, absurdly nested, wrong types or too long
            return self._sse_response(iter([("error", _failure("invalid_message", messages=messages, text=_bad_request_key(refusal)))]))
        try:
            plan = engine.stream_prepare(message, session_generation=session_generation, request_id=request_id)
        except BuddyBusy as busy:
            return self._sse_response(iter([("error", _failure(busy.code, engine, messages))]))
        return Response(_stream_frames(engine, plan, messages), status=200,
                        headers=self._sse_headers(), direct_passthrough=True)

    @staticmethod
    def _sse_headers():
        # no-cache + X-Accel-Buffering: nginx must not hold the stream back (see docs/INSTALL.md).
        return [("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-cache, no-transform"),
                ("X-Accel-Buffering", "no"), ("X-Content-Type-Options", "nosniff")]

    def _sse_response(self, events):
        """A short stream (one error event) for failures that happen before any model work."""
        body = b"".join(sse_frame(event, payload) for event, payload in events)
        return Response(body, status=200, headers=self._sse_headers())

    @http.route("/buddy/feedback", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_FEEDBACK_BODY)
    def feedback(self, **kwargs):
        """The 👍 / 👎 of an answer. A plain HTTP route with a JSON body, protected like /buddy/chat_stream: Content-Type
        must be application/json (a cross-site form cannot send it without a CORS preflight that Odoo never grants) and
        Odoo's CSRF token must come in the X-CSRF-Token header. Internal users only; the body is capped by the route itself
        (`max_content_length`, applied before Odoo reads any parameter: werkzeug then cuts even a chunked upload that
        declares no length) and read again with a cap here;
        the engine validates every value against closed lists, checks that the answer is the caller's own (token and
        user) and applies its own rate limit. Answers `{"ok": true, "vote": ...}` or `{"ok": false, "error", "message"}`.
        """
        engine = _engine()  # internal users only (403 otherwise)
        httprequest = request.httprequest
        if httprequest.mimetype != "application/json":
            return Response("Content-Type must be application/json", status=415, content_type="text/plain")
        if not request.validate_csrf(httprequest.headers.get("X-CSRF-Token")):
            return Response("Invalid CSRF token", status=400, content_type="text/plain")
        if (httprequest.content_length or 0) > MAX_FEEDBACK_BODY:
            return self._json({"ok": False, "error": "invalid_request", "message": _friendly("invalid_request")}, status=413)
        try:
            raw = _read_body(httprequest, MAX_FEEDBACK_BODY)
            if len(raw) > MAX_FEEDBACK_BODY:
                raise ValueError("body")
            data = json.loads(raw.decode("utf-8") or "null")
            if not isinstance(data, dict) or not set(data) <= FEEDBACK_KEYS:
                raise ValueError("body")
            token, vote = data.get("token"), data.get("vote")
            reason, include_question = data.get("reason"), data.get("include_question", False)
            if not isinstance(token, str) or not isinstance(vote, str) or (reason is not None and not isinstance(reason, str))                     or type(include_question) is not bool:
                raise ValueError("body")
        except (ValueError, RecursionError):  # not JSON, not UTF-8, absurdly nested, unknown keys or wrong types
            return self._json({"ok": False, "error": "invalid_request", "message": _friendly("invalid_request")}, status=400)
        try:
            result = engine.feedback(token, vote, reason, include_question)
        except BuddyBusy as busy:
            code = {"rate_limited": "feedback_rate"}.get(busy.code, busy.code)
            status = 429 if busy.code == "rate_limited" else 404 if busy.code == "feedback_unknown" else 400
            return self._json({"ok": False, "error": busy.code, "message": _friendly(code)}, status=status)
        return self._json({"ok": True, "vote": result["vote"]})

    @staticmethod
    def _json(payload, status=200):
        return Response(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), status=status,
                        headers=[("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store"),
                                 ("X-Content-Type-Options", "nosniff")])

    def _json_post(self, limit, keys):
        """The checks every JSON route with a body shares (the same as /buddy/chat_stream and /buddy/feedback): JSON content
        type (a cross-site form cannot send it without a CORS preflight that Odoo never grants), Odoo's CSRF token in the
        X-CSRF-Token header, a body of at most `limit` bytes (declared or chunked) and only the allowed keys. Returns
        `(data, None)` or `(None, response)`."""
        httprequest = request.httprequest
        if httprequest.mimetype != "application/json":
            return None, Response("Content-Type must be application/json", status=415, content_type="text/plain")
        if not request.validate_csrf(httprequest.headers.get("X-CSRF-Token")):
            return None, Response("Invalid CSRF token", status=400, content_type="text/plain")
        bad = self._json({"ok": False, "error": "invalid_request", "message": _friendly("invalid_request")}, status=400)
        if (httprequest.content_length or 0) > limit:
            return None, self._json({"ok": False, "error": "invalid_request", "message": _friendly("invalid_request")}, status=413)
        try:
            raw = _read_body(httprequest, limit)
            if len(raw) > limit:
                raise ValueError("body")
            data = json.loads(raw.decode("utf-8") or "null")
            if not isinstance(data, dict) or not set(data) <= keys:
                raise ValueError("body")
        except (ValueError, RecursionError):  # not JSON, not UTF-8, absurdly nested, unknown keys
            return None, bad
        return data, None

    @staticmethod
    def _journey_engine():
        if not request.env.user.has_group("base.group_user"):
            raise AccessError(request.env._("Buddy está disponible para usuarios internos."))
        return request.env["buddy.journey.engine"]

    @http.route("/buddy/journey", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_JOURNEY_BODY)
    def journey(self, **kwargs):
        """The person's onboarding tasks for the widget. Reads only the caller's own rows (the engine runs with their rights and
        the record rules do the filtering). Internal users only; no parameters: the body is `{}`."""
        engine = self._journey_engine()  # internal users only (403 otherwise)
        data, refusal = self._json_post(MAX_JOURNEY_BODY, JOURNEY_KEYS)
        if refusal is not None:
            return refusal
        if not _JOURNEY_READS.allow(request.env.uid):
            return self._json({"ok": False, "error": "rate_limited", "message": _friendly("journey_rate")}, status=429)
        return self._json({"ok": True, **engine.tasks()})

    @http.route("/buddy/journey/step", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_JOURNEY_BODY)
    def journey_step(self, **kwargs):
        """Tick (done = true) or untick (done = false) one of the caller's own steps; answers the updated list. The state is sent,
        not «toggle», so repeating a request after a lost reply changes nothing."""
        engine = self._journey_engine()
        data, refusal = self._json_post(MAX_JOURNEY_BODY, JOURNEY_STEP_KEYS)
        if refusal is not None:
            return refusal
        step_id, done = data.get("id"), data.get("done")
        if type(step_id) is not int or step_id < 1 or step_id > 2 ** 31 - 1 or type(done) is not bool:
            return self._json({"ok": False, "error": "invalid_request", "message": _friendly("invalid_request")}, status=400)
        try:
            result = engine.set_done(step_id, done)
        except BuddyBusy as busy:
            code = {"rate_limited": "journey_rate"}.get(busy.code, busy.code)
            status = 429 if busy.code == "rate_limited" else 404 if busy.code == "journey_unknown" else 400
            return self._json({"ok": False, "error": busy.code, "message": _friendly(code)}, status=status)
        return self._json({"ok": True, **result})

    @http.route("/buddy/suggestions", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_SUGGESTIONS_BODY)
    def suggestions(self, **kwargs):
        """The chips that fit the person (the questions of their onboarding steps and the topics the company asks about). Reads only
        what the person can read: the engine runs with their rights. Internal users only; no parameters: the body is `{}`. No AI call."""
        if not request.env.user.has_group("base.group_user"):
            raise AccessError(request.env._("Buddy está disponible para usuarios internos."))
        data, refusal = self._json_post(MAX_SUGGESTIONS_BODY, SUGGESTION_KEYS)
        if refusal is not None:
            return refusal
        if not _SUGGESTION_READS.allow(request.env.uid):
            return self._json({"ok": False, "error": "rate_limited", "message": _friendly("suggestions_rate")}, status=429)
        return self._json({"ok": True, **request.env["buddy.proactive"].suggestions()})

    @http.route("/buddy/ticket", type="json", auth="user", methods=["POST"], max_content_length=MAX_JSON_BODY)
    def ticket(self, title="", description="", request_key=None, session_generation=None, **kwargs):
        engine = _engine()
        try:
            title, description = _text(title, 120, required=True), _text(description, 2000)
            request_key, session_generation = _key(request_key), _generation(session_generation)
        except ValueError:
            return {"error": "invalid_request"}
        try:
            return engine._create_ticket(title, description, request_key, session_generation)
        except BuddyBusy as busy:
            return _failure(busy.code, engine)
        except ValidationError as refusal:
            # A reason made for people (the daily cap of tickets, a limit): the widget shows it instead of a generic failure.
            return {"error": "ticket_rejected", "message": str(refusal.args[0]) if refusal.args else ""}

    @http.route("/buddy/history", type="json", auth="user", methods=["POST"], max_content_length=MAX_JSON_BODY)
    def history(self, **kwargs):
        """The user's current conversation (last messages), so the widget does not start from scratch on every page.

        Cheap (no AI call, no quota). Same checks as the other routes: internal users only; the engine reads the
        private message table filtering by the session user and returns only validated fields.
        """
        engine = _engine()
        try:
            return engine.history()
        except BuddyBusy as busy:
            return _failure(busy.code)

    @http.route("/buddy/forget", type="json", auth="user", methods=["POST"], max_content_length=MAX_JSON_BODY)
    def forget(self, **kwargs):
        engine = _engine()
        try:
            return {"ok": True, "session_generation": engine.forget()}
        except BuddyBusy as busy:
            return {"ok": False, **_failure(busy.code)}
