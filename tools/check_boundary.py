import ast
import datetime
import io
import json
import re
"""Exercise the real controllers/model guards with a small in-memory ORM.

No Odoo server, database permissions or SQL concurrency is simulated here.
Those are covered by the native Odoo suite, which must run on the target server.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / "odoo-addons" / "buddy_ia"
sys.dont_write_bytecode = True


class Rows(list):
    def __or__(self, other):
        return Rows(self + [row for row in other if row not in self])

    def __getattr__(self, name):
        return getattr(self[0], name)


class Model:
    def browse(self):
        return Rows()

    def search(self, domain, limit=None):
        match = dict((name, value) for name, op, value in domain)
        return Rows(row for row in self.records if all(getattr(row, key) == value for key, value in match.items()))

    def sudo(self):
        return self

    def search_count(self, domain):
        match = dict((name, value) for name, op, value in domain if op == "=")
        return len([row for row in self.records if all(getattr(row, key) == value for key, value in match.items())])

    def create(self, values):
        rows = Rows(types.SimpleNamespace(id=len(self.records) + index + 1, _buddy_notify_responsible=lambda: None, **value)
                    for index, value in enumerate(values))
        self.records.extend(rows)
        return rows


class User:
    name = "Ana Prueba"

    def __init__(self, internal=True, admin=False):
        self.internal, self.admin = internal, admin

    def has_group(self, group):
        return self.admin if group == "base.group_system" else self.internal


class Params:
    def __init__(self, **values):
        self.values = values

    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return self.values.get(key, default)

    def set_param(self, key, value):
        self.values[key] = value


class NoContacts:
    """buddy.contact with no rows: the messages that name the «general» contact fall back to a generic label."""

    def sudo(self):
        return self

    def search(self, domain, limit=None):
        return Rows()


class Env(dict):
    uid, su = 7, False

    def __missing__(self, key):
        # Models the controller reads for every request (the assistant's name, the contact label): empty by default.
        if key == "ir.config_parameter":
            return Params()
        if key == "buddy.contact":
            return NoContacts()
        if key == "buddy.digest":
            return Mock()  # turning the weekly summary off closes its notice (tests/test_digest.py checks it for real)
        raise KeyError(key)

    def __init__(self, user=None, **values):
        super().__init__(values)
        self.user = user or User()

    def _(self, source, *args, **kwargs):
        """Stand-in for env._(): the real one translates, then interpolates."""
        return source % (kwargs or args) if (args or kwargs) else source


odoo = types.ModuleType("odoo")
odoo.api = types.SimpleNamespace(model=lambda fn: fn, model_create_multi=lambda fn: fn,
                                 constrains=lambda *args: lambda fn: fn, depends=lambda *args: lambda fn: fn,
                                 depends_context=lambda *args: lambda fn: fn, private=lambda fn: fn,
                                 onchange=lambda *args: lambda fn: fn)
odoo.SUPERUSER_ID = 1
odoo.fields = types.SimpleNamespace(**{name: lambda *args, **kwargs: None
                                      for name in ("Char", "Text", "Many2one", "Many2many", "One2many", "Selection", "Integer", "Float", "Boolean", "Binary")})
odoo._ = lambda source, *args, **kwargs: source % (kwargs or args) if (args or kwargs) else source
odoo.fields.Datetime = type("Datetime", (), {"now": staticmethod(datetime.datetime.now), "__new__": lambda cls, *args, **kwargs: None})
odoo.models = types.SimpleNamespace(Model=Model, TransientModel=object, AbstractModel=object)
odoo.http = types.ModuleType("odoo.http")
odoo.http.Controller = object
odoo.http.route = lambda *args, **kwargs: lambda fn: fn
odoo.http.request = types.SimpleNamespace(env=None)


class FakeResponse:
    """odoo.http.Response: keeps what the route passed (body or generator, status, headers)."""

    def __init__(self, response=None, status=200, headers=None, content_type=None, direct_passthrough=False):
        self.response, self.status, self.direct_passthrough = response, status, direct_passthrough
        self.headers = dict(headers or [])
        if content_type:
            self.headers["Content-Type"] = content_type


odoo.http.Response = FakeResponse
exceptions = types.ModuleType("odoo.exceptions")
exceptions.AccessError = type("AccessError", (Exception,), {})
exceptions.ValidationError = type("ValidationError", (Exception,), {})
exceptions.UserError = type("UserError", (Exception,), {})
engine_module = types.ModuleType("boundary.models.buddy_engine")


class BuddyBusy(Exception):
    def __init__(self, code):
        self.code = code


engine_module.BuddyBusy = BuddyBusy
engine_module.SYNONYMS_PARAM = "buddy_ia.synonyms"
engine_module.MAX_DAILY_TOTAL = 1_000_000
odoo.tools = types.ModuleType("odoo.tools")
odoo.tools.sql = types.ModuleType("odoo.tools.sql")
odoo.tools.SQL = lambda *parts: parts
odoo.tools.sql.create_index = lambda *args, **kwargs: None
sys.modules.update({"odoo": odoo, "odoo.http": odoo.http, "odoo.exceptions": exceptions,
                    "odoo.tools": odoo.tools, "odoo.tools.sql": odoo.tools.sql,
                    "boundary.models.buddy_engine": engine_module})
for name in ("boundary", "boundary.models", "boundary.controllers"):
    module = types.ModuleType(name)
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("boundary.models.buddy_text", "models/buddy_text.py")
load("boundary.models.buddy_privacy", "models/buddy_privacy.py")
load("boundary.models.buddy_journey_logic", "models/buddy_journey_logic.py")
load("boundary.models.buddy_proactive_logic", "models/buddy_proactive_logic.py")
look = load("boundary.models.buddy_look", "models/buddy_look.py")
controller = load("boundary.controllers.main", "controllers/main.py")
ticket = load("boundary.models.buddy_ticket", "models/buddy_ticket.py")
contact = load("boundary.models.buddy_contact", "models/buddy_contact.py")
insight = load("boundary.models.buddy_insight", "models/buddy_insight.py")
load("boundary.models.buddy_chunker", "models/buddy_chunker.py")
load("boundary.models.drive_client", "models/drive_client.py")
load("boundary.models.buddy_source", "models/buddy_source.py")
load("boundary.models.buddy_stream", "models/buddy_stream.py")
load("boundary.models.buddy_vectors", "models/buddy_vectors.py")
load("boundary.models.buddy_provider", "models/buddy_provider.py")
load("boundary.models.buddy_embedding", "models/buddy_embedding.py")
settings = load("boundary.models.res_config_settings", "models/res_config_settings.py")
KEY = "143d5336-232e-44e8-91d4-767cab21d8d1"


STREAM_TOKEN = "good-csrf-token"


class StreamRouteTests(unittest.TestCase):
    """The streaming route's guards, with the real controller code and a fake engine."""

    def setUp(self):
        self.engine = Mock()
        self.engine.stream_prepare.return_value = {"plan": True}
        self.engine.stream_events.side_effect = lambda plan: iter([("delta", {"text": "Hola"}), ("done", {"answer": "Hola"})])
        odoo.http.request.env = Env(**{"buddy.engine": self.engine})
        self.headers = {"X-CSRF-Token": STREAM_TOKEN}
        odoo.http.request.validate_csrf = lambda token: token == STREAM_TOKEN
        self.set_http("application/json", json.dumps({"message": "hola", "request_id": KEY, "session_generation": 3}))
        self.controller = controller.BuddyController()

    def set_http(self, mimetype, body, length="auto", stream=None):
        """`length` is the declared Content-Length (None: chunked, nothing declared); `stream` is what can be read.

        Like Odoo's request wrapper over werkzeug: no `stream` attribute, `max_content_length` can be set, and
        `get_data()` reads the body cut at that size.
        """
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        source = io.BytesIO(data) if stream is None else stream
        request = types.SimpleNamespace(mimetype=mimetype, headers=self.headers, max_content_length=None,
                                        content_length=len(data) if length == "auto" else length)
        request.get_data = lambda: source.read(request.max_content_length)
        odoo.http.request.httprequest = request

    def frames(self, response):
        body = response.response if isinstance(response.response, (bytes, bytearray)) else b"".join(response.response)
        return body.decode("ascii")

    def test_portal_cannot_reach_the_stream(self):
        odoo.http.request.env.user = User(internal=False)
        with self.assertRaises(exceptions.AccessError):
            self.controller.chat_stream()
        self.engine.stream_prepare.assert_not_called()

    def test_only_json_content_type(self):
        for mimetype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data", "", None):
            self.set_http(mimetype, "{}")
            self.assertEqual(self.controller.chat_stream().status, 415, mimetype)
        self.engine.stream_prepare.assert_not_called()

    def test_csrf_token_is_required_and_checked(self):
        for token in (None, "", "other", STREAM_TOKEN + "x"):
            self.headers.clear()
            if token is not None:
                self.headers["X-CSRF-Token"] = token
            self.assertEqual(self.controller.chat_stream().status, 400, token)
        self.engine.stream_prepare.assert_not_called()

    def test_invalid_bodies_are_an_error_event_and_never_reach_the_engine(self):
        bodies = ["no json", "[]", "null", "5", json.dumps({"message": ""}), json.dumps({"message": 7}),
                  json.dumps({"message": "x" * 2001}), json.dumps({"message": "hola", "session_generation": "3"}),
                  json.dumps({"message": "hola", "session_generation": True}), json.dumps({"message": "hola", "request_id": "x"})]
        for body in bodies:
            self.set_http("application/json", body)
            text = self.frames(self.controller.chat_stream())
            self.assertTrue(text.startswith("event: error\ndata: "), body)
            self.assertIn('"error":"invalid_message"', text)
        self.set_http("application/json", "{}", length=100000)
        self.assertIn("invalid_message", self.frames(self.controller.chat_stream()))
        self.engine.stream_prepare.assert_not_called()

    def test_a_chunked_body_is_read_at_most_one_byte_past_the_limit(self):
        reads = []

        class Endless:
            """A chunked upload with no Content-Length that never ends."""

            def read(self, amount=None):
                reads.append(amount)
                return b"x" * (amount if amount is not None else 10 ** 9)

        self.set_http("application/json", "", length=None, stream=Endless())
        text = self.frames(self.controller.chat_stream())
        self.assertIn('"error":"invalid_message"', text)
        self.assertEqual(reads, [controller.MAX_STREAM_BODY + 1], "one bounded read, never the whole body")
        self.engine.stream_prepare.assert_not_called()

    def test_werkzeug_refusing_a_body_is_an_invalid_message_and_other_failures_are_not_swallowed(self):
        class TooLarge(Exception):
            code = 413

        def refuse():
            raise TooLarge()

        self.set_http("application/json", "{}")
        odoo.http.request.httprequest.get_data = refuse
        self.assertIn("invalid_message", self.frames(self.controller.chat_stream()))
        self.engine.stream_prepare.assert_not_called()

        def broken():
            raise OSError("socket closed")

        odoo.http.request.httprequest.get_data = broken
        with self.assertRaises(OSError):
            self.controller.chat_stream()

    def test_the_body_limit_is_set_on_the_request_before_reading(self):
        self.set_http("application/json", json.dumps({"message": "hola"}))
        self.controller.chat_stream()
        self.assertEqual(odoo.http.request.httprequest.max_content_length, controller.MAX_STREAM_BODY + 1)

    def test_a_body_exactly_at_the_limit_is_accepted_and_one_byte_more_is_not(self):
        pad = "x" * (controller.MAX_STREAM_BODY - len(json.dumps({"message": "hola", "pad": ""})))
        body = json.dumps({"message": "hola", "pad": pad})
        self.assertEqual(len(body), controller.MAX_STREAM_BODY)
        self.set_http("application/json", body, length=None)
        self.controller.chat_stream()
        self.engine.stream_prepare.assert_called_once()
        self.engine.stream_prepare.reset_mock()
        self.set_http("application/json", body + " ", length=None)
        self.assertIn("invalid_message", self.frames(self.controller.chat_stream()))
        self.engine.stream_prepare.assert_not_called()

    def test_deeply_nested_or_non_utf8_bodies_are_invalid_not_a_server_error(self):
        for body in (b"[" * 8000, b'{"a":' * 1500 + b"1" + b"}" * 1500, b'{"message": "\xff\xfe"}', b"\x80"):
            self.set_http("application/json", body, length=None)
            self.assertIn("invalid_message", self.frames(self.controller.chat_stream()), body[:12])
        self.engine.stream_prepare.assert_not_called()

    def test_control_characters_are_removed_from_the_question(self):
        self.set_http("application/json", json.dumps({"message": "ho\u0000la\u0001 \ud800mundo", "request_id": KEY}))
        self.controller.chat_stream()
        self.engine.stream_prepare.assert_called_once_with("hola mundo", session_generation=None, request_id=KEY)
        self.engine.stream_prepare.reset_mock()
        self.set_http("application/json", json.dumps({"message": "\u0000\u0001  "}))
        self.assertIn("invalid_message", self.frames(self.controller.chat_stream()))
        self.engine.stream_prepare.assert_not_called()

    def test_valid_request_prepares_first_and_returns_an_unbuffered_event_stream(self):
        response = self.controller.chat_stream()
        self.engine.stream_prepare.assert_called_once_with("hola", session_generation=3, request_id=KEY)
        self.engine.stream_events.assert_called_once_with({"plan": True})  # a generator: nothing runs until it is read
        self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertTrue(response.direct_passthrough)
        # The web request is over when the body is consumed: the generator must not need `request`.
        odoo.http.request.env, odoo.http.request.httprequest = None, None
        body = b"".join(response.response).decode("ascii")
        self.assertEqual(body, ': open\n\nevent: delta\ndata: {"text":"Hola"}\n\nevent: done\ndata: {"answer":"Hola"}\n\n')

    def test_error_events_carry_the_friendly_message_computed_before_the_stream(self):
        self.engine.stream_events.side_effect = lambda plan: iter([("error", {"error": "session_changed", "session_generation": 9})])
        response = self.controller.chat_stream()
        odoo.http.request.env = None
        text = b"".join(response.response).decode("ascii")
        data = json.loads(text.split("event: error\ndata: ")[1].strip())
        self.assertEqual((data["error"], data["session_generation"], data["ring"]), ("session_changed", 9, "doubt"))
        self.assertTrue(data["answer"])

    def test_preparation_errors_become_one_error_event_with_the_current_generation(self):
        self.engine.stream_prepare.side_effect = BuddyBusy("session_changed")
        self.engine._session_generation.return_value = 5
        text = self.frames(self.controller.chat_stream())
        self.assertEqual(text.count("event:"), 1)
        self.assertIn('"error":"session_changed"', text)
        self.assertIn('"session_generation":5', text)
        self.engine.stream_events.assert_not_called()

    def test_an_unexpected_error_is_not_swallowed(self):
        self.engine.stream_prepare.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            self.controller.chat_stream()

    def test_closing_the_body_before_a_single_byte_was_read_releases_the_reservation(self):
        # The browser left before the server started reading: a generator that never started has no `finally` to run.
        response = self.controller.chat_stream()
        response.response.close()
        self.engine._release_unstarted.assert_called_once_with({"plan": True})
        response.response.close()  # closing twice does not release twice
        self.engine._release_unstarted.assert_called_once()
        self.assertEqual(list(response.response), [], "a closed body yields nothing more")

    def test_closing_the_body_after_reading_closes_the_engine_generator_instead(self):
        closed = []

        class Events:
            def __iter__(self):
                return self

            def __next__(self):
                return ("delta", {"text": "Hola"})

            def close(self):
                closed.append(True)

        self.engine.stream_events.side_effect = lambda plan: Events()
        response = self.controller.chat_stream()
        body = iter(response.response)
        self.assertEqual(next(body), b": open\n\n")
        self.assertIn(b'"text":"Hola"', next(body))
        response.response.close()
        self.assertEqual(closed, [True], "the engine's own cleanup (GeneratorExit) runs")
        self.engine._release_unstarted.assert_not_called()

    def test_a_body_that_only_sent_the_opening_comment_still_counts_as_not_started(self):
        response = self.controller.chat_stream()
        self.assertEqual(next(iter(response.response)), b": open\n\n")
        response.response.close()
        self.engine._release_unstarted.assert_called_once()

    def test_sse_frame_is_ascii_single_event(self):
        frame = controller.sse_frame("delta", {"text": "línea\n  😀 \ud800"})
        frame.decode("ascii")
        self.assertEqual(frame.count(b"\n"), 3)
        self.assertEqual(json.loads(frame.split(b"data: ")[1])["text"], "línea\n  😀 \ud800")


FEEDBACK_TOKEN = "Zk3-mQ9_aB7xLw2PqR5tYu"


class FeedbackRouteTests(unittest.TestCase):
    """/buddy/feedback: the guards of the route (real controller code, fake engine). The engine is checked natively."""

    def setUp(self):
        self.engine = Mock()
        self.engine.feedback.side_effect = lambda token, vote, reason=None, include_question=False: {"vote": None if vote == "none" else vote}
        odoo.http.request.env = Env(**{"buddy.engine": self.engine})
        self.headers = {"X-CSRF-Token": STREAM_TOKEN}
        odoo.http.request.validate_csrf = lambda token: token == STREAM_TOKEN
        self.controller = controller.BuddyController()
        self.post({"token": FEEDBACK_TOKEN, "vote": "up"}, call=False)

    def post(self, body, mimetype="application/json", length="auto", stream=None, call=True):
        data = (body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8"))
        source = io.BytesIO(data) if stream is None else stream
        request = types.SimpleNamespace(mimetype=mimetype, headers=self.headers, max_content_length=None,
                                        content_length=len(data) if length == "auto" else length)
        request.get_data = lambda: source.read(request.max_content_length)
        odoo.http.request.httprequest = request
        return self.controller.feedback() if call else None

    def test_portal_users_cannot_reach_it(self):
        odoo.http.request.env.user = User(internal=False)
        with self.assertRaises(exceptions.AccessError):
            self.post({"token": FEEDBACK_TOKEN, "vote": "up"})
        self.engine.feedback.assert_not_called()

    def test_only_json_content_type(self):
        for mimetype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data", "", None):
            self.assertEqual(self.post({"token": FEEDBACK_TOKEN, "vote": "up"}, mimetype=mimetype).status, 415, mimetype)
        self.engine.feedback.assert_not_called()

    def test_csrf_token_is_required_and_checked(self):
        for token in (None, "", "other", STREAM_TOKEN + "x"):
            self.headers.clear()
            if token is not None:
                self.headers["X-CSRF-Token"] = token
            self.assertEqual(self.post({"token": FEEDBACK_TOKEN, "vote": "up"}).status, 400, token)
        self.engine.feedback.assert_not_called()

    def test_a_valid_vote_reaches_the_engine_with_exactly_the_four_values(self):
        response = self.post({"token": FEEDBACK_TOKEN, "vote": "down", "reason": "wrong", "include_question": True})
        self.engine.feedback.assert_called_once_with(FEEDBACK_TOKEN, "down", "wrong", True)
        self.assertEqual((response.status, json.loads(response.response)), (200, {"ok": True, "vote": "down"}))
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.engine.feedback.reset_mock()
        self.post({"token": FEEDBACK_TOKEN, "vote": "up"})
        self.engine.feedback.assert_called_once_with(FEEDBACK_TOKEN, "up", None, False)
        self.assertEqual(json.loads(self.post({"token": FEEDBACK_TOKEN, "vote": "none"}).response), {"ok": True, "vote": None})

    def test_invalid_bodies_never_reach_the_engine(self):
        bodies = ["no json", "[]", "null", "5", "{}", {"vote": "up"}, {"token": FEEDBACK_TOKEN}, {"token": 5, "vote": "up"},
                  {"token": FEEDBACK_TOKEN, "vote": 1}, {"token": FEEDBACK_TOKEN, "vote": "up", "extra": 1},
                  {"token": FEEDBACK_TOKEN, "vote": "up", "reason": 5}, {"token": FEEDBACK_TOKEN, "vote": "down", "include_question": "true"},
                  {"token": FEEDBACK_TOKEN, "vote": "down", "include_question": 1}, {"token": [FEEDBACK_TOKEN], "vote": "up"}]
        for body in bodies:
            response = self.post(body)
            self.assertEqual(response.status, 400, body)
            payload = json.loads(response.response)
            self.assertEqual((payload["ok"], payload["error"]), (False, "invalid_request"))
            self.assertTrue(payload["message"])
        for raw in (b"[" * 8000, b'{"a":' * 1500 + b"1" + b"}" * 1500, b'{"token": "\xff\xfe"}', b"\x80"):
            self.assertEqual(self.post(raw, length=None).status, 400, raw[:12])
        self.engine.feedback.assert_not_called()

    def test_the_body_is_capped_declared_or_chunked(self):
        self.assertEqual(self.post({"token": FEEDBACK_TOKEN, "vote": "up", "pad": "x" * 2000}).status, 413)
        self.assertEqual(self.post("{}", length=100000).status, 413)
        reads = []

        class Endless:
            def read(self, amount=None):
                reads.append(amount)
                return b"x" * (amount if amount is not None else 10 ** 9)

        self.assertEqual(self.post("", length=None, stream=Endless()).status, 400)
        self.assertEqual(reads, [controller.MAX_FEEDBACK_BODY + 1], "one bounded read, never the whole body")

        class TooLarge(Exception):
            code = 413

        request = odoo.http.request.httprequest

        def refuse():
            raise TooLarge()

        request.get_data = refuse
        self.assertEqual(self.controller.feedback().status, 400)
        self.engine.feedback.assert_not_called()

    def test_a_body_exactly_at_the_limit_is_read_and_one_byte_more_is_not(self):
        pad = "x" * (controller.MAX_FEEDBACK_BODY - len(json.dumps({"token": FEEDBACK_TOKEN, "vote": "up", "reason": ""})))
        body = json.dumps({"token": FEEDBACK_TOKEN, "vote": "up", "reason": pad})
        self.assertEqual(len(body), controller.MAX_FEEDBACK_BODY)
        self.assertEqual(self.post(body, length=None).status, 200)
        self.engine.feedback.assert_called_once()
        self.engine.feedback.reset_mock()
        self.assertEqual(self.post(body + " ", length=None).status, 400)
        self.engine.feedback.assert_not_called()

    def test_the_engines_refusals_become_readable_statuses(self):
        for code, status, key in (("feedback_unknown", 404, "feedback_unknown"), ("rate_limited", 429, "feedback_rate"),
                                  ("invalid_request", 400, "invalid_request"), ("session_changed", 400, "session_changed")):
            self.engine.feedback.side_effect = BuddyBusy(code)
            response = self.post({"token": FEEDBACK_TOKEN, "vote": "up"})
            payload = json.loads(response.response)
            self.assertEqual((response.status, payload["ok"], payload["error"]), (status, False, code))
            self.assertEqual(payload["message"], controller._friendly(key))
            self.assertTrue(payload["message"])
        self.assertIn("votando", controller._friendly("feedback_rate"))

    def test_every_route_of_the_controller_sets_its_own_body_cap(self):
        """Odoo reads the parameters of a request BEFORE the controller runs, and by default accepts 128 MiB: each route says
        how much it really needs (found by the E3 security review)."""
        source = (ROOT / "controllers/main.py").read_text(encoding="utf-8")
        routes = re.findall(r"@http\.route\([^\n]*\)", source)
        self.assertEqual(len(routes), 10, routes)
        for route in routes:
            self.assertIn("max_content_length=", route, route)

    def test_the_route_is_post_only_json_and_without_the_automatic_csrf_check(self):
        source = (ROOT / "controllers/main.py").read_text(encoding="utf-8")
        self.assertIn('@http.route("/buddy/feedback", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_FEEDBACK_BODY)', source)
        self.assertIn('csrf=False, max_content_length=MAX_STREAM_BODY)', source, "the cap is set by the route, before the body is read")
        self.assertIn("request.validate_csrf(", source.split("def feedback(")[1].split("def _json")[0])


class JourneyRouteTests(unittest.TestCase):
    """/buddy/journey and /buddy/journey/step: the guards of the routes (real controller code, fake engine). The engine and the
    record rules are checked natively (tests/test_journey.py)."""

    def setUp(self):
        self.engine = Mock()
        self.engine.tasks.return_value = {"journeys": [], "pending": 0}
        self.engine.set_done.return_value = {"journeys": [], "pending": 0}
        odoo.http.request.env = Env(**{"buddy.journey.engine": self.engine})
        odoo.http.request.env.uid = 7
        self.headers = {"X-CSRF-Token": STREAM_TOKEN}
        odoo.http.request.validate_csrf = lambda token: token == STREAM_TOKEN
        self.controller = controller.BuddyController()
        self.window = controller.RateWindow(1000, 60)
        patcher = unittest.mock.patch.object(controller, "_JOURNEY_READS", self.window)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, route, body, mimetype="application/json", length="auto", stream=None):
        data = (body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8"))
        source = io.BytesIO(data) if stream is None else stream
        request = types.SimpleNamespace(mimetype=mimetype, headers=self.headers, max_content_length=None,
                                        content_length=len(data) if length == "auto" else length)
        request.get_data = lambda: source.read(request.max_content_length)
        odoo.http.request.httprequest = request
        return getattr(self.controller, route)()

    def test_portal_users_cannot_reach_them(self):
        odoo.http.request.env.user = User(internal=False)
        for route in ("journey", "journey_step"):
            with self.assertRaises(exceptions.AccessError):
                self.post(route, {})
        self.engine.tasks.assert_not_called()
        self.engine.set_done.assert_not_called()

    def test_only_json_content_type_and_a_valid_csrf_token(self):
        for route, body in (("journey", {}), ("journey_step", {"id": 3, "done": True})):
            for mimetype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data", "", None):
                self.assertEqual(self.post(route, body, mimetype=mimetype).status, 415, (route, mimetype))
            for token in (None, "", "other", STREAM_TOKEN + "x"):
                self.headers.clear()
                if token is not None:
                    self.headers["X-CSRF-Token"] = token
                self.assertEqual(self.post(route, body).status, 400, (route, token))
            self.headers["X-CSRF-Token"] = STREAM_TOKEN
        self.engine.tasks.assert_not_called()
        self.engine.set_done.assert_not_called()

    def test_the_list_takes_an_empty_object_and_nothing_else(self):
        response = self.post("journey", {})
        self.assertEqual((response.status, json.loads(response.response)), (200, {"ok": True, "journeys": [], "pending": 0}))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.engine.tasks.assert_called_once_with()
        self.engine.tasks.reset_mock()
        for body in ({"user_id": 3}, {"id": 1}, [], "no json", "null", "5", b"\x80"):
            self.assertEqual(self.post("journey", body).status, 400, body)
        self.engine.tasks.assert_not_called()

    def test_a_tick_reaches_the_engine_with_exactly_the_id_and_the_state(self):
        response = self.post("journey_step", {"id": 41, "done": True})
        self.assertEqual((response.status, json.loads(response.response)["ok"]), (200, True))
        self.engine.set_done.assert_called_once_with(41, True)
        self.engine.set_done.reset_mock()
        self.post("journey_step", {"id": 2, "done": False})
        self.engine.set_done.assert_called_once_with(2, False)

    def test_invalid_ticks_never_reach_the_engine(self):
        bodies = [{}, {"id": 1}, {"done": True}, {"id": "1", "done": True}, {"id": True, "done": True}, {"id": 1.0, "done": True}, {"id": 0, "done": True},
                  {"id": -1, "done": True}, {"id": 2 ** 31, "done": True}, {"id": 1, "done": 1}, {"id": 1, "done": "true"}, {"id": 1, "done": None},
                  {"id": 1, "done": True, "user_id": 2}, [1, True], "[", "null", b"\xff\xfe", {"id": [1], "done": True}]
        for body in bodies:
            response = self.post("journey_step", body)
            self.assertEqual(response.status, 400, body)
            payload = json.loads(response.response)
            self.assertEqual((payload["ok"], payload["error"]), (False, "invalid_request"))
            self.assertTrue(payload["message"])
        for raw in (b"[" * 8000, b'{"a":' * 1500 + b"1" + b"}" * 1500):
            self.assertEqual(self.post("journey_step", raw, length=None).status, 400, raw[:12])
        self.engine.set_done.assert_not_called()

    def test_the_body_is_capped_declared_or_chunked(self):
        for route in ("journey", "journey_step"):
            self.assertEqual(self.post(route, {"id": 1, "done": True, "pad": "x" * 1000}).status, 413)
            self.assertEqual(self.post(route, "{}", length=100000).status, 413)
        reads = []

        class Endless:
            def read(self, amount=None):
                reads.append(amount)
                return b"x" * (amount if amount is not None else 10 ** 9)

        self.assertEqual(self.post("journey_step", "", length=None, stream=Endless()).status, 400)
        self.assertEqual(reads, [controller.MAX_JOURNEY_BODY + 1], "one bounded read, never the whole body")
        self.engine.set_done.assert_not_called()

    def test_the_engines_refusals_become_readable_statuses(self):
        for code, status, key in (("journey_unknown", 404, "journey_unknown"), ("rate_limited", 429, "journey_rate"), ("invalid_request", 400, "invalid_request")):
            self.engine.set_done.side_effect = BuddyBusy(code)
            response = self.post("journey_step", {"id": 1, "done": True})
            payload = json.loads(response.response)
            self.assertEqual((response.status, payload["ok"], payload["error"], payload["message"]), (status, False, code, controller._friendly(key)))
            self.assertTrue(payload["message"])

    def test_the_list_is_limited_per_person_and_minute(self):
        self.window.limit = 2
        self.assertEqual([self.post("journey", {}).status for _ in range(3)], [200, 200, 429])
        response = self.post("journey", {})
        self.assertEqual(json.loads(response.response)["error"], "rate_limited")
        odoo.http.request.env.uid = 8
        self.assertEqual(self.post("journey", {}).status, 200, "another person has their own count")

    def test_the_routes_are_post_only_json_and_without_the_automatic_csrf_check(self):
        source = (ROOT / "controllers/main.py").read_text(encoding="utf-8")
        for route in ("/buddy/journey", "/buddy/journey/step"):
            self.assertIn('@http.route("%s", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_JOURNEY_BODY)' % route, source)
        self.assertIn("request.validate_csrf(", source.split("def _json_post(")[1].split("def _journey_engine")[0])


class SuggestionsRouteTests(unittest.TestCase):
    """/buddy/suggestions: the guards of the route (real controller code, fake engine). What the engine returns for each person
    (own steps, topics, anonymity thresholds) is checked natively (tests/test_proactive.py)."""

    def setUp(self):
        self.engine = Mock()
        self.engine.suggestions.return_value = {"suggestions": [{"kind": "topic", "text": "Tema", "label": "Tema"}]}
        odoo.http.request.env = Env(**{"buddy.proactive": self.engine})
        odoo.http.request.env.uid = 7
        self.headers = {"X-CSRF-Token": STREAM_TOKEN}
        odoo.http.request.validate_csrf = lambda token: token == STREAM_TOKEN
        self.controller = controller.BuddyController()
        self.window = controller.RateWindow(1000, 60)
        patcher = unittest.mock.patch.object(controller, "_SUGGESTION_READS", self.window)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, mimetype="application/json", length="auto", stream=None):
        data = (body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode("utf-8"))
        source = io.BytesIO(data) if stream is None else stream
        request = types.SimpleNamespace(mimetype=mimetype, headers=self.headers, max_content_length=None,
                                        content_length=len(data) if length == "auto" else length)
        request.get_data = lambda: source.read(request.max_content_length)
        odoo.http.request.httprequest = request
        return self.controller.suggestions()

    def test_portal_users_cannot_reach_it(self):
        odoo.http.request.env.user = User(internal=False)
        with self.assertRaises(exceptions.AccessError):
            self.post({})
        self.engine.suggestions.assert_not_called()

    def test_only_json_content_type_and_a_valid_csrf_token(self):
        for mimetype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data", "", None):
            self.assertEqual(self.post({}, mimetype=mimetype).status, 415, mimetype)
        for token in (None, "", "other", STREAM_TOKEN + "x"):
            self.headers.clear()
            if token is not None:
                self.headers["X-CSRF-Token"] = token
            self.assertEqual(self.post({}).status, 400, token)
        self.engine.suggestions.assert_not_called()

    def test_it_takes_an_empty_object_and_nothing_else(self):
        response = self.post({})
        self.assertEqual((response.status, json.loads(response.response)["ok"]), (200, True))
        self.assertEqual(json.loads(response.response)["suggestions"][0]["text"], "Tema")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.engine.suggestions.assert_called_once_with()
        self.engine.suggestions.reset_mock()
        for body in ({"user_id": 3}, {"suggestions": []}, [], "no json", "null", "5", b"\x80"):
            self.assertEqual(self.post(body).status, 400, body)
        self.engine.suggestions.assert_not_called()

    def test_the_body_is_capped_declared_or_chunked(self):
        self.assertEqual(self.post({"pad": "x" * 1000}).status, 413)
        self.assertEqual(self.post("{}", length=100000).status, 413)
        reads = []

        class Endless:
            def read(self, amount=None):
                reads.append(amount)
                return b"x" * (amount if amount is not None else 10 ** 9)

        self.assertEqual(self.post("", length=None, stream=Endless()).status, 400)
        self.assertEqual(reads, [controller.MAX_SUGGESTIONS_BODY + 1], "one bounded read, never the whole body")

    def test_it_is_limited_per_person_and_minute(self):
        self.window.limit = 2
        self.assertEqual([self.post({}).status for _ in range(3)], [200, 200, 429])
        self.assertEqual(json.loads(self.post({}).response)["error"], "rate_limited")
        odoo.http.request.env.uid = 8
        self.assertEqual(self.post({}).status, 200, "another person has their own count")

    def test_the_route_is_post_only_json_and_without_the_automatic_csrf_check(self):
        source = (ROOT / "controllers/main.py").read_text(encoding="utf-8")
        self.assertIn('@http.route("/buddy/suggestions", type="http", auth="user", methods=["POST"], csrf=False, max_content_length=MAX_SUGGESTIONS_BODY)', source)
        self.assertEqual(controller.MAX_SUGGESTIONS_BODY, 256)

    def test_the_widget_asks_for_them_with_the_same_protections_as_the_other_routes(self):
        source = (ROOT / "static/src/js/buddy_suggestions.js").read_text(encoding="utf-8")
        self.assertIn('"/buddy/suggestions"', source)
        self.assertIn('"Content-Type": "application/json"', source)
        self.assertIn('"X-CSRF-Token"', source)
        self.assertNotIn("innerHTML", source)


class ViewStructureTests(unittest.TestCase):
    """What the browser's form/list compilers cannot read inside a <field> (found by looking at the onboarding views in a real
    browser: a <div> placed inside the one2many <field> broke the whole form with «Cannot find key div in the views
    registry», and the server never complained)."""

    def test_no_view_puts_html_inside_a_field(self):
        import xml.etree.ElementTree as ET
        offenders = []
        for path in sorted((ROOT / "views").glob("*.xml")):
            for arch in (node for node in ET.parse(path).getroot().iter("field") if node.get("name") == "arch"):
                for node in arch.iter("field"):  # the fields drawn INSIDE a view (not the «help» of an action, which is HTML)
                    for child in node:
                        if child.tag in ("div", "span", "p", "strong", "button", "group", "notebook"):
                            offenders.append("%s: <field name=%s> contains <%s>" % (path.name, node.get("name"), child.tag))
        self.assertEqual(offenders, [])


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Mock()
        self.engine._session_generation.return_value = 3
        self.engine.forget.return_value = 4
        odoo.http.request.env = Env(**{"buddy.engine": self.engine})
        self.controller = controller.BuddyController()
        self.ticket = ticket.BuddyTicket()
        self.ticket.env, self.ticket.records = Env(**{"ir.config_parameter": Params()}), []

    def test_portal_cannot_reach_engine(self):
        odoo.http.request.env.user = User(internal=False)
        for action in (self.controller.config, self.controller.chat, self.controller.ticket, self.controller.forget,
                       self.controller.history):
            with self.assertRaises(exceptions.AccessError):
                action()
        self.engine.chat.assert_not_called()
        self.engine.forget.assert_not_called()
        self.engine.history.assert_not_called()

    def test_history_returns_what_the_engine_gives_and_ignores_client_arguments(self):
        self.engine.history.return_value = {"messages": [{"role": "user", "text": "hola"}], "session_generation": 3}
        # Nothing the browser sends can pick another user or a bigger window: the engine is called without arguments.
        result = self.controller.history(user_id=2, limit=5000)
        self.engine.history.assert_called_once_with()
        self.assertEqual(result["session_generation"], 3)
        self.assertEqual(result["messages"][0]["text"], "hola")

    def test_history_busy_is_a_recoverable_error(self):
        self.engine.history.side_effect = BuddyBusy("busy")
        result = self.controller.history()
        self.assertEqual(result["error"], "busy")
        self.assertNotIn("messages", result)

    def test_invalid_message_cannot_consume_provider_call(self):
        for value in (None, 1, {}, [], "   ", "x" * 2001):
            self.assertEqual(self.controller.chat(message=value)["error"], "invalid_request")
        self.engine.chat.assert_not_called()

    def test_generation_must_be_positive_integer(self):
        for generation in (True, False, 0, -1, 1.2, "3"):
            self.assertEqual(self.controller.chat(message="hola", session_generation=generation)["error"], "invalid_request")
        self.engine.chat.assert_not_called()

    def test_invalid_key_rejected_before_engine(self):
        for key in ("bad", 12, {}, "x" * 100):
            self.assertEqual(self.controller.chat(message="hola", request_id=key)["error"], "invalid_request")
        self.engine.chat.assert_not_called()

    def test_chat_forwards_request_identity_and_generation(self):
        self.controller.chat(message=" hola ", session_generation=3, request_id=KEY.upper())
        self.engine.chat.assert_called_once_with("hola", session_generation=3, request_id=KEY)

    def test_session_changed_response_is_safe_and_current(self):
        self.engine.chat.side_effect = BuddyBusy("session_changed")
        response = self.controller.chat(message="hola", session_generation=2, request_id=KEY)
        self.assertEqual((response["session_generation"], response["sources"], response["ticket"]), (3, [], None))
        self.assertEqual(response["ring"], "doubt")

    def test_generation_refresh_busy_keeps_original_safe_error(self):
        self.engine.chat.side_effect = BuddyBusy("session_changed")
        self.engine._session_generation.side_effect = BuddyBusy("busy")
        result = self.controller.chat(message="hola", session_generation=2, request_id=KEY)
        self.assertEqual(result["error"], "session_changed")
        self.assertNotIn("session_generation", result)

    def test_forget_does_not_report_success_after_exception(self):
        self.engine.forget.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            self.controller.forget()

    def test_forget_returns_confirmed_generation(self):
        self.assertEqual(self.controller.forget(), {"ok": True, "session_generation": 4})

    def test_forget_busy_never_claims_success(self):
        self.engine.forget.side_effect = BuddyBusy("busy")
        result = self.controller.forget()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "busy")

    def test_config_busy_is_recoverable(self):
        odoo.http.request.env["ir.config_parameter"] = Params(**{"buddy_ia.tagline": "Mi equipo"})
        self.engine._session_generation.side_effect = BuddyBusy("busy")
        result = self.controller.config()
        self.assertEqual(result["error"], "busy")
        self.assertTrue(result["answer"])
        # The appearance survives a busy engine: the widget still renders and explains the problem on send.
        self.assertEqual((result["tagline"], result["enabled"]), ("Mi equipo", True))
        self.assertNotIn("session_generation", result)

    def config_with(self, **values):
        odoo.http.request.env["ir.config_parameter"] = Params(**values)
        return self.controller.config()

    def test_config_returns_appearance_defaults_for_a_fresh_install(self):
        result = self.config_with()
        self.assertEqual(result, {"buddy_name": "Buddy", "enabled": True, "greeting": "", "tagline": "", "suggestions": [],
                                  "primary_color": "", "first_name": "Ana", "session_generation": 3, "configured": True,
                                  "company": "", "is_admin": False, "character": "mochi", "accessory": "none",
                                  "keep_questions": False, "proactive": {"suggestions": True, "nudge": True},
                                  "idle": {"enabled": True, "frequency": "normal",
                                           "groups": ["expressions", "accessories", "sleep"]}})

    def test_config_says_whether_the_administrator_keeps_every_question(self):
        for value, expected in (("True", True), ("true", True), ("1", True), ("False", False), ("", False), (None, False), ("yes", False)):
            self.assertIs(self.config_with(**{"buddy_ia.insight_questions": value})["keep_questions"], expected, value)

    def test_config_returns_customizations_and_never_secrets_or_prompts(self):
        result = self.config_with(**{
            "buddy_ia.buddy_name": "Lía", "buddy_ia.greeting": "Buenas, {nombre}", "buddy_ia.tagline": "Tu compañera",
            "buddy_ia.suggestion_1": "¿Cómo pido vacaciones?", "buddy_ia.suggestion_2": "  ",
            "buddy_ia.suggestion_3": "Necesito IT", "buddy_ia.primary_color": "#AABBCC",
            "buddy_ia.extra_instructions": "SECRETO DE LA EMPRESA", "buddy_ia.api_key": "sk-secret",
            "buddy_ia.hidden": "True"})
        self.assertEqual(result["suggestions"], ["¿Cómo pido vacaciones?", "Necesito IT"])
        self.assertEqual((result["buddy_name"], result["primary_color"], result["enabled"]), ("Lía", "#aabbcc", False))
        self.assertEqual(result["greeting"], "Buenas, {nombre}")
        self.assertNotIn("SECRETO", repr(result))
        self.assertNotIn("sk-secret", repr(result))
        self.assertNotIn("extra_instructions", result)

    def test_config_clamps_lengths_and_drops_invalid_values(self):
        values = {"buddy_ia.buddy_name": "n" * 500, "buddy_ia.tagline": "t" * 500, "buddy_ia.greeting": "g" * 500,
                  "buddy_ia.suggestion_1": "s" * 500, "buddy_ia.suggestion_5": "ignored",
                  "buddy_ia.primary_color": "red; background:url(x)"}
        result = self.config_with(**values)
        self.assertEqual((len(result["buddy_name"]), len(result["tagline"]), len(result["greeting"]),
                          len(result["suggestions"][0])), (80, 60, 140, 80))
        self.assertEqual(result["primary_color"], "")
        for bad in ("#12345", "#12345g", "123456", "#1234567", "rgb(1,2,3)", 5, ["#aabbcc"]):
            self.assertEqual(self.config_with(**{"buddy_ia.primary_color": bad})["primary_color"], "", bad)

    def test_config_returns_the_chosen_look_from_the_whitelists(self):
        result = self.config_with(**{"buddy_ia.character": "kitsune", "buddy_ia.accessory": "wizard",
                                     "buddy_ia.idle_enabled": "0", "buddy_ia.idle_frequency": "high",
                                     "buddy_ia.idle_groups": "sleep,expressions"})
        self.assertEqual((result["character"], result["accessory"]), ("kitsune", "wizard"))
        self.assertEqual(result["idle"], {"enabled": False, "frequency": "high", "groups": ["expressions", "sleep"]})

    def test_config_never_sends_free_text_as_the_look(self):
        evil = '<img src=x onerror=alert(1)>'
        result = self.config_with(**{"buddy_ia.character": evil, "buddy_ia.accessory": "wizard; background:url(x)",
                                     "buddy_ia.idle_frequency": "<b>", "buddy_ia.idle_groups": evil + ",sleep,../x",
                                     "buddy_ia.idle_enabled": evil})
        self.assertEqual((result["character"], result["accessory"], result["idle"]["frequency"]), ("mochi", "none", "normal"))
        self.assertEqual(result["idle"]["groups"], ["sleep"])
        self.assertNotIn("<", repr(result["idle"]) + result["character"] + result["accessory"])
        for bad in (5, ["kitsune"], {"a": 1}, None, False, ""):
            self.assertEqual(self.config_with(**{"buddy_ia.character": bad})["character"], "mochi", bad)

    def test_config_collapses_whitespace_and_ignores_non_text(self):
        result = self.config_with(**{"buddy_ia.tagline": "  una\n  frase\t larga  ", "buddy_ia.greeting": 12345,
                                     "buddy_ia.suggestion_1": ["lista"]})
        self.assertEqual((result["tagline"], result["greeting"], result["suggestions"]), ("una frase larga", "", []))

    def test_hidden_flag_only_accepts_true_values(self):
        for value, enabled in (("True", False), ("true", False), ("1", False), ("False", True), ("", True), (False, True), (None, True)):
            self.assertEqual(self.config_with(**{"buddy_ia.hidden": value})["enabled"], enabled, value)

    def test_friendly_errors_are_readable_for_every_engine_code(self):
        for code in ("rate_limited", "provider_error", "provider_auth", "invalid_request", "provider_timeout", "invalid_message", "invalid_response",
                     "not_configured", "session_changed", "busy", "request_pending", "unknown_code"):
            self.assertTrue(controller._friendly(code), code)
        # An employee cannot do anything about the settings: the message sends them to whoever administers Odoo.
        self.assertIn("quien administra Odoo", controller._friendly("not_configured"))
        self.assertNotIn("Ajustes", controller._friendly("not_configured"))

    def test_contact_codes_are_normalized_validated_and_unique(self):
        contacts = contact
        self.assertEqual(contacts.normalize_code("  RRHH "), "rrhh")
        self.assertIsNone(contacts.normalize_code(None))

        class Fake(list):
            def sudo(self):
                return self

            def search(self, domain):
                return Fake(row for row in self.others)

            def filtered(self, function):
                return Fake(row for row in self if function(row))

        def check(code, others=()):
            row = types.SimpleNamespace(id=1, ids=[1], code=code, label="Propio")
            fake = Fake([row])
            fake.others = [types.SimpleNamespace(id=2, code=other, label="Otro") for other in others]
            contact.BuddyContact._check_code(fake)

        check("it")
        check("rrhh_2-b")
        for invalid in ("", None, "con espacio", "Mayus", "ñandú", "x" * 33, "a;b", "-start"):
            with self.subTest(code=invalid), self.assertRaises(exceptions.ValidationError):
                check(invalid)
        with self.assertRaises(exceptions.ValidationError):
            check("it", others=("it",))
        with self.assertRaises(exceptions.ValidationError):
            check("it", others=(" IT ",))  # legacy row saved with capitals
        check("it", others=("rrhh", "ventas"))

    def test_ticket_limits_reject_before_creation(self):
        for title, detail in (("", ""), ("x" * 121, ""), ("valid", "x" * 2001), ("valid", {})):
            self.assertEqual(self.controller.ticket(title=title, description=detail)["error"], "invalid_request")
        self.engine._create_ticket.assert_not_called()

    def test_ticket_preserves_retry_identity(self):
        self.controller.ticket(title=" ayuda ", description="detalle", request_key=KEY, session_generation=3)
        self.engine._create_ticket.assert_called_once_with("ayuda", "detalle", KEY, 3)

    def test_direct_model_rejects_impersonation(self):
        with self.assertRaises(exceptions.AccessError):
            self.ticket.create([{"name": "ayuda", "user_id": 8}])
        self.assertEqual(self.ticket.records, [])

    def test_direct_model_cannot_start_with_resolved_ticket(self):
        with self.assertRaises(exceptions.AccessError):
            self.ticket.create([{"name": "ayuda", "state": "done"}])

    def test_direct_model_enforces_input_limits(self):
        for values in ({"name": " "}, {"name": "x" * 121}, {"name": "ok", "description": "x" * 2001},
                       {"name": "ok", "request_key": "bad"}):
            with self.assertRaises(exceptions.ValidationError):
                self.ticket.create([values])

    def test_ticket_retry_returns_existing_record(self):
        values = {"name": "ayuda", "description": "detalle", "request_key": KEY}
        first, second = self.ticket.create([values]), self.ticket.create([values])
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.ticket.records), 1)

    def test_daily_ticket_cap_is_enforced_per_user_in_the_model(self):
        self.ticket.env = Env(**{"ir.config_parameter": Params(**{"buddy_ia.tickets_per_day": "2"})})
        self.ticket.records = []
        for index in range(2):
            self.ticket.create([{"name": "ayuda %d" % index}])
        for row in self.ticket.records:
            row.create_date = datetime.datetime.now()
        with self.assertRaises(exceptions.ValidationError):
            self.ticket.create([{"name": "una más"}])

    def test_rate_limits_below_one_are_rejected(self):
        values = {"buddy_top_k": 4, "buddy_retention_days": 30, "buddy_rate_per_minute": 6,
                  "buddy_rate_per_day": 150, "buddy_new_user_turns": 12, "buddy_sync_hours": 24,
                  "buddy_max_concurrent": 3}
        for field in ("buddy_rate_per_minute", "buddy_rate_per_day"):
            with self.subTest(field=field), self.assertRaises(exceptions.ValidationError):
                settings.ResConfigSettings._validate_buddy_settings([types.SimpleNamespace(**{**values, field: 0})])

    def test_retry_key_cannot_replace_payload(self):
        self.ticket.create([{"name": "ayuda", "request_key": KEY}])
        with self.assertRaises(exceptions.ValidationError):
            self.ticket.create([{"name": "otra", "request_key": KEY}])

    def test_employee_diagnostic_rejected_before_any_work(self):
        config = settings.ResConfigSettings()
        config.env = Env(**{"buddy.engine": self.engine})
        with self.assertRaises(exceptions.AccessError):
            config.action_buddy_test_connection()
        self.engine._self_test.assert_not_called()

    def test_diagnostic_quota_is_readable_notification(self):
        config = settings.ResConfigSettings()
        params = Params()
        config.env = Env(User(admin=True), **{"buddy.engine": self.engine, "ir.config_parameter": params})
        self.engine._self_test.side_effect = BuddyBusy("rate_limited")
        result = config.action_buddy_test_connection()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertIn("Esperá", result["params"]["message"])
        self.assertNotIn(settings.LAST_TEST_PARAM, params.values, "a throttled test is not a real test result")

    def test_diagnostic_result_is_remembered_for_the_checklist(self):
        config = settings.ResConfigSettings()
        params = Params()
        config.env = Env(User(admin=True), **{"buddy.engine": self.engine, "ir.config_parameter": params})
        self.engine._self_test.return_value = (True, ["Chat JSON OK"])
        result = config.action_buddy_test_connection()
        self.assertEqual((result["params"]["type"], result["params"]["sticky"]), ("success", False))
        self.assertTrue(params.values[settings.LAST_TEST_PARAM].startswith("ok|"))
        self.assertEqual(config._buddy_last_test()[0], True)
        self.engine._self_test.return_value = (False, ["Embeddings fallan"])
        result = config.action_buddy_test_connection()
        self.assertEqual((result["params"]["type"], result["params"]["sticky"]), ("danger", True))
        self.assertTrue(params.values[settings.LAST_TEST_PARAM].startswith("fail|"))
        self.assertEqual(config._buddy_last_test()[0], False)
        self.assertEqual(result["params"]["next"]["tag"], "soft_reload", "the checklist refreshes after testing")

    def test_last_test_defaults_to_never_tested(self):
        config = settings.ResConfigSettings()
        config.env = Env(User(admin=True), **{"ir.config_parameter": Params()})
        self.assertEqual(config._buddy_last_test(), (False, ""))
        config.env = Env(User(admin=True), **{"ir.config_parameter": Params(**{settings.LAST_TEST_PARAM: "garbage"})})
        self.assertEqual(config._buddy_last_test(), (False, ""))

    def test_checklist_screens_are_admin_only(self):
        config = settings.ResConfigSettings()
        config.env = Env(User(), **{})
        for opener in (config.action_buddy_open_sources, config.action_buddy_clear_api_key,
                       config.action_buddy_clear_drive_key):
            with self.assertRaises(exceptions.AccessError):
                opener()

    def test_text_limits_color_and_suggestions_are_validated(self):
        base = {name: "" for name in ("buddy_name", "buddy_company", "buddy_tagline", "buddy_greeting", "buddy_primary_color",
                                       "buddy_suggestion_1", "buddy_suggestion_2", "buddy_suggestion_3",
                                       "buddy_suggestion_4", "buddy_extra_instructions", "buddy_chat_model",
                                       "buddy_api_base", "buddy_api_key_input")}
        base["buddy_provider"] = "anthropic"
        validate = settings.ResConfigSettings._validate_buddy_texts
        validate([types.SimpleNamespace(**base)])
        validate([types.SimpleNamespace(**{**base, "buddy_primary_color": "#2F6DF6", "buddy_greeting": "g" * 140,
                                            "buddy_extra_instructions": "x" * 1500})])
        for field, invalid in (("buddy_name", "x" * 81), ("buddy_company", "x" * 161), ("buddy_tagline", "x" * 61),
                               ("buddy_greeting", "x" * 141), ("buddy_suggestion_1", "x" * 81),
                               ("buddy_suggestion_4", "x" * 81), ("buddy_extra_instructions", "x" * 1501),
                               ("buddy_primary_color", "red"), ("buddy_primary_color", "#12345"),
                               ("buddy_primary_color", "#12345g"), ("buddy_primary_color", "#aabbcc; x"),
                               ("buddy_chat_model", "m" * 201), ("buddy_api_key_input", "k" * 501),
                               ("buddy_api_key_input", "sk con espacio"), ("buddy_api_key_input", "sk-a" + chr(10) + "b"),
                               ("buddy_provider", "custom")):
            with self.subTest(field=field, value=invalid[:12]), self.assertRaises(exceptions.ValidationError):
                validate([types.SimpleNamespace(**{**base, field: invalid})])

    def test_company_synonyms_are_validated(self):
        validate = settings.ResConfigSettings._validate_buddy_synonyms
        validate([types.SimpleNamespace(buddy_synonyms="")])
        validate([types.SimpleNamespace(buddy_synonyms=None)])
        validate([types.SimpleNamespace(buddy_synonyms="vacaciones, licencia, días libres\nsueldo, haberes\n\n")])
        validate([types.SimpleNamespace(buddy_synonyms="\n".join("grupo%d, otro%d" % (n, n) for n in range(40)))])
        too_many_groups = "\n".join("grupo%d, otro%d" % (n, n) for n in range(41))
        too_many_words = ", ".join("palabra%d" % n for n in range(13))
        for invalid in ("x" * 2001, too_many_groups, too_many_words, "sola", "uno, , tres", "uno,", "a" * 41 + ", otra",
                        "uno dos tres cuatro cinco, otra", "ticket | caso, otra", "ticket & caso, otra", "ticket!, caso",
                        "'ticket', caso", "(ticket), caso", "ticket:*, caso", "<->, caso", "el, un", "ticket;caso, otro"):
            with self.subTest(invalid=invalid[:30]), self.assertRaises(exceptions.ValidationError):
                settings.ResConfigSettings._validate_buddy_synonyms([types.SimpleNamespace(buddy_synonyms=invalid)])

    def test_company_synonyms_are_stored_without_blank_lines(self):
        class Parent:
            def set_values(self):
                """Stand-in for res.config.settings.set_values."""

        config = type("Settings", (settings.ResConfigSettings, Parent), {})()
        params = Params()
        config.env = Env(User(admin=True), **{"ir.config_parameter": params})
        config.buddy_extra_instructions = ""
        config.buddy_api_key_input = config.buddy_drive_key_input = ""
        config.buddy_synonyms = "  vacaciones, licencia \r\n\r\n sueldo, haberes\n"
        config.set_values()
        self.assertEqual(params.values[settings.SYNONYMS_PARAM], "vacaciones, licencia\nsueldo, haberes")
        config.buddy_synonyms = " \n"
        config.set_values()
        self.assertFalse(params.values[settings.SYNONYMS_PARAM])

    def test_the_busy_message_covers_the_global_limit_and_stays_kind(self):
        odoo.http.request.env = Env()
        messages = controller._friendly_messages()
        self.assertIn("muchas consultas", messages["busy"])
        self.assertIn("probá de nuevo", messages["busy"])
        self.assertEqual(controller._friendly("busy", messages), messages["busy"])

    def test_settings_bounds_match_engine(self):
        values = {"buddy_top_k": 4, "buddy_retention_days": 30, "buddy_rate_per_minute": 6,
                  "buddy_rate_per_day": 150, "buddy_new_user_turns": 12, "buddy_sync_hours": 24,
                  "buddy_max_concurrent": 3}
        settings.ResConfigSettings._validate_buddy_settings([types.SimpleNamespace(**values)])
        for field, invalid in (("buddy_top_k", 11), ("buddy_top_k", 0), ("buddy_retention_days", -1),
                               ("buddy_rate_per_minute", 1001), ("buddy_rate_per_day", 100001),
                               ("buddy_new_user_turns", 10001), ("buddy_sync_hours", 0), ("buddy_sync_hours", 169),
                               ("buddy_max_concurrent", 0), ("buddy_max_concurrent", 101)):
            with self.subTest(field=field), self.assertRaises(exceptions.ValidationError):
                settings.ResConfigSettings._validate_buddy_settings([types.SimpleNamespace(**{**values, field: invalid})])


class FirstUseAndLimitsTests(unittest.TestCase):
    """Pieces of the administration work that need no Odoo: validation, key files, placeholders, messages."""

    def setUp(self):
        self.engine = Mock()
        odoo.http.request.env = Env(**{"buddy.engine": self.engine, "ir.config_parameter": Params()})
        self.controller = controller.BuddyController()

    # --- /buddy/config: configured --------------------------------------------------------------------------
    def test_config_says_whether_the_buddy_is_configured(self):
        self.engine._session_generation.return_value = 3
        self.engine._configured.return_value = False
        self.assertIs(self.controller.config()["configured"], False)
        self.engine._configured.return_value = True
        self.assertIs(self.controller.config()["configured"], True)

    def test_config_still_says_it_when_the_session_is_busy_and_never_leaks_settings(self):
        self.engine._session_generation.side_effect = BuddyBusy("busy")
        self.engine._configured.return_value = False
        result = self.controller.config()
        self.assertIs(result["configured"], False)
        self.assertEqual(result["error"], "busy")
        self.assertNotIn("api_key", str(result))

    # --- friendly messages for each limit -----------------------------------------------------------------------
    def test_each_limit_has_its_own_message_and_the_daily_ones_do_not_invite_a_retry(self):
        messages = controller._friendly_messages()
        texts = {code: messages[code] for code in ("rate_limited", "daily_limit", "system_limit")}
        self.assertEqual(len(set(texts.values())), 3)
        self.assertIn("muy seguido", texts["rate_limited"])
        for code in ("daily_limit", "system_limit"):
            self.assertNotRegex(texts[code].lower(), r"prob[aá] de nuevo en un (momento|minuto)", code)
        self.assertIn("hoy", texts["daily_limit"])
        self.assertIn("empresa", texts["system_limit"])
        self.assertEqual(controller._friendly("daily_limit", messages), texts["daily_limit"])
        failure = controller._failure("system_limit", messages=messages)
        self.assertEqual((failure["error"], failure["ring"], failure["ticket"]), ("system_limit", "doubt", None))

    def test_limit_messages_use_the_configured_name_and_the_general_contact_and_promise_no_tomorrow(self):
        class Contacts:
            def sudo(self):
                return self

            def search(self, domain, limit=None):
                return Rows([types.SimpleNamespace(label="RR.HH.")])

        odoo.http.request.env = Env(**{"ir.config_parameter": Params(**{"buddy_ia.buddy_name": "Lía"}), "buddy.contact": Contacts()})
        messages = controller._friendly_messages()
        for code in ("daily_limit", "system_limit"):
            text = messages[code]
            self.assertIn("Lía", text, code)
            self.assertIn("RR.HH.", text, code)
            self.assertIn("Buddy → Tickets", text, code)
            self.assertIn("unas horas", text, code)
            self.assertNotIn("mañana", text.lower(), code)
            self.assertNotIn("Ajustes", text, code)
            self.assertNotIn("administrador", text.lower(), code)
        self.assertIn("ventana móvil", messages["system_limit"])
        # Without a «general» contact and with the default name there is still a sensible text.
        odoo.http.request.env = Env()
        fallback = controller._friendly_messages()["daily_limit"]
        self.assertIn("Buddy", fallback)
        self.assertIn("la mesa de ayuda", fallback)

    def test_a_refused_key_says_the_same_to_everybody_but_the_administrator_is_told_where_to_fix_it(self):
        employee = controller._friendly_messages()
        odoo.http.request.env.user = User(admin=True)
        admin = controller._friendly_messages()
        for code in ("provider_auth", "not_configured"):
            self.assertNotEqual(employee[code], admin[code], code)
            self.assertIn("quien administra Odoo", employee[code])
            self.assertNotIn("Ajustes", employee[code])
            self.assertIn("Buddy → Configuración", admin[code])
            self.assertNotIn("quien administra Odoo", admin[code], "an administrator is not told to ask themselves")
        self.assertNotRegex(employee["provider_auth"].lower(), r"prob[aá] de nuevo|reintent", "a retry cannot fix a refused key")

    def test_an_invalid_id_or_generation_is_not_reported_as_a_message_of_the_wrong_length(self):
        too_long = self.controller.chat(message="x" * 2001)
        wrong_type = self.controller.chat(message="hola", session_generation="3")
        wrong_key = self.controller.chat(message="hola", request_id="no-es-un-uuid")
        for result in (too_long, wrong_type, wrong_key):
            self.assertEqual(result["error"], "invalid_request")
        self.assertIn("2000 caracteres", too_long["answer"])
        for result in (wrong_type, wrong_key):
            self.assertNotIn("caracteres", result["answer"])
            self.assertIn("Recargá la página", result["answer"])
        self.engine.chat.assert_not_called()

    def test_the_config_route_tells_whether_the_person_is_an_administrator(self):
        self.engine._session_generation.return_value = 3
        odoo.http.request.env.user = User(admin=True)
        self.assertIs(self.controller.config()["is_admin"], True)
        odoo.http.request.env.user = User()
        self.assertIs(self.controller.config()["is_admin"], False)

    def test_a_refused_ticket_returns_its_reason_for_the_widget(self):
        self.engine._create_ticket.side_effect = exceptions.ValidationError("Llegaste al máximo de tickets por día.")
        result = self.controller.ticket(title="ayuda", description="d", request_key=KEY, session_generation=3)
        self.assertEqual(result, {"error": "ticket_rejected", "message": "Llegaste al máximo de tickets por día."})

    # --- contacts: sample addresses ----------------------------------------------------------------------------------
    def test_sample_addresses_are_recognised(self):
        for sample in ("ayuda@ejemplo.test", "it@ejemplo.com", "x@example.com", "x@sub.example.org", "x@algo.test", "x@mail.invalid",
                       "x@localhost", "sin-arroba", "", None, " ", "@dominio.com", "x@", "x@sinpunto", "X@EJEMPLO.COM"):
            self.assertTrue(contact.is_placeholder_email(sample), repr(sample))
        for real in ("ayuda@miempresa.com", "it@miempresa.com.ar", "rrhh@grupo-ejemplar.com", "a.b+c@sub.dominio.io", " ana@acme.co "):
            self.assertFalse(contact.is_placeholder_email(real), repr(real))

    # --- the Drive key as a file ----------------------------------------------------------------------------------------
    @staticmethod
    def uploaded(content):
        import base64
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        record = types.SimpleNamespace(buddy_drive_key_file=base64.b64encode(data))
        record.with_context = lambda **context: record
        return record

    def test_a_key_file_is_decoded_trimmed_and_tolerates_a_byte_order_mark(self):
        read = settings.ResConfigSettings._buddy_uploaded_key
        self.assertEqual(read(self.uploaded('{"type": "service_account"}')), '{"type": "service_account"}')
        self.assertEqual(read(self.uploaded(b"\xef\xbb\xbf  {\"a\": 1}\r\n")), '{"a": 1}')
        self.assertEqual(read(types.SimpleNamespace(buddy_drive_key_file=False)), "")
        self.assertEqual(read(types.SimpleNamespace(buddy_drive_key_file=None)), "")

    def test_a_bad_key_file_is_a_friendly_validation_error_that_does_not_echo_it(self):
        read = settings.ResConfigSettings._buddy_uploaded_key
        for content in (b"\x00\xff\xfe\x80", b"x" * (settings.MAX_KEY_FILE_BYTES + 1)):
            with self.assertRaises(exceptions.ValidationError) as caught:
                read(self.uploaded(content))
            self.assertIn("64 KB", str(caught.exception))
        broken = types.SimpleNamespace(buddy_drive_key_file=b"no es base64 !!!")
        broken.with_context = lambda **context: broken
        with self.assertRaises(exceptions.ValidationError):
            read(broken)

    def test_the_email_is_the_only_thing_read_from_the_stored_key(self):
        from_source = sys.modules["boundary.models.buddy_source"]
        self.assertEqual(from_source.service_account_email('{"client_email": "bot@x.iam.gserviceaccount.com", "private_key": "SECRETO"}'),
                         "bot@x.iam.gserviceaccount.com")
        for raw in (None, False, "", "{", "[]", "5", '{"client_email": 3}', "{}"):
            self.assertEqual(from_source.service_account_email(raw), "", repr(raw))

    def test_saving_a_key_file_and_a_pasted_text_together_is_refused(self):
        class Parent:
            def set_values(self):
                """Stand-in for res.config.settings.set_values."""

        config = type("Settings", (settings.ResConfigSettings, Parent), {})()
        config.env = Env(User(admin=True), **{"ir.config_parameter": Params()})
        config.buddy_extra_instructions = config.buddy_synonyms = config.buddy_api_key_input = ""
        config.buddy_drive_key_input = '{"type": "service_account"}'
        config.buddy_drive_key_file = base64_of('{"type": "service_account"}')
        config.with_context = lambda **context: config
        with self.assertRaises(exceptions.ValidationError) as caught:
            config.set_values()
        self.assertIn("una sola forma", str(caught.exception))

    # --- new numeric settings ------------------------------------------------------------------------------------------------
    def test_costs_and_company_limits_are_validated(self):
        valid = {"buddy_insight_days": 90, "buddy_price_in": 0.0, "buddy_price_out": 0.0, "buddy_max_daily_total": 1000,
                 "buddy_max_daily_cost": 0.0}
        check = settings.ResConfigSettings._validate_buddy_costs
        check([types.SimpleNamespace(**valid)])
        check([types.SimpleNamespace(**{**valid, "buddy_price_in": 3.0, "buddy_max_daily_cost": 25.0})])
        for field, invalid in (("buddy_insight_days", 0), ("buddy_insight_days", 3651), ("buddy_max_daily_total", 0),
                               ("buddy_max_daily_total", 1_000_001), ("buddy_price_in", -0.1), ("buddy_price_out", 1_000_001.0),
                               ("buddy_max_daily_cost", -1.0), ("buddy_max_daily_cost", 5.0)):
            with self.subTest(field=field, value=invalid), self.assertRaises(exceptions.ValidationError):
                check([types.SimpleNamespace(**{**valid, field: invalid})])

    def test_the_old_checks_do_not_need_the_new_fields(self):
        values = {"buddy_top_k": 4, "buddy_retention_days": 30, "buddy_rate_per_minute": 6,
                  "buddy_rate_per_day": 40, "buddy_new_user_turns": 12, "buddy_sync_hours": 24, "buddy_max_concurrent": 3}
        settings.ResConfigSettings._validate_buddy_settings([types.SimpleNamespace(**values)])

    # --- statistics model: pure helpers ---------------------------------------------------------------------------------------
    def test_insight_parameter_helpers(self):
        for value, expected in (("True", True), ("true", True), ("1", True), (True, True), ("False", False), ("", False), (None, False),
                                (False, False), ("0", False), ("yes", False)):
            self.assertIs(insight.flag(value), expected, repr(value))
        for value, expected in (("3", 3.0), ("0.5", 0.5), (7, 7.0), (None, 0.0), ("", 0.0), ("abc", 0.0), ("-1", 0.0),
                                ("1000001", 0.0), ("nan", 0.0), ("1000000", 1000000.0)):
            self.assertEqual(insight.price(value), expected, repr(value))
        self.assertEqual(insight.DEFAULT_RETENTION_DAYS, 90)
        self.assertEqual({code for code, label in insight.ERRORS} - {"other"},
                         {"provider_error", "provider_auth", "provider_timeout", "invalid_response", "session_changed", "client_disconnected",
                          "not_configured"})
        self.assertNotIn("user_id", dir(insight.BuddyInsight), "a statistics row never says who asked")

    # --- the result of «Probar conexión» stays ---------------------------------------------------------------------------------
    def test_the_lines_of_the_last_test_are_stored_for_the_checklist_and_a_throttled_test_keeps_the_old_ones(self):
        config = settings.ResConfigSettings()
        params = Params(**{settings.LAST_TEST_DETAIL_PARAM: "anterior"})
        config.env = Env(User(admin=True), **{"buddy.engine": self.engine, "ir.config_parameter": params})
        self.engine._self_test.return_value = (False, ["Falta la clave de API del proveedor de IA.", "No hay carpetas de Drive conectadas."])
        config.action_buddy_test_connection()
        self.assertEqual(params.values[settings.LAST_TEST_DETAIL_PARAM],
                         "Falta la clave de API del proveedor de IA.\nNo hay carpetas de Drive conectadas.")
        self.engine._self_test.side_effect = BuddyBusy("rate_limited")
        config.action_buddy_test_connection()
        self.assertIn("Falta la clave", params.values[settings.LAST_TEST_DETAIL_PARAM])
        self.engine._self_test.side_effect = None
        self.engine._self_test.return_value = (False, ["x" * 500] * 40)
        config.action_buddy_test_connection()
        self.assertLessEqual(len(params.values[settings.LAST_TEST_DETAIL_PARAM]), settings.LAST_TEST_DETAIL_LIMIT)

    def test_new_buttons_are_for_administrators(self):
        config = settings.ResConfigSettings()
        config.env = Env(User(), **{})
        for opener in (config.action_buddy_open_contacts, config.action_buddy_open_unanswered, config.action_buddy_delete_insights):
            with self.assertRaises(exceptions.AccessError):
                opener()


def base64_of(text):
    import base64
    return base64.b64encode(text.encode("utf-8"))


def read_po(path):
    """Minimal .po/.pot reader: {msgid: msgstr} (header and obsolete entries skipped)."""
    entries, current, field = {}, {"msgid": "", "msgstr": ""}, None

    def flush():
        if current["msgid"]:
            entries[current["msgid"]] = current["msgstr"]

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("msgid "):
            flush()
            current, field = {"msgid": "", "msgstr": ""}, "msgid"
            line = line[6:]
        elif line.startswith("msgstr "):
            field, line = "msgstr", line[7:]
        elif not line.startswith('"'):
            continue
        current[field] += ast.literal_eval(line)
    flush()
    return entries


def literal_message(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# Python files whose messages are translated with `_()` (every `_("...")` of these must be in the .pot).
TRANSLATED_PYTHON = ("controllers/main.py", "models/res_config_settings.py", "models/buddy_contact.py", "models/buddy_source.py",
                     "models/buddy_engine.py", "models/buddy_provider.py", "models/buddy_ticket.py", "models/buddy_insight.py",
                     "models/buddy_journey.py", "models/buddy_journey_engine.py", "models/buddy_digest.py", "models/buddy_proactive.py")


class TranslationTests(unittest.TestCase):
    """The .pot must list every source string (run the Odoo export to refresh). The source language is Spanish and
    there is deliberately no en.po: Odoo would use it as the fallback for Spanish users and mix both languages."""

    I18N = ROOT / "i18n"
    PLACEHOLDER = re.compile(r"%\(\w+\)s|\{\w+\}")

    @classmethod
    def setUpClass(cls):
        cls.pot = read_po(cls.I18N / "buddy_ia.pot")

    def test_there_is_no_english_po_that_would_override_the_spanish_source(self):
        self.assertFalse((self.I18N / "en.po").exists(), "en.po makes Spanish users see English for untranslated terms")

    def test_every_js_translatable_string_is_in_the_pot(self):
        found = set()
        for name in ("buddy.js", "buddy_sync_refresh.js", "buddy_look.js", "buddy_journey.js"):
            source = (ROOT / "static/src/js" / name).read_text(encoding="utf-8")
            found |= {match.group(2) for match in re.finditer(r'\b_t\(\s*(["\'])((?:\\.|(?!\1).)*)\1', source)}
        self.assertGreater(len(found), 20)
        missing = {text for text in found if text not in self.pot}
        self.assertEqual(missing, set(), "run the Odoo i18n export again")

    def test_every_python_translatable_string_is_in_the_pot(self):
        found = set()
        for relative in TRANSLATED_PYTHON:
            for node in ast.walk(ast.parse((ROOT / relative).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_" and node.args:
                    message = literal_message(node.args[0])
                    if message is not None:
                        found.add(message)
        self.assertGreater(len(found), 60)
        self.assertEqual({text for text in found if text not in self.pot}, set(), "run the Odoo i18n export again")

    def test_messages_an_administrator_or_employee_can_read_are_never_raw_text(self):
        """AccessError / ValidationError / UserError / ProviderError messages go through _() (or env._): raw literals
        would stay in Spanish for everybody. The pure modules (drive_client, buddy_text, buddy_chunker, prompts,
        buddy_stream) have no Odoo to translate with and keep their own Spanish messages on purpose."""
        raw = []
        for relative in TRANSLATED_PYTHON:
            for node in ast.walk(ast.parse((ROOT / relative).read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id in ("AccessError", "ValidationError", "UserError") and node.args:
                    message = literal_message(node.args[0])
                elif node.func.id == "ProviderError" and len(node.args) > 1:
                    message = literal_message(node.args[1])
                else:
                    continue
                if message is not None:
                    raw.append("%s: %s" % (relative, message[:60]))
        self.assertEqual(raw, [])

    def test_static_template_text_is_extractable(self):
        # Attribute values built with t-att-* are NOT translated by Odoo: they must come from _t() getters.
        template = (ROOT / "static/src/xml/buddy.xml").read_text(encoding="utf-8")
        for attribute in re.findall(r't-att-(?:aria-label|title|placeholder)="([^"]*)"', template):
            self.assertNotRegex(attribute, r"['\"][A-Za-zÁÉÍÓÚáéíóúñ¿¡]{3}", "hard-coded text in a dynamic attribute: " + attribute)


if __name__ == "__main__":
    unittest.main(verbosity=2)
