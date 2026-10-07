"""Conversation memory shown again by the widget: /buddy/history data, retention clock and the administrator's wipe."""
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from ..models.buddy_engine import HISTORY_LIMIT, BuddyBusy
from .common import FakeClient, drive_file, new_admin, new_document, new_group, new_source, new_user
from .test_engine import CHUNK, VALID, EngineCase


@tagged("post_install", "-at_install")
class TestBuddyHistory(EngineCase):
    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "hist")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, message="Pregunta", engine=None, output=None, chunks=(CHUNK,), **kwargs):
        with patch.object(self.cls, "_retrieve", return_value=list(chunks)), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID if output is None else output)):
            # An abstract-model recordset is always empty (falsy): compare with None.
            return (self.eng if engine is None else engine).chat(message, **kwargs)

    # --- history ------------------------------------------------------------------------------------
    def test_empty_history_for_a_new_user(self):
        result = self.eng.history()
        self.assertEqual(result["messages"], [])
        self.assertGreaterEqual(result["session_generation"], 1)

    def test_history_returns_the_conversation_in_order_with_validated_fields(self):
        request_id = str(uuid.uuid4())
        self.chat("Primera", request_id=request_id)
        human = {"answer": "Eso no está documentado.", "expression": "doubt", "needs_human": True,
                 "contact_role": "general", "ticket": {"title": "Consulta: sueldo", "description": "¿Y el sueldo?"}}
        second_id = str(uuid.uuid4())
        self.chat("¿Y el sueldo?", output=human, request_id=second_id)
        result = self.eng.history()
        messages = result["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual([m["text"] for m in messages if m["role"] == "user"], ["Primera", "¿Y el sueldo?"])
        first = messages[1]
        self.assertEqual((first["text"], first["expression"], first["ring"]), ("Respuesta", "happy", "ok"))
        self.assertEqual([s["title"] for s in first["sources"]], ["Guía"])
        self.assertEqual(first["sources"][0]["link"], "https://docs.google.com/document/d/abc/edit")
        self.assertEqual((first["ticket"], first["contact"], first["ticket_key"]), (None, None, None))
        doubt = messages[3]
        self.assertEqual((doubt["ring"], doubt["sources"]), ("doubt", []))
        self.assertEqual(doubt["ticket"]["title"], "Consulta: sueldo")
        self.assertEqual(doubt["contact"]["role"], "general")
        self.assertEqual(doubt["ticket_key"], second_id, "the ticket proposal is bound to its own request")
        self.assertEqual(result["session_generation"], self.eng._session_generation())

    def test_history_never_leaks_internal_fields(self):
        self.chat()
        for message in self.eng.history()["messages"]:
            self.assertFalse(set(message) & {"request_id", "generation", "response_json", "user_id", "id", "provider", "error"}, message)
        assistant = self.eng.history()["messages"][1]
        self.assertEqual(set(assistant), {"role", "text", "date", "expression", "ring", "sources", "ticket", "contact", "ticket_key", "ticket_created",
                                          "confidence", "conflict", "feedback_token", "feedback"})
        self.assertEqual(set(assistant["sources"][0]), {"title", "owner", "updated", "link"})
        self.assertEqual(set(self.eng.history()["messages"][0]), {"role", "text", "date"})

    def test_history_only_returns_the_users_own_messages(self):
        other = new_user(self.env, "other")
        other_engine = self.env["buddy.engine"].with_user(other)
        self.chat("Secreto de otro", engine=other_engine)
        self.chat("Mío")
        texts = [m["text"] for m in self.eng.history()["messages"]]
        self.assertIn("Mío", texts)
        self.assertNotIn("Secreto de otro", texts)
        self.assertEqual([m["role"] for m in other_engine.history()["messages"]], ["user", "assistant"])

    def test_history_is_limited_to_the_last_messages_in_chronological_order(self):
        generation = self.eng._session_generation()
        for number in range(HISTORY_LIMIT + 10):
            self.eng._remember("user" if number % 2 == 0 else "assistant", "mensaje %d" % number, generation)
        messages = self.eng.history()["messages"]
        self.assertEqual(len(messages), HISTORY_LIMIT)
        self.assertEqual(messages[0]["text"], "mensaje 10")
        self.assertEqual(messages[-1]["text"], "mensaje %d" % (HISTORY_LIMIT + 9))
        self.assertEqual(len(self.eng.history(limit=5000)["messages"]), HISTORY_LIMIT, "a caller cannot widen the window")

    def test_only_the_current_generation_is_returned(self):
        self.chat("Antes de borrar")
        self.eng.forget()
        self.assertEqual(self.eng.history()["messages"], [])
        self.chat("Después")
        self.assertEqual([m["text"] for m in self.eng.history()["messages"] if m["role"] == "user"], ["Después"])
        # A row left behind by an older generation is never shown.
        self.env["buddy.message"].sudo().create({"user_id": self.user.id, "role": "user", "content": "viejo", "generation": 1})
        self.assertNotIn("viejo", [m["text"] for m in self.eng.history()["messages"]])

    def test_a_failed_answer_leaves_no_messages(self):
        with patch.object(self.cls, "_retrieve", side_effect=Exception("boom")):
            try:
                self.eng.chat("Pregunta que falla")
            except Exception:  # noqa: BLE001 (the engine reports it as BuddyBusy; only the stored state matters here)
                pass
        self.assertEqual(self.eng.history()["messages"], [])

    def test_a_permission_change_empties_the_history(self):
        self.chat("Con permisos viejos")
        with patch.object(self.cls, "_authorization_hash", return_value="authorization-B"):
            result = self.eng.history()
        self.assertEqual(result["messages"], [])
        self.assertEqual(self.messages_of(self.user), 0)

    def test_stored_answers_are_validated_again_on_the_way_out(self):
        generation = self.eng._session_generation()
        dirty = {"answer": "x", "expression": "<script>", "ring": "weird", "internal": "no",
                 "sources": [{"title": "Doc", "owner": "Ops", "updated": "2026-01-01", "link": "javascript:alert(1)", "extra": "no"},
                             "garbage", {"title": 5}],
                 "ticket": {"title": "", "description": "sin asunto"}, "contact": "no es un diccionario"}
        self.eng._remember("user", "Pregunta", generation, str(uuid.uuid4()))
        self.eng._remember("assistant", "Respuesta", generation, str(uuid.uuid4()), dirty)
        answer = self.eng.history()["messages"][1]
        self.assertEqual((answer["expression"], answer["ring"]), ("neutral", "ok"))
        self.assertEqual(answer["sources"][0]["link"], "", "only Google's own https hosts reach the browser")
        self.assertNotIn("extra", answer["sources"][0])
        self.assertEqual(len(answer["sources"]), 2, "a non-dictionary source is dropped")
        self.assertIsNone(answer["ticket"])
        self.assertIsNone(answer["contact"])
        self.assertNotIn("internal", answer)

    def test_an_answer_without_stored_json_still_has_text(self):
        generation = self.eng._session_generation()
        self.eng._remember("user", "Pregunta", generation)
        self.eng._remember("assistant", "Respuesta de una versión vieja", generation)
        answer = self.eng.history()["messages"][1]
        self.assertEqual((answer["text"], answer["sources"], answer["ticket"], answer["contact"]),
                         ("Respuesta de una versión vieja", [], None, None))

    def test_portal_users_cannot_read_history(self):
        portal = new_user(self.env, "portalh", groups=("base.group_portal",))
        with self.assertRaises(AccessError):
            self.env["buddy.engine"].with_user(portal).history()

    def test_history_does_not_call_the_ai_or_spend_quota(self):
        self.chat()
        usage = self.env["buddy.usage"].sudo().search_count([("user_id", "=", self.user.id)])
        with patch.object(self.cls, "_ask_model", side_effect=AssertionError("no AI call")), patch.object(
                self.cls, "_retrieve", side_effect=AssertionError("no search")):
            self.eng.history()
            self.eng.history()
        self.assertEqual(self.env["buddy.usage"].sudo().search_count([("user_id", "=", self.user.id)]), usage)

    def test_next_question_after_a_reload_keeps_the_same_conversation(self):
        self.chat("Primera")
        generation = self.eng.history()["session_generation"]
        self.chat("Segunda", session_generation=generation)
        self.assertEqual(len(self.eng.history()["messages"]), 4)

@tagged("post_install", "-at_install")
class TestBuddySyncKeepsConversation(EngineCase):
    """With the REAL fingerprint (not patched): the previous design wiped every history on each Drive sync."""

    def test_a_real_sync_keeps_the_conversation(self):
        self.env["buddy.source"].with_context(active_test=False).search([]).unlink()
        group = new_group(self.env, "SyncKeep")
        employee = new_user(self.env, "keep", extra_groups=[group])
        engine = self.env["buddy.engine"].with_user(employee)
        source = new_source(self.env, "Ventas", [group])
        document = new_document(self.env, source, "Guía", "Contenido.")
        generation = engine._session_generation()
        engine._remember("user", "Pregunta", generation)
        engine._remember("assistant", "Respuesta", generation, response=dict(VALID))
        # A document edited and another one added, then one removed from Drive.
        source._sync(client=FakeClient(
            files=[drive_file("nuevo", "Guía nueva"), drive_file(document.drive_id, "Guía editada", modified="2027-01-01T00:00:00.000Z", md5="z")],
            texts={"nuevo": "Texto nuevo.", document.drive_id: "Texto editado."}))
        source._sync(client=FakeClient(files=[drive_file("nuevo", "Guía nueva")], texts={"nuevo": "Texto nuevo."}))
        self.assertEqual(source.document_count, 1)
        self.assertEqual(engine._session_generation(), generation)
        self.assertEqual([m["text"] for m in engine.history()["messages"]], ["Pregunta", "Respuesta"])


@tagged("post_install", "-at_install")
class TestBuddyRetention(EngineCase):
    """A conversation lives until `retention_days` after its LAST message."""

    def setUp(self):
        super().setUp()
        self.P.set_param("buddy_ia.retention_days", "30")
        self.user = new_user(self.env, "ret")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        self.Message = self.env["buddy.message"].sudo()

    def make(self, user, count=2):
        generation = self.env["buddy.engine"].with_user(user)._session_generation()
        for number in range(count):
            self.env["buddy.engine"].with_user(user)._remember("user" if number % 2 == 0 else "assistant", "m%d" % number, generation)

    def age(self, user, days, newest_only=False, all_but_last=False):
        """Make some of the user's messages `days` old (a TransactionCase has a single now())."""
        rows = self.Message.search([("user_id", "=", user.id)], order="id")
        rows = rows[-1:] if newest_only else rows[:-1] if all_but_last else rows
        self.env.cr.execute("UPDATE buddy_message SET create_date = now() at time zone 'utc' - %s * interval '1 day' WHERE id IN %s",
                            (days, tuple(rows.ids)))
        rows.invalidate_recordset()

    def test_a_recent_conversation_is_kept(self):
        self.make(self.user)
        self.age(self.user, 29)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 2)

    def test_the_whole_conversation_goes_after_n_days_without_messages(self):
        self.make(self.user)
        self.age(self.user, 31)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 0)

    def test_the_clock_runs_from_the_last_message_not_from_the_first(self):
        # Started 40 days ago, last message yesterday: the user is still in that conversation and keeps all of it.
        self.make(self.user, count=4)
        self.age(self.user, 40, all_but_last=True)
        self.age(self.user, 1, newest_only=True)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 4)

    def test_only_inactive_users_lose_their_conversation(self):
        other = new_user(self.env, "ret2")
        self.make(self.user)
        self.make(other)
        self.age(self.user, 45)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 0)
        self.assertEqual(self.Message.search_count([("user_id", "=", other.id)]), 2)

    def test_zero_days_does_not_delete_by_inactivity(self):
        self.P.set_param("buddy_ia.retention_days", "0")
        self.make(self.user)
        self.age(self.user, 179)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 2)
        # ... but nothing outlives the hard ceiling (see TestBuddyMaxAge).

    def test_the_setting_is_respected(self):
        self.P.set_param("buddy_ia.retention_days", "7")
        self.make(self.user)
        self.age(self.user, 8)
        self.Message._cron_purge()
        self.assertEqual(self.Message.search_count([("user_id", "=", self.user.id)]), 0)

    def test_purged_history_is_empty_for_the_widget(self):
        self.make(self.user)
        self.age(self.user, 31)
        self.Message._cron_purge()
        self.assertEqual(self.eng.history()["messages"], [])


@tagged("post_install", "-at_install")
class TestBuddyDeleteAllConversations(EngineCase):
    def setUp(self):
        super().setUp()
        self.ana = new_user(self.env, "ana_del")
        self.leo = new_user(self.env, "leo_del")
        self.admin = new_admin(self.env)
        self.admin_engine = self.env["buddy.engine"].with_user(self.admin)

    def talk(self, user):
        engine = self.env["buddy.engine"].with_user(user)
        generation = engine._session_generation()
        engine._remember("user", "Pregunta de " + user.name, generation)
        return generation

    def test_it_deletes_everybody_and_invalidates_every_session(self):
        before = {user: self.talk(user) for user in (self.ana, self.leo)}
        usage = self.env["buddy.usage"].sudo().search_count([])
        count = self.admin_engine._delete_all_conversations()
        self.assertEqual(count, 2)
        self.assertEqual(self.env["buddy.message"].sudo().search_count([]), 0)
        for user, generation in before.items():
            self.assertGreater(self.env["buddy.engine"].with_user(user)._session_generation(), generation)
        self.assertEqual(self.env["buddy.usage"].sudo().search_count([]), usage, "quotas are not reset by a wipe")

    def test_a_late_answer_cannot_bring_the_text_back(self):
        engine = self.env["buddy.engine"].with_user(self.ana)
        generation = engine._session_generation()
        token = engine._start_request("pendiente", engine._cfg(), generation, str(uuid.uuid4()))
        self.admin_engine._delete_all_conversations()
        with self.assertRaises(BuddyBusy):
            engine._request_live(token, response={"answer": "tarde"})
        self.assertEqual(self.env["buddy.message"].sudo().search_count([]), 0)

    def test_only_administrators(self):
        self.talk(self.ana)
        with self.assertRaises(AccessError):
            self.env["buddy.engine"].with_user(self.ana)._delete_all_conversations()
        self.assertEqual(self.env["buddy.message"].sudo().search_count([]), 1)

    def test_the_settings_button_is_for_administrators_and_confirms_in_the_view(self):
        self.talk(self.ana)
        settings = self.env["res.config.settings"].with_user(self.ana).new({})
        with self.assertRaises(AccessError):
            settings.action_buddy_delete_all_conversations()
        self.assertEqual(self.env["buddy.message"].sudo().search_count([]), 1)
        admin_settings = self.env["res.config.settings"].with_user(self.admin).new({})
        action = admin_settings.action_buddy_delete_all_conversations()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(self.env["buddy.message"].sudo().search_count([]), 0)
        view = self.env["ir.ui.view"].search([("model", "=", "res.config.settings"), ("arch_db", "like", "action_buddy_delete_all_conversations")])
        self.assertTrue(view and "confirm=" in view.arch_db, "the button asks for confirmation")
