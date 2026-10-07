"""Streaming answers: the engine (`chat_stream`) with an in-memory provider, and the /buddy/chat_stream route."""
import json
import re
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from odoo import api
from odoo.tests.common import HttpCase, new_test_user, tagged

from ..controllers.main import sse_frame
from ..models.buddy_engine import BuddyBusy
from ..models.buddy_provider import ProviderError
from .common import new_document, new_group, new_source, new_user
from .test_engine import CHUNK, VALID, EngineCase

HUMAN = {"answer": "Eso no está documentado.", "expression": "doubt", "needs_human": True, "contact_role": "general",
         "ticket": {"title": "Consulta: sueldo", "description": "¿Y el sueldo?"}}


def pieces(data, size=7, fence=False):
    """A model output as the fragments a provider would stream (answer key first)."""
    text = json.dumps(data, ensure_ascii=False)
    if fence:
        text = "```json\n" + text + "\n```"
    return [text[start:start + size] for start in range(0, len(text), size)]


class FakeProvider:
    """Replaces buddy.provider._chat_stream with scripted fragments; records what the engine sent."""

    def __init__(self, fragments, error=None):
        self.fragments, self.error = list(fragments), error
        self.calls, self.started, self.closed = [], False, False

    def patch(self, env):
        """Patch buddy.provider._chat_stream (with a plain function, so that it binds like the real method)."""
        return patch.object(env.registry["buddy.provider"], "_chat_stream", lambda provider, *args, **kwargs: self(*args, **kwargs))

    def __call__(self, model, messages, timeout, **kwargs):
        self.calls.append({"model": model, "messages": messages, "timeout": timeout, "settings": kwargs.get("settings")})
        return self._generate()

    def _generate(self):
        self.started = True
        try:
            yield from self.fragments
            if self.error:
                raise self.error
        finally:
            self.closed = True


@contextmanager
def fresh_test_engine(engine, lock=False):
    """Like production, a NEW cursor (never the one of the web request, which is closed when the route returns);
    only the READ COMMITTED switch is skipped, because the fixtures live in the shared test transaction."""
    with engine.env.registry.cursor() as cursor:
        yield api.Environment(cursor, engine.env.uid, dict(engine.env.context))["buddy.engine"]


@tagged("post_install", "-at_install")
class TestBuddyStreamEngine(EngineCase):
    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "stream")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        for patcher in (patch.object(self.cls, "_authorization_hash", return_value="authorization-A"),
                        patch.object(self.cls, "_retrieve", return_value=[CHUNK])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def provider(self, fragments=None, error=None, output=VALID, **kwargs):
        fake = FakeProvider(pieces(output, **kwargs) if fragments is None else fragments, error)
        patcher = fake.patch(self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def usage(self, state=None):
        domain = [("user_id", "=", self.user.id)] + ([("state", "=", state)] if state else [])
        return self.env["buddy.usage"].sudo().search(domain)

    def session(self):
        return self.env["buddy.session"].sudo().search([("user_id", "=", self.user.id)])

    def run_stream(self, message="Pregunta", **kwargs):
        return list(self.eng.chat_stream(message, **kwargs))

    # --- the happy path -----------------------------------------------------------------------------------
    def test_deltas_then_done_identical_to_chat_and_persisted(self):
        fake = self.provider()
        request_id = str(uuid.uuid4())
        events = self.run_stream(request_id=request_id)
        kinds = [kind for kind, unused in events]
        self.assertEqual(kinds[-1], "done")
        self.assertTrue(kinds[:-1] and set(kinds[:-1]) == {"delta"})
        self.assertEqual("".join(payload["text"] for kind, payload in events if kind == "delta"), VALID["answer"])
        done = events[-1][1]
        with patch.object(self.cls, "_ask_model", return_value=dict(VALID)):
            expected = self.env["buddy.engine"].with_user(new_user(self.env, "other")).chat("Pregunta")
        # The rating code is random per answer: it must exist on both routes, and nothing else may differ.
        self.assertTrue(done.pop("feedback_token") and expected.pop("feedback_token"))
        self.assertEqual(done, expected, "same response shape as the non-streaming route")
        self.assertEqual(done["session_generation"], self.eng._session_generation())
        self.assertEqual([row["title"] for row in done["sources"]], ["Guía"])
        self.assertEqual(self.usage("done").mapped("request_id"), [request_id])
        self.assertEqual(self.messages_of(self.user), 2)
        self.assertFalse(self.session().inflight_id, "the reservation is released")
        self.assertTrue(fake.closed)
        self.assertEqual(fake.calls[0]["model"], "qa-chat")
        self.assertEqual(fake.calls[0]["settings"]["base_url"], "https://llm.test/v1", "settings are read during the preparation")

    def test_the_preparation_happens_before_the_first_event(self):
        fake = self.provider()
        request_id = str(uuid.uuid4())
        stream = self.eng.chat_stream("Pregunta", request_id=request_id)
        # Nothing was consumed yet: the quota is reserved, the question is stored and the provider was not called.
        self.assertEqual(self.usage("reserved").mapped("request_id"), [request_id])
        self.assertEqual(self.messages_of(self.user), 1)
        self.assertTrue(self.session().inflight_id)
        self.assertFalse(fake.started)
        stream.close()  # never started: nothing to clean
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("Otra", request_id=str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "busy", "the first one still holds the user's in-flight slot")

    def test_preparation_errors_are_raised_like_chat(self):
        self.provider()
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("   ")
        self.assertEqual(caught.exception.code, "invalid_message")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("x" * 2001)
        self.assertEqual(caught.exception.code, "invalid_message")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("hola", request_id="no-es-uuid")
        self.assertEqual(caught.exception.code, "invalid_message")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("hola", session_generation=self.eng._session_generation() + 5)
        self.assertEqual(caught.exception.code, "session_changed")
        self.P.set_param("buddy_ia.api_key", "")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("hola")
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(self.usage().mapped("state"), [], "nothing was reserved")

    def test_quota_is_the_same_as_chat(self):
        self.P.set_param("buddy_ia.rate_per_minute", "1")
        self.provider()
        self.assertEqual(self.run_stream()[-1][0], "done")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.chat_stream("Otra")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.usage()), 1)

    def test_duplicate_request_id_returns_the_cached_answer_without_calling_the_ai(self):
        fake = self.provider()
        request_id = str(uuid.uuid4())
        first = self.run_stream(request_id=request_id)[-1][1]
        calls = len(fake.calls)
        again = self.run_stream(request_id=request_id)
        self.assertEqual(again, [("done", first)])
        self.assertEqual(len(fake.calls), calls)
        self.assertEqual(len(self.usage()), 1)
        # ... and the plain JSON route (the widget's fallback) recovers the same answer.
        with patch.object(self.cls, "_ask_model", side_effect=AssertionError("no AI call")):
            self.assertEqual(self.eng.chat("Pregunta", request_id=request_id), first)

    def test_needs_human_has_no_sources_and_proposes_contact_and_ticket(self):
        self.provider(output=HUMAN)
        done = self.run_stream("¿Y el sueldo?")[-1][1]
        self.assertEqual((done["ring"], done["sources"]), ("doubt", []))
        self.assertEqual(done["ticket"]["title"], "Consulta: sueldo")
        self.assertEqual(done["contact"]["role"], "general")

    def test_a_json_fence_is_tolerated_but_the_content_stays_strict(self):
        self.provider(fence=True)
        events = self.run_stream()
        self.assertEqual(events[-1][0], "done")
        self.assertEqual("".join(p["text"] for k, p in events if k == "delta"), VALID["answer"])
        self.provider(output=dict(VALID, expression="no-existe"), fence=True)
        self.assertEqual(self.run_stream("Otra")[-1][1]["error"], "invalid_response")
        # A key outside the contract is ignored, not an error (the required ones stay strict).
        self.provider(output=dict(VALID, extra="x"), fence=True)
        self.assertEqual(self.run_stream("Una más")[-1][0], "done")

    def test_answer_in_the_middle_of_the_keys_also_streams(self):
        reordered = json.dumps({"expression": "happy", "answer": "Al medio", "needs_human": False, "contact_role": None,
                                "ticket": None})
        self.provider(fragments=[reordered[i:i + 5] for i in range(0, len(reordered), 5)])
        events = self.run_stream()
        self.assertEqual("".join(p["text"] for k, p in events if k == "delta"), "Al medio")
        self.assertEqual(events[-1][1]["answer"], "Al medio")

    # --- greeting and sources of the answer ---------------------------------------------------------------------
    def test_an_opening_greeting_is_neither_streamed_nor_stored_unless_the_person_greeted(self):
        answer = "¡Hola, Ana! Pedí el reembolso desde Gastos con el comprobante."
        clean = "Pedí el reembolso desde Gastos con el comprobante."
        for size in (1, 7, 300):
            self.provider(output=dict(VALID, answer=answer), size=size)
            events = self.run_stream("¿Cómo pido el reembolso?")
            self.assertEqual(events[-1][0], "done")
            shown = "".join(payload["text"] for kind, payload in events if kind == "delta")
            self.assertEqual(shown, clean, size)
            self.assertEqual(events[-1][1]["answer"], clean, "the final answer agrees with what was streamed")
            self.assertNotIn("Hola", shown)
        stored = self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id), ("role", "=", "assistant")])
        self.assertEqual(set(stored.mapped("content")), {clean}, "history keeps the answer without the greeting")
        self.provider(output=dict(VALID, answer=answer))
        events = self.run_stream("Hola, ¿cómo pido el reembolso?")
        self.assertEqual("".join(p["text"] for k, p in events if k == "delta"), answer)
        self.assertEqual(events[-1][1]["answer"], answer, "she greeted: the greeting stays")

    def test_the_streamed_response_lists_only_the_documents_the_model_used(self):
        self.provider(output=dict(VALID, sources_used=[1]))
        self.assertEqual([row["title"] for row in self.run_stream()[-1][1]["sources"]], ["Guía"])
        self.provider(output=dict(VALID, answer="Eso no es lo mío.", sources_used=[]))
        done = self.run_stream("¿Quién ganó el partido?")[-1][1]
        self.assertEqual((done["sources"], done["ticket"], done["contact"], done["ring"]), ([], None, None, "ok"))
        self.provider(output=VALID)  # it did not say: every retrieved document, as before
        self.assertEqual([row["title"] for row in self.run_stream("Otra más")[-1][1]["sources"]], ["Guía"])
        self.provider(output=dict(VALID, sources_used=[40, 41]))  # ids that point at nothing: do not trust the list
        self.assertEqual([row["title"] for row in self.run_stream("Y otra")[-1][1]["sources"]], ["Guía"])

    # --- failures ---------------------------------------------------------------------------------------------
    def assert_released(self, code, stored_messages=0):
        self.assertEqual(self.usage("failed").mapped("error_code"), [code])
        self.assertEqual(self.messages_of(self.user), stored_messages, "no text, partial or not, is kept")
        self.assertFalse(self.session().inflight_id)

    def test_provider_error_midway_releases_the_reservation(self):
        fake = self.provider(fragments=pieces(VALID)[:3], error=ProviderError("unavailable", "boom"))
        events = self.run_stream()
        self.assertEqual([k for k, p in events][-1], "error")
        self.assertEqual(events[-1][1], {"error": "provider_error"})
        self.assertIn("delta", [k for k, p in events])
        self.assert_released("provider_error")
        self.assertTrue(fake.closed)
        # The user can ask again right away (nothing is left in flight).
        self.provider()
        self.assertEqual(self.run_stream("Otra vez")[-1][0], "done")

    def test_error_codes_follow_the_non_streaming_mapping(self):
        for error, code in ((TimeoutError("lento"), "provider_timeout"), (ValueError("incompleta"), "invalid_response"),
                            (RuntimeError("raro"), "provider_error")):
            with self.subTest(code=code):
                self.provider(fragments=pieces(VALID)[:2], error=error)
                self.assertEqual(self.run_stream("Otra %s" % code)[-1][1]["error"], code)

    def test_a_stream_that_is_not_valid_json_is_an_error_and_stores_nothing(self):
        self.provider(fragments=["Esto no ", "es JSON"])
        events = self.run_stream()
        self.assertEqual(events, [("error", {"error": "invalid_response"})])
        self.assert_released("invalid_response")

    def test_an_endless_stream_is_cut(self):
        self.provider(fragments=['{"answer": "'] + ["x" * 1000] * 20)
        events = self.run_stream()
        self.assertEqual(events[-1], ("error", {"error": "invalid_response"}))
        self.assert_released("invalid_response")

    def test_client_disconnect_releases_and_stores_nothing(self):
        fake = self.provider(fragments=pieces(VALID, size=3))
        stream = self.eng.chat_stream("Pregunta")
        self.assertEqual(next(stream)[0], "delta")
        stream.close()  # what the web server does when the browser goes away
        self.assert_released("client_disconnected")
        self.assertTrue(fake.closed, "the provider connection is closed too")
        self.assertEqual(self.env["buddy.message"].sudo().search_count([("user_id", "=", self.user.id), ("role", "=", "assistant")]), 0)

    def test_disconnect_after_the_answer_was_saved_keeps_it(self):
        self.provider()
        stream = self.eng.chat_stream("Pregunta")
        last = None
        for event in stream:
            last = event
            if event[0] == "done":
                break
        stream.close()
        self.assertEqual(last[0], "done")
        self.assertEqual(self.usage("done").mapped("state"), ["done"])
        self.assertEqual(self.messages_of(self.user), 2)

    def test_late_answer_after_new_conversation_is_discarded(self):
        self.provider(fragments=pieces(VALID, size=3))
        generation = self.eng._session_generation()
        stream = self.eng.chat_stream("Pregunta", session_generation=generation)
        self.assertEqual(next(stream)[0], "delta")
        self.eng.forget()  # "Nueva conversación" while the model is still writing
        events = list(stream)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], "session_changed")
        self.assertGreater(events[-1][1]["session_generation"], generation)
        self.assertEqual(self.messages_of(self.user), 0)
        self.assertEqual(self.usage("done").mapped("state"), [])

    def test_permission_change_during_the_stream_discards_the_answer(self):
        self.provider(fragments=pieces(VALID, size=3))
        stream = self.eng.chat_stream("Pregunta")
        self.assertEqual(next(stream)[0], "delta")
        with patch.object(self.cls, "_authorization_hash", return_value="authorization-B"):
            events = list(stream)
        self.assertEqual(events[-1][1]["error"], "session_changed")
        self.assertEqual(self.messages_of(self.user), 0)

    def test_the_budget_of_the_whole_query_reaches_the_provider(self):
        fake = self.provider()
        self.run_stream()
        self.assertLessEqual(fake.calls[0]["timeout"], 35)
        self.assertGreater(fake.calls[0]["timeout"], 25)


@tagged("post_install", "-at_install")
class TestBuddyStreamPermissions(EngineCase):
    """The real retrieval (no patches): what reaches the provider is only what the user may read."""

    def test_a_user_never_sends_fragments_of_folders_they_cannot_read(self):
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)
        group_a, group_b = new_group(self.env, "A"), new_group(self.env, "B")
        reader = new_user(self.env, "reader", extra_groups=[group_a])
        outsider = new_user(self.env, "outsider", extra_groups=[group_b])
        source = new_source(self.env, "Finanzas", [group_a])
        new_document(self.env, source, "Sueldos", "Las vacaciones de gerencia incluyen el bonus ULTRASECRETO.")
        sent = {}
        for name, user in (("reader", reader), ("outsider", outsider)):
            fake = FakeProvider(pieces(VALID))
            with fake.patch(self.env):
                events = list(self.env["buddy.engine"].with_user(user).chat_stream("vacaciones gerencia bonus"))
            self.assertEqual(events[-1][0], "done")
            sent[name] = json.dumps(fake.calls[0]["messages"])
        self.assertIn("ULTRASECRETO", sent["reader"])
        self.assertNotIn("ULTRASECRETO", sent["outsider"])


@tagged("post_install", "-at_install")
class TestBuddyStreamRoute(HttpCase):
    LOGIN = "buddy_stream_route"

    def setUp(self):
        super().setUp()
        new_test_user(self.env, login=self.LOGIN, groups="base.group_user")
        new_test_user(self.env, login="buddy_stream_portal", groups="base.group_portal")
        new_test_user(self.env, login="buddy_stream_other", groups="base.group_user")
        params = self.env["ir.config_parameter"].sudo()
        for key, value in {"provider": "custom", "api_base": "https://llm.test/v1", "api_key": "not-a-real-key",
                           "chat_model": "qa-chat", "rate_per_minute": "1000", "rate_per_day": "100000"}.items():
            params.set_param("buddy_ia." + key, value)
        self.fake = FakeProvider(pieces(VALID, size=5))
        # The fresh-cursor boundary opens a real second cursor, so a stream that wrongly used the (closed) cursor of
        # the web request would fail here as it would in production.
        for patcher in (patch.object(self.env.registry["buddy.engine"], "_fresh_engine", fresh_test_engine),
                        patch.object(self.env.registry["buddy.engine"], "_authorization_hash", return_value="authorization-A")):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = self.fake.patch(self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def token(self):
        page = self.url_open("/odoo")
        match = re.search(r'csrf_token: "([^"]+)"', page.text)
        self.assertTrue(match, "the backend page carries the CSRF token the widget uses")
        return match.group(1)

    def post(self, body=None, token="auto", content_type="application/json", login=None):
        self.authenticate(login or self.LOGIN, login or self.LOGIN)
        headers = {"Content-Type": content_type}
        if token == "auto":
            token = self.token()
        if token is not None:
            headers["X-CSRF-Token"] = token
        data = body if isinstance(body, str) else json.dumps({"message": "Pregunta", "request_id": str(uuid.uuid4())} if body is None else body)
        return self.url_open("/buddy/chat_stream", data=data, headers=headers)

    @staticmethod
    def frames(text):
        out = []
        for block in text.split("\n\n"):
            lines = [line for line in block.split("\n") if line and not line.startswith(":")]
            if lines:
                out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
        return out

    def post_chunked(self, pieces_):
        """Like `post`, but the body goes out as a chunked upload (no Content-Length): a generator makes requests do that."""
        self.authenticate(self.LOGIN, self.LOGIN)
        headers = {"Content-Type": "application/json", "X-CSRF-Token": self.token()}
        return self.url_open("/buddy/chat_stream", data=(piece for piece in pieces_), headers=headers)

    def test_a_chunked_body_over_the_limit_is_refused(self):
        body = json.dumps({"message": "Pregunta", "pad": "x" * 20000}).encode()
        response = self.post_chunked([body[i:i + 1000] for i in range(0, len(body), 1000)])
        self.assertEqual(response.status_code, 200)
        frames = self.frames(response.text)
        self.assertEqual([kind for kind, unused in frames], ["error"])
        self.assertEqual(frames[0][1]["error"], "invalid_message")
        self.assertEqual(self.fake.calls, [], "the provider was never reached")

    def test_a_small_chunked_body_works_like_any_other(self):
        body = json.dumps({"message": "Pregunta", "request_id": str(uuid.uuid4())}).encode()
        frames = self.frames(self.post_chunked([body[:10], body[10:30], body[30:]]).text)
        self.assertEqual(frames[-1][0], "done")

    def test_control_characters_in_the_question_do_not_break_the_request(self):
        frames = self.frames(self.post({"message": "Pre\u0000gun\u0001ta", "request_id": str(uuid.uuid4())}).text)
        self.assertEqual(frames[-1][0], "done")
        stored = self.env["buddy.message"].sudo().search([("role", "=", "user")], order="id desc", limit=1)
        self.assertEqual(stored.content, "Pregunta")

    def test_a_valid_request_streams_server_sent_events(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertIn("no-cache", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        self.assertTrue(response.text.startswith(": open\n\n"))
        frames = self.frames(response.text)
        self.assertEqual({kind for kind, unused in frames[:-1]}, {"delta"})
        self.assertEqual("".join(payload["text"] for kind, payload in frames[:-1]), VALID["answer"])
        kind, done = frames[-1]
        self.assertEqual((kind, done["answer"], done["error"]), ("done", VALID["answer"], None))
        self.assertEqual(len(self.fake.calls), 1)

    def test_content_type_must_be_json(self):
        for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.post(content_type=content_type).status_code, 415)
        self.assertEqual(self.fake.calls, [])

    def test_the_csrf_token_is_required(self):
        for token in (None, "", "basura", "00000o9999999999"):
            with self.subTest(token=token):
                self.assertEqual(self.post(token=token).status_code, 400)
        self.assertEqual(self.fake.calls, [])

    def test_another_sessions_csrf_token_is_rejected(self):
        stolen = self.token()
        self.assertEqual(self.post(token=stolen, login="buddy_stream_other").status_code, 400)
        self.assertEqual(self.fake.calls, [])

    def test_only_internal_users(self):
        response = self.post(login="buddy_stream_portal")
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.fake.calls, [])
        self.authenticate(None, None)
        self.assertNotEqual(self.url_open("/buddy/chat_stream", data="{}", headers={"Content-Type": "application/json"}).headers.get("Content-Type", "")[:17], "text/event-stream")

    def test_only_post(self):
        self.authenticate(self.LOGIN, self.LOGIN)
        self.assertIn(self.url_open("/buddy/chat_stream").status_code, (404, 405))

    def test_invalid_bodies_are_an_error_event(self):
        for body in ("no json", "[]", "null", {"message": ""}, {"message": 5}, {"message": "x" * 2001},
                     {"message": "hola", "session_generation": "1"}, {"message": "hola", "request_id": "no-uuid"}):
            with self.subTest(body=str(body)[:30]):
                response = self.post(body)
                self.assertEqual(response.status_code, 200)
                (kind, payload), = self.frames(response.text)
                self.assertEqual(kind, "error")
                self.assertEqual(payload["error"], "invalid_message")
                self.assertTrue(payload["answer"])
        self.assertEqual(self.fake.calls, [])

    def test_preparation_errors_arrive_as_a_friendly_error_event(self):
        self.env["ir.config_parameter"].sudo().set_param("buddy_ia.api_key", "")
        (kind, payload), = self.frames(self.post().text)
        self.assertEqual((kind, payload["error"]), ("error", "not_configured"))
        self.assertIn("quien administra Odoo", payload["answer"])
        self.assertEqual(payload["ring"], "doubt")

    def test_provider_failure_is_an_error_event_after_the_stream_started(self):
        self.fake.fragments, self.fake.error = pieces(VALID, size=5)[:2], ProviderError("unavailable", "boom")
        frames = self.frames(self.post().text)
        self.assertEqual(frames[-1][0], "error")
        self.assertEqual(frames[-1][1]["error"], "provider_error")
        self.assertIn("No pude consultar", frames[-1][1]["answer"], "the message was translated before the stream began")

    def test_a_repeated_request_id_is_answered_from_the_stored_answer(self):
        body = {"message": "Pregunta", "request_id": str(uuid.uuid4())}
        first = self.frames(self.post(body).text)[-1][1]
        again = self.frames(self.post(body).text)
        self.assertEqual(again, [("done", first)])
        self.assertEqual(len(self.fake.calls), 1)

    def test_the_json_route_still_works(self):
        self.authenticate(self.LOGIN, self.LOGIN)
        with patch.object(self.env.registry["buddy.engine"], "_ask_model", return_value=dict(VALID)), patch.object(
                self.env.registry["buddy.engine"], "_retrieve", return_value=[]):
            payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {"message": "Hola"}})
            result = self.url_open("/buddy/chat", data=payload, headers={"Content-Type": "application/json"}).json()["result"]
        self.assertEqual(result["answer"], VALID["answer"])

    def test_sse_frames_are_ascii_and_single_event(self):
        frame = sse_frame("delta", {"text": "línea\nnueva   😀 \ud800"})
        frame.decode("ascii")
        self.assertEqual(frame.count(b"\n"), 3)
        self.assertTrue(frame.endswith(b"\n\n"))
        self.assertEqual(json.loads(frame.split(b"data: ")[1])["text"], "línea\nnueva   😀 \ud800")
