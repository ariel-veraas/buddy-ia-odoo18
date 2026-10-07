"""Feedback (👍/👎) on the answers, and contradictions / confidence (E3): the anonymous row, who may vote, one vote per
answer, limits, retention, the history with the vote, the panel's access rights and the /buddy/feedback route."""
import json
import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged

from ..models.buddy_engine import FEEDBACK_MAX_CHANGES, FEEDBACK_PER_MINUTE, BuddyBusy
from ..models.buddy_feedback import MIN_AUDIENCE_PARAM, REASON_CODES
from .common import new_document, new_group, new_source, new_user
from .test_engine import CHUNK, VALID, EngineCase
from .test_streaming import FakeProvider, fresh_test_engine, pieces

INSIGHT_QUESTIONS = "buddy_ia.insight_questions"


class FeedbackCase(EngineCase):
    """A user, an engine whose model answers with `VALID` over the fixed chunk, and helpers to vote."""

    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "voter")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Feedback = self.env["buddy.feedback"]
        self.Feedback.sudo().search([]).unlink()

    def chat(self, message="¿Cuánto tarda el reintegro?", engine=None, output=None, chunks=(CHUNK,), **kwargs):
        with patch.object(self.cls, "_retrieve", return_value=list(chunks)), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID if output is None else output)):
            return (self.eng if engine is None else engine).chat(message, **kwargs)

    def rows(self):
        return self.Feedback.sudo().search([])

    def message_of(self, response, user=None):
        return self.env["buddy.message"].sudo().search([("user_id", "=", (user or self.user).id), ("feedback_token", "=", response["feedback_token"])])


@tagged("post_install", "-at_install")
class TestFeedbackAnswers(FeedbackCase):
    """Which answers can be rated, and what the private message keeps."""

    def test_an_answer_about_the_documents_carries_an_opaque_random_code(self):
        first, second = self.chat(), self.chat()
        for response in (first, second):
            self.assertRegex(response["feedback_token"], r"^[A-Za-z0-9_-]{16,64}$")
        self.assertNotEqual(first["feedback_token"], second["feedback_token"])

    def test_the_code_is_kept_on_the_private_message_with_the_cited_documents(self):
        group = new_group(self.env, "G")
        document = new_document(self.env, new_source(self.env, "S", [group]), "Guía", "texto")
        chunk = dict(CHUNK, doc_id=document.id)
        response = self.chat(chunks=[chunk])
        message = self.message_of(response)
        self.assertEqual((message.role, message.user_id), ("assistant", self.user))
        self.assertEqual(message.feedback_context, {"docs": [document.id], "model": "qa-chat"})
        self.assertEqual(message.response_json["feedback_token"], response["feedback_token"], "a replayed answer keeps its code")

    def test_a_retried_request_returns_the_same_code(self):
        request_id = str(uuid.uuid4())
        first, second = self.chat(request_id=request_id), self.chat(request_id=request_id)
        self.assertEqual(first["feedback_token"], second["feedback_token"])

    def test_small_talk_and_off_topic_replies_cannot_be_rated(self):
        greeting = self.chat("Hola", output=dict(VALID, answer="¡Hola! Puedo ayudarte con Ventas.", sources_used=[]), chunks=[])
        self.assertIsNone(greeting["feedback_token"])
        off_topic = self.chat("¿Quién ganó el mundial?", output=dict(VALID, sources_used=[], answer="Eso no es lo mío."))
        self.assertIsNone(off_topic["feedback_token"])
        self.assertFalse(self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id), ("feedback_token", "!=", False)]))

    def test_the_honest_not_documented_answer_can_be_rated(self):
        human = {"answer": "Eso no está documentado.", "expression": "doubt", "needs_human": True, "contact_role": "general", "ticket": None}
        self.assertTrue(self.chat(output=human)["feedback_token"])

    def test_an_error_has_no_code(self):
        with patch.object(self.cls, "_retrieve", return_value=[CHUNK]), patch.object(
                self.cls, "_ask_model", side_effect=BuddyBusy("provider_error")), self.assertRaises(BuddyBusy):
            self.eng.chat("Pregunta")
        self.assertFalse(self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id), ("feedback_token", "!=", False)]))

    def test_streamed_answers_get_a_code_too(self):
        fake = FakeProvider(pieces(VALID))
        with patch.object(self.cls, "_retrieve", return_value=[CHUNK]), fake.patch(self.env):
            events = list(self.eng.chat_stream("Pregunta"))
        done = events[-1][1]
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(done["feedback_token"])
        self.assertTrue(self.message_of(done))


@tagged("post_install", "-at_install")
class TestFeedbackVote(FeedbackCase):
    def test_a_vote_creates_one_anonymous_row(self):
        response = self.chat()
        self.assertEqual(self.eng.feedback(response["feedback_token"], "up"), {"vote": "up"})
        row = self.rows()
        self.assertEqual((len(row), row.value, row.reason, row.positive, row.model_name), (1, "up", False, 1.0, "qa-chat"))
        self.assertEqual(row.day, fields.Date.today())
        self.assertFalse(row.question, "a vote does not keep the question unless asked")
        message = self.message_of(response)
        self.assertEqual((message.feedback_vote, message.feedback_id, message.feedback_changes), ("up", row, 1))

    def test_the_row_cannot_say_who_voted(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down", "wrong", True)
        fields_of_row = set(self.Feedback._fields)
        self.assertEqual(fields_of_row, {"id", "display_name", "value", "reason", "day", "visible", "votes", "positive", "question",
                                         "model_name", "document_ids", "document_titles"})
        for name in ("user_id", "partner_id", "login", "message_id", "token", "create_uid", "write_uid", "create_date", "write_date",
                     "feedback_token", "request_id"):
            self.assertNotIn(name, self.Feedback._fields)
        self.env.cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'buddy_feedback'")
        self.assertEqual({column for (column,) in self.env.cr.fetchall()}, {"id", "value", "reason", "day", "visible", "votes", "positive", "question", "model_name"})
        self.env.cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'buddy_feedback_document_rel'")
        self.assertEqual({column for (column,) in self.env.cr.fetchall()}, {"feedback_id", "document_id"})
        self.env.cr.execute("SELECT row_to_json(f)::text FROM buddy_feedback f")
        dump = " ".join(text for (text,) in self.env.cr.fetchall())
        self.assertNotIn(response["feedback_token"], dump)
        self.assertNotIn(self.user.login, dump)
        self.assertNotIn(self.user.name, dump)

    def test_voting_again_changes_the_same_row(self):
        response = self.chat()
        token = response["feedback_token"]
        self.eng.feedback(token, "up")
        row = self.rows()
        self.eng.feedback(token, "down", "incomplete")
        self.assertEqual(self.rows(), row, "the same row, not a second vote")
        self.assertEqual((row.value, row.reason, row.positive), ("down", "incomplete", 0.0))
        self.eng.feedback(token, "up", "incomplete")
        self.assertEqual((row.value, row.reason, row.positive), ("up", False, 1.0), "a reason belongs to a 👎 only")
        self.assertEqual(self.message_of(response).feedback_vote, "up")

    def test_taking_the_vote_back_deletes_the_row(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down", "wrong", True)
        self.assertEqual(self.eng.feedback(response["feedback_token"], "none"), {"vote": None})
        self.assertFalse(self.rows())
        message = self.message_of(response)
        self.assertEqual((message.feedback_vote, message.feedback_id), (False, self.Feedback))
        self.eng.feedback(response["feedback_token"], "up")  # and it can be given again
        self.assertEqual(len(self.rows()), 1)

    def test_taking_back_a_vote_that_was_never_given_is_harmless(self):
        response = self.chat()
        self.assertEqual(self.eng.feedback(response["feedback_token"], "none"), {"vote": None})
        self.assertFalse(self.rows())

    def test_every_reason_of_the_closed_list_is_accepted(self):
        for reason in REASON_CODES:
            response = self.chat()
            self.eng.feedback(response["feedback_token"], "down", reason)
            self.assertEqual(self.message_of(response).feedback_id.reason, reason)

    def test_a_downvote_can_come_without_a_reason(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down")
        self.assertEqual((self.rows().value, self.rows().reason), ("down", False))

    def test_the_documents_the_answer_cited_travel_with_the_vote(self):
        self.P.set_param(MIN_AUDIENCE_PARAM, "1")
        group = new_group(self.env, "G")
        source = new_source(self.env, "S", [group])
        cited, other = new_document(self.env, source, "Citado", "a"), new_document(self.env, source, "Otro", "b")
        response = self.chat(chunks=[dict(CHUNK, doc_id=cited.id), dict(CHUNK, id=2, doc_id=other.id)], output=dict(VALID, sources_used=[1]))
        self.eng.feedback(response["feedback_token"], "down")
        self.assertEqual(self.rows().document_ids, cited, "only what the answer used, not everything the search found")

    def test_the_model_name_comes_from_the_answer_not_from_the_browser(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "up")
        self.assertEqual(self.rows().model_name, "qa-chat")


@tagged("post_install", "-at_install")
class TestFeedbackQuestion(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.P.search([("key", "=", INSIGHT_QUESTIONS)]).unlink()

    def test_the_question_is_not_stored_by_default(self):
        response = self.chat("¿Cuánto tarda el reintegro de gastos?")
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        self.assertFalse(self.rows().question)

    def test_the_question_is_stored_only_when_the_person_ticked_the_box(self):
        response = self.chat("¿Cuánto tarda el reintegro de gastos?")
        self.eng.feedback(response["feedback_token"], "down", "wrong", True)
        self.assertEqual(self.rows().question, "¿Cuánto tarda el reintegro de gastos?")

    def test_the_text_is_the_persons_own_message_never_something_the_browser_sends(self):
        response = self.chat("Mi pregunta de verdad")
        other = self.chat("Otra pregunta distinta")
        self.eng.feedback(response["feedback_token"], "down", None, True)
        self.assertEqual(self.rows().question, "Mi pregunta de verdad")
        self.assertNotEqual(self.rows().question, "Otra pregunta distinta")
        self.assertTrue(other)

    def test_changing_the_vote_without_the_box_drops_the_stored_question(self):
        response = self.chat("Una pregunta")
        self.eng.feedback(response["feedback_token"], "down", None, True)
        self.assertTrue(self.rows().question)
        self.eng.feedback(response["feedback_token"], "up")
        self.assertFalse(self.rows().question)

    def test_the_global_setting_that_keeps_every_question_keeps_it_with_a_downvote(self):
        self.P.set_param(INSIGHT_QUESTIONS, "True")
        response = self.chat("Una pregunta guardada por el ajuste")
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        self.assertEqual(self.rows().question, "Una pregunta guardada por el ajuste")

    def test_an_upvote_never_brings_the_question_not_even_with_the_box_or_the_setting(self):
        response = self.chat("Una pregunta que gustó")
        self.eng.feedback(response["feedback_token"], "up", None, True)
        self.assertFalse(self.rows().question)
        self.P.set_param(INSIGHT_QUESTIONS, "True")
        self.eng.feedback(response["feedback_token"], "up")
        self.assertFalse(self.rows().question)

    def test_a_long_question_is_cut_and_cleaned(self):
        response = self.chat("x" * 1500)
        self.eng.feedback(response["feedback_token"], "down", None, True)
        self.assertEqual(len(self.rows().question), 500)


@tagged("post_install", "-at_install")
class TestFeedbackWhoMayVote(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.other = new_user(self.env, "intruder")
        self.other_eng = self.env["buddy.engine"].with_user(self.other)

    def test_somebody_elses_code_finds_nothing(self):
        response = self.chat()
        with self.assertRaises(BuddyBusy) as caught:
            self.other_eng.feedback(response["feedback_token"], "down", "wrong", True)
        self.assertEqual(caught.exception.code, "feedback_unknown")
        self.assertFalse(self.rows())
        self.assertFalse(self.message_of(response).feedback_vote)

    def test_a_made_up_or_malformed_code_finds_nothing(self):
        for token in ("A" * 22, "x" * 16, "no/valido-con-barra", "a" * 65, "corto", "", "' OR 1=1 --", None, 5, ["a" * 22], {"t": 1}):
            with self.subTest(token=token), self.assertRaises(BuddyBusy) as caught:
                self.eng.feedback(token, "up")
            self.assertIn(caught.exception.code, ("feedback_unknown", "invalid_request"))
        self.assertFalse(self.rows())

    def test_closed_lists_are_enforced_before_touching_anything(self):
        token = self.chat()["feedback_token"]
        for args in ((token, "love"), (token, "UP"), (token, None), (token, ["up"]), (token, "up", "terrible"), (token, "down", 5),
                     (token, "down", "wrong", "yes"), (token, "down", "wrong", 1), (token, "down", "wrong", None)):
            with self.subTest(args=args), self.assertRaises(BuddyBusy) as caught:
                self.eng.feedback(*args)
            self.assertEqual(caught.exception.code, "invalid_request")
        self.assertFalse(self.rows())
        self.assertEqual(self.env["buddy.message"].sudo().search([("feedback_token", "=", token)]).feedback_changes, 0)

    def test_a_made_up_code_costs_no_lock_and_no_session(self):
        newcomer = new_user(self.env, "newcomer")
        engine = self.env["buddy.engine"].with_user(newcomer)
        with self.assertRaises(BuddyBusy) as caught:
            engine.feedback("N" * 22, "up")
        self.assertEqual(caught.exception.code, "feedback_unknown")
        self.assertFalse(self.env["buddy.session"].sudo().search([("user_id", "=", newcomer.id)]), "the session was never even opened")

    def test_a_user_message_is_not_an_answer(self):
        self.chat()
        user_message = self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id), ("role", "=", "user")], limit=1)
        user_message.feedback_token = "U" * 22
        with self.assertRaises(BuddyBusy):
            self.eng.feedback("U" * 22, "up")

    def test_a_code_of_an_old_conversation_stops_working(self):
        response = self.chat()
        self.eng.forget()
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.feedback(response["feedback_token"], "up")
        self.assertEqual(caught.exception.code, "feedback_unknown")

    def test_a_permission_change_discards_the_conversation_and_its_codes(self):
        response = self.chat()
        with patch.object(self.cls, "_authorization_hash", return_value="authorization-B"), self.assertRaises(BuddyBusy):
            self.eng.feedback(response["feedback_token"], "up")

    def test_the_method_is_not_reachable_from_the_generic_rpc(self):
        from odoo.service.model import get_public_method
        with self.assertRaises(AccessError):
            get_public_method(self.eng, "feedback")

    def test_portal_users_cannot_vote(self):
        portal = new_test_user(self.env, login="buddy_fb_portal", groups="base.group_portal")
        with self.assertRaises(AccessError):
            self.env["buddy.engine"].with_user(portal).feedback("A" * 22, "up")


@tagged("post_install", "-at_install")
class TestFeedbackSmallFolders(FeedbackCase):
    """A 👎 on a document that very few people can read would name the voter: the vote counts, the document is left out."""

    def setUp(self):
        super().setUp()
        self.P.search([("key", "=", MIN_AUDIENCE_PARAM)]).unlink()

    def readers(self, group, count):
        for number in range(count):
            new_user(self.env, "reader%d" % number, extra_groups=[group])

    def vote_on(self, group):
        document = new_document(self.env, new_source(self.env, "S", [group]), "Doc %s" % group.id, "texto")
        response = self.chat(chunks=[dict(CHUNK, doc_id=document.id)])
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        return document, self.rows()[0]

    def test_documents_of_a_folder_with_few_readers_are_left_out_but_the_vote_counts(self):
        group = new_group(self.env, "Chica")
        self.readers(group, 3)
        document, row = self.vote_on(group)
        self.assertEqual((row.value, row.reason, row.document_ids), ("down", "wrong", self.env["buddy.document"]))

    def test_documents_of_a_folder_with_enough_readers_are_kept(self):
        group = new_group(self.env, "Grande")
        self.readers(group, 5)
        document, row = self.vote_on(group)
        self.assertEqual(row.document_ids, document)

    def test_the_readers_are_those_who_can_read_ALL_the_cited_folders_not_each_one_alone(self):
        """Two folders of 6 readers each, but only one person reads both: that vote would name her."""
        first, second = new_group(self.env, "Primera"), new_group(self.env, "Segunda")
        for number in range(5):
            new_user(self.env, "solo-a%d" % number, extra_groups=[first])
            new_user(self.env, "solo-b%d" % number, extra_groups=[second])
        new_user(self.env, "ambas", extra_groups=[first, second])
        doc_a = new_document(self.env, new_source(self.env, "A", [first]), "Doc A", "a")
        doc_b = new_document(self.env, new_source(self.env, "B", [second]), "Doc B", "b")
        response = self.chat(chunks=[dict(CHUNK, id=1, doc_id=doc_a.id), dict(CHUNK, id=2, doc_id=doc_b.id)])
        self.eng.feedback(response["feedback_token"], "down")
        self.assertFalse(self.rows().document_ids, "6 readers of each folder, 1 of both: no document is named")
        # each folder alone (the same people at the same time) would have passed
        self.assertEqual(self.Feedback._anonymous_documents(doc_a), doc_a)
        self.assertEqual(self.Feedback._anonymous_documents(doc_b), doc_b)
        self.assertFalse(self.Feedback._anonymous_documents(doc_a | doc_b))

    def test_only_active_internal_users_count_as_readers(self):
        group = new_group(self.env, "Mixta")
        self.readers(group, 4)
        inactive = new_user(self.env, "inactive", extra_groups=[group])
        inactive.active = False
        new_test_user(self.env, login="buddy_fb_portal_reader", groups="base.group_portal").write({"groups_id": [(4, group.id)]})
        document, row = self.vote_on(group)
        self.assertFalse(row.document_ids, "4 active internal readers: the inactive and the portal user do not count")

    def test_the_minimum_is_a_technical_parameter_and_a_bad_value_falls_back_to_the_safe_default(self):
        group = new_group(self.env, "Parametro")
        self.readers(group, 2)
        self.P.set_param(MIN_AUDIENCE_PARAM, "2")
        document, row = self.vote_on(group)
        self.assertEqual(row.document_ids, document)
        for bad in ("no", "0", "-3", "", "999999"):
            self.P.set_param(MIN_AUDIENCE_PARAM, bad)
            self.assertEqual(self.Feedback._min_audience(), 5, bad)


@tagged("post_install", "-at_install")
class TestFeedbackLimits(FeedbackCase):
    def test_one_person_can_vote_on_a_limited_number_of_answers_per_minute(self):
        tokens = [self.chat()["feedback_token"] for unused in range(FEEDBACK_PER_MINUTE + 2)]
        for token in tokens[:FEEDBACK_PER_MINUTE]:
            self.eng.feedback(token, "up")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.feedback(tokens[FEEDBACK_PER_MINUTE], "up")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.rows()), FEEDBACK_PER_MINUTE)
        self.eng.feedback(tokens[0], "down")  # changing a vote already given is not a new answer
        self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id), ("feedback_at", "!=", False)]).write(
            {"feedback_at": fields.Datetime.now() - timedelta(seconds=61)})
        self.eng.feedback(tokens[FEEDBACK_PER_MINUTE], "up")  # a minute later it works again

    def test_the_limit_is_per_person(self):
        other = new_user(self.env, "other-voter")
        other_eng = self.env["buddy.engine"].with_user(other)
        for unused in range(FEEDBACK_PER_MINUTE):
            self.eng.feedback(self.chat()["feedback_token"], "up")
        self.assertEqual(other_eng.feedback(self.chat(engine=other_eng)["feedback_token"], "up"), {"vote": "up"})

    def test_one_answer_can_only_be_changed_a_few_times(self):
        token = self.chat()["feedback_token"]
        for number in range(FEEDBACK_MAX_CHANGES):
            self.eng.feedback(token, "up" if number % 2 else "down")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.feedback(token, "none")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(len(self.rows()), 1)

    def test_a_refused_vote_changes_nothing(self):
        token = self.chat()["feedback_token"]
        for unused in range(FEEDBACK_MAX_CHANGES):
            self.eng.feedback(token, "up")
        before = (self.rows().value, self.message_of({"feedback_token": token}).feedback_changes)
        with self.assertRaises(BuddyBusy):
            self.eng.feedback(token, "down", "wrong", True)
        self.assertEqual((self.rows().value, self.message_of({"feedback_token": token}).feedback_changes), before)


@tagged("post_install", "-at_install")
class TestFeedbackHistoryAndErasure(FeedbackCase):
    def test_the_history_brings_the_code_and_the_vote_so_a_reload_shows_it(self):
        response = self.chat()
        before = self.eng.history()["messages"][1]
        self.assertEqual((before["feedback_token"], before["feedback"]), (response["feedback_token"], None))
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        after = self.eng.history()["messages"][1]
        self.assertEqual((after["feedback_token"], after["feedback"]), (response["feedback_token"], "down"))
        self.eng.feedback(response["feedback_token"], "none")
        self.assertIsNone(self.eng.history()["messages"][1]["feedback"])

    def test_the_history_of_one_person_never_shows_another_persons_code(self):
        other = new_user(self.env, "history-other")
        other_eng = self.env["buddy.engine"].with_user(other)
        mine = self.chat()
        theirs = self.chat(engine=other_eng)
        for message in self.eng.history()["messages"]:
            self.assertNotEqual(message.get("feedback_token"), theirs["feedback_token"])
        self.assertEqual([m.get("feedback_token") for m in self.eng.history()["messages"] if m["role"] == "assistant"], [mine["feedback_token"]])

    def test_the_history_hides_a_code_that_does_not_have_the_expected_shape(self):
        response = self.chat()
        self.message_of(response).feedback_token = "../etc/passwd"
        self.assertIsNone(self.eng.history()["messages"][1]["feedback_token"])

    def test_the_history_never_leaks_the_row_of_the_vote_or_the_documents(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down", "wrong", True)
        item = self.eng.history()["messages"][1]
        self.assertFalse(set(item) & {"feedback_id", "feedback_context", "feedback_changes", "feedback_at", "document_ids", "docs", "model"})

    def test_deleting_the_conversation_leaves_the_vote_with_no_link_to_anybody(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        self.eng.forget()
        self.assertEqual(len(self.rows()), 1, "the anonymous vote survives «Nueva conversación»")
        self.assertFalse(self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id)]))

    def test_retention_of_the_conversation_does_not_touch_the_votes(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "up")
        self.message_of(response).sudo()  # exists
        self.env["buddy.message"]._purge_older_than(-1)  # everything is "older" than tomorrow
        self.assertFalse(self.env["buddy.message"].sudo().search([("user_id", "=", self.user.id)]))
        self.assertEqual(len(self.rows()), 1)

    def test_the_wipe_button_also_clears_the_vote_the_private_message_remembered(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "down", "wrong")
        message = self.message_of(response)
        self.assertEqual((message.feedback_vote, bool(message.feedback_id), bool(message.feedback_at)), ("down", True, True))
        self.env["buddy.insight"]._delete_everything()
        message = self.message_of(response)
        self.assertEqual((message.feedback_vote, bool(message.feedback_id), bool(message.feedback_at)), (False, False, False))
        self.assertEqual(self.eng.history()["messages"][1]["feedback"], None)
        self.assertEqual(self.eng.feedback(response["feedback_token"], "up"), {"vote": "up"}, "the person can vote again")

    def test_deleting_the_vote_row_clears_the_pointer_of_the_message(self):
        response = self.chat()
        self.eng.feedback(response["feedback_token"], "up")
        self.rows().unlink()
        self.assertFalse(self.message_of(response).feedback_id)
        self.eng.feedback(response["feedback_token"], "down")  # the person can vote again: a new row
        self.assertEqual(len(self.rows()), 1)


@tagged("post_install", "-at_install")
class TestFeedbackRetentionAndErasure(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Feedback = self.env["buddy.feedback"]
        self.Feedback.sudo().search([]).unlink()
        self.P = self.env["ir.config_parameter"].sudo()

    def make(self, days_ago, value="up"):
        return self.Feedback._store(self.Feedback, {"value": value}).sudo().write({"day": fields.Date.today() - timedelta(days=days_ago)})

    def test_old_votes_are_deleted_with_the_retention_of_the_statistics(self):
        self.P.set_param("buddy_ia.insight_days", "30")
        for age in (1, 29, 31, 100):
            self.make(age)
        self.env["buddy.insight"]._cron_purge()
        self.assertEqual(sorted((fields.Date.today() - row.day).days for row in self.Feedback.sudo().search([])), [1, 29])

    def test_the_default_retention_is_90_days_and_a_bad_value_never_keeps_forever(self):
        self.P.search([("key", "=", "buddy_ia.insight_days")]).unlink()
        self.make(89)
        self.make(91)
        self.Feedback._cron_purge()
        self.assertEqual(len(self.Feedback.sudo().search([])), 1)
        self.P.set_param("buddy_ia.insight_days", "no-es-un-numero")
        self.Feedback._cron_purge()
        self.assertEqual(len(self.Feedback.sudo().search([])), 1)

    def test_the_administrators_wipe_button_deletes_the_votes_and_their_questions_too(self):
        self.Feedback._store(self.Feedback, {"value": "down", "keep_question": True, "question": "algo"})
        admin = new_test_user(self.env, login="buddy_fb_wipe", groups="base.group_user,base.group_system")
        self.env["res.config.settings"].with_user(admin).create({}).action_buddy_delete_insights()
        self.assertFalse(self.Feedback.sudo().search([]))

    def test_the_closed_lists_are_enforced_by_the_model_too(self):
        with self.assertRaises(ValueError):
            self.Feedback._store(self.Feedback, {"value": "meh"})
        row = self.Feedback._store(self.Feedback, {"value": "down", "reason": "not-in-the-list"})
        self.assertFalse(row.reason)
        row = self.Feedback._store(self.Feedback, {"value": "up", "reason": "wrong"})
        self.assertFalse(row.reason)

    def test_the_question_rule_lives_in_the_model(self):
        self.assertFalse(self.Feedback._store(self.Feedback, {"value": "down", "question": "x", "keep_question": False}).question)
        self.assertEqual(self.Feedback._store(self.Feedback, {"value": "down", "question": " x\u0000y ", "keep_question": True}).question, "xy")


@tagged("post_install", "-at_install")
class TestFeedbackAccessRights(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Feedback = self.env["buddy.feedback"]
        self.env["ir.config_parameter"].sudo().set_param(MIN_AUDIENCE_PARAM, "1")
        group = new_group(self.env, "Docs")
        document = new_document(self.env, new_source(self.env, "S", [group]), "Documento secreto", "t")
        self.row = self.Feedback._store(self.Feedback, {"value": "down", "reason": "wrong", "keep_question": True,
                                                        "question": "¿algo?", "document_ids": [document.id]})
        self.document = document
        self.employee = new_test_user(self.env, login="buddy_fb_emp", groups="base.group_user")
        self.manager = new_test_user(self.env, login="buddy_fb_mgr", groups="base.group_user,buddy_ia.group_buddy_manager")
        self.admin = new_test_user(self.env, login="buddy_fb_adm", groups="base.group_user,base.group_system")
        # Managers only see the votes of earlier days (see test_managers_see_votes_from_the_next_day): age this one.
        self.row.write({"day": fields.Date.today() - timedelta(days=1)})
        self.Feedback._reveal()

    def test_an_employee_cannot_see_the_votes_not_even_their_own(self):
        with self.assertRaises(AccessError):
            self.Feedback.with_user(self.employee).search([])
        with self.assertRaises(AccessError):
            self.row.with_user(self.employee).read(["value"])

    def test_the_manager_and_the_administrator_can_read_but_nobody_can_change_anything(self):
        for user in (self.manager, self.admin):
            rows = self.Feedback.with_user(user).search([("id", "=", self.row.id)])
            self.assertEqual((rows.value, rows.reason, rows.question), ("down", "wrong", "¿algo?"))
            with self.assertRaises(AccessError):
                rows.write({"value": "up"})
            with self.assertRaises(AccessError):
                rows.unlink()
            with self.assertRaises(AccessError):
                self.Feedback.with_user(user).create({"value": "up", "day": fields.Date.today()})

    def test_the_managers_see_the_titles_they_may_read_but_not_the_document_records(self):
        self.assertFalse(self.row.with_user(self.manager).document_titles, "the manager cannot read that folder")
        self.assertEqual(self.row.with_user(self.admin).document_titles, "Documento secreto")
        with self.assertRaises(AccessError):
            self.row.with_user(self.manager).document_ids  # noqa: B018 (reading it is the test)
        self.assertEqual(self.row.with_user(self.admin).document_ids.name, "Documento secreto")

    def test_managers_see_votes_from_the_next_day_and_administrators_at_once(self):
        today = self.Feedback._store(self.Feedback, {"value": "up"})
        self.assertEqual(self.Feedback.with_user(self.manager).search([]), self.row, "today's vote is not visible to a manager yet")
        self.assertEqual(self.Feedback.with_user(self.admin).search([]), self.row | today)
        self.assertEqual(self.Feedback.with_user(self.manager).search_count([]), 1)
        self.assertEqual(self.Feedback._reveal(), 0, "the cron only reveals rows of earlier days: today's stays hidden")
        today.write({"day": fields.Date.today() - timedelta(days=1)})
        self.assertEqual(self.Feedback.with_user(self.manager).search_count([]), 1, "a changed date alone reveals nothing: the flag does")
        self.assertEqual(self.Feedback._reveal(), 1)
        self.assertEqual(self.Feedback.with_user(self.manager).search([]), self.row | today)

    def test_the_daily_statistics_cron_reveals_the_votes_and_a_rewritten_vote_is_hidden_again(self):
        fresh = self.Feedback._store(self.Feedback, {"value": "up"})
        fresh.write({"day": fields.Date.today() - timedelta(days=2)})
        self.env["buddy.insight"]._cron_purge()
        self.assertTrue(fresh.visible)
        self.assertEqual(self.Feedback.with_user(self.manager).search_count([("id", "=", fresh.id)]), 1)
        again = self.Feedback._store(fresh, {"value": "down", "reason": "wrong"})
        self.assertEqual((again, again.visible, again.day), (fresh, False, fields.Date.today()), "changing a vote hides it until the next day")
        self.assertEqual(self.Feedback.with_user(self.manager).search_count([("id", "=", fresh.id)]), 0)

    def test_nobody_but_administrators_can_search_or_group_by_the_cited_documents(self):
        """Odoo refuses to READ a field with groups=, but lets anybody search or group by it: a yes/no oracle and, by group, the
        names of documents in folders the person cannot see (found by the security review)."""
        manager = self.Feedback.with_user(self.manager)
        calls = (
            lambda: manager.search_count([("document_ids", "in", [self.document.id])]),
            lambda: manager.search([("document_ids.name", "ilike", "secreto")]),
            lambda: manager.search_read([("document_ids", "!=", False)], ["value"]),
            lambda: manager.search(["|", ("value", "=", "up"), "&", ("day", "!=", False), ("document_ids", "=", self.document.id)]),
            lambda: manager.read_group([], ["value"], ["document_ids"]),
            lambda: manager._read_group([], ["document_ids"], ["__count"]),
            lambda: manager._read_group([], ["value"], ["document_ids:count"]),
            lambda: manager.web_read_group([], ["value"], ["document_ids"]),
            lambda: manager.search([], order="document_ids"),
        )
        for index, call in enumerate(calls):
            with self.subTest(call=index), self.assertRaises(AccessError):
                call()
        admin = self.Feedback.with_user(self.admin)
        self.assertEqual(admin.search_count([("document_ids", "in", [self.document.id])]), 1)
        self.assertEqual(admin._read_group([], ["document_ids"], ["__count"])[0][0], self.document)

    def test_the_same_guard_protects_the_statistics(self):
        insight = self.env["buddy.insight"]._record({"question": "x", "document_ids": [self.document.id]})
        manager = self.env["buddy.insight"].with_user(self.manager)
        for call in (lambda: manager.search_count([("document_ids", "in", [self.document.id])]),
                     lambda: manager.read_group([], ["tokens_in"], ["document_ids"]),
                     lambda: manager._read_group([], ["document_ids"], ["__count"])):
            with self.assertRaises(AccessError):
                call()
        self.assertEqual(manager.search([("id", "=", insight.id)]), insight, "everything else still works")
        self.assertEqual(manager.read_group([], ["tokens_in:sum"], ["outcome"])[0]["outcome_count"] >= 1, True)
        self.assertEqual(self.env["buddy.insight"].with_user(self.admin).search_count([("document_ids", "in", [self.document.id])]), 1)

    def test_the_title_filter_of_the_statistics_still_works_for_a_manager(self):
        self.env["buddy.insight"]._record({"question": "x", "document_ids": [self.document.id]})
        found = self.env["buddy.insight"].with_user(self.manager).search([("document_titles", "ilike", "secreto")])
        self.assertFalse(found, "the manager cannot read that folder: no title, no match")
        self.assertTrue(self.env["buddy.insight"].with_user(self.admin).search([("document_titles", "ilike", "secreto")]))

    def test_the_model_has_no_public_rpc_method_of_its_own(self):
        from .test_rpc_surface import own_public_methods
        self.assertEqual(own_public_methods(self.env, "buddy.feedback"), set())


@tagged("post_install", "-at_install")
class TestContradictionsAndConfidence(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.P.set_param("buddy_ia.provider", "custom")
        group = new_group(self.env, "G")
        source = new_source(self.env, "S", [group])
        self.doc_a = new_document(self.env, source, "Política A", "Reintegro en 15 días hábiles.")
        self.doc_b = new_document(self.env, source, "Política B", "Reintegro en 30 días corridos.")
        self.chunks = [dict(CHUNK, id=1, doc_id=self.doc_a.id, title="Política A"), dict(CHUNK, id=2, doc_id=self.doc_b.id, title="Política B")]
        self.conflict = dict(VALID, answer="Encontré información distinta: Política A dice 15 días hábiles y Política B 30 días corridos. "
                                           "Confirmalo con RR.HH.", expression="doubt", contact_role="rrhh", sources_used=[1, 2],
                             confidence="medium", conflict=True)

    def test_a_contradiction_cites_both_documents_and_points_to_the_contact_without_a_ticket(self):
        response = self.chat(chunks=self.chunks, output=self.conflict)
        self.assertEqual([s["title"] for s in response["sources"]], ["Política A", "Política B"])
        self.assertEqual((response["conflict"], response["confidence"], response["ring"]), (True, "medium", "doubt"))
        self.assertEqual(response["contact"]["role"], "rrhh")
        self.assertIsNone(response["ticket"], "it did answer: no ticket is proposed")
        self.assertTrue(response["feedback_token"])

    def test_weak_evidence_offers_the_contact_too(self):
        output = dict(VALID, answer="No estoy del todo seguro, pero el documento solo menciona los viáticos.", expression="doubt",
                      confidence="low", conflict=False, sources_used=[1])
        response = self.chat(output=output)
        self.assertEqual((response["conflict"], response["confidence"], response["ring"]), (False, "low", "doubt"))
        self.assertTrue(response["contact"])
        self.assertEqual([s["title"] for s in response["sources"]], ["Guía"])

    def test_a_plain_answer_has_no_contact_and_a_calm_ring(self):
        response = self.chat(output=dict(VALID, confidence="high", conflict=False))
        self.assertEqual((response["confidence"], response["conflict"], response["ring"], response["contact"]), ("high", False, "ok", None))

    def test_a_missing_confidence_or_conflict_takes_its_default(self):
        response = self.chat(output=dict(VALID))
        self.assertEqual((response["confidence"], response["conflict"]), (None, False))

    def test_silly_values_are_tolerated_not_fatal(self):
        response = self.chat(output=dict(VALID, confidence="altísima", conflict="sí"))
        self.assertEqual((response["confidence"], response["conflict"], response["ring"]), (None, False, "ok"))

    def test_a_list_of_documents_in_conflict_counts_as_a_conflict(self):
        response = self.chat(output=dict(VALID, conflict=[1, 2]))
        self.assertTrue(response["conflict"])

    def test_extra_keys_are_still_ignored(self):
        response = self.chat(output=dict(VALID, confidence="low", ignorar_esto="<script>"))
        self.assertNotIn("ignorar_esto", response)

    def test_the_history_keeps_the_note(self):
        self.chat(chunks=self.chunks, output=self.conflict)
        item = self.eng.history()["messages"][1]
        self.assertEqual((item["conflict"], item["confidence"], item["ring"]), (True, "medium", "doubt"))
        self.assertEqual(item["contact"]["role"], "rrhh")

    def test_a_streamed_contradiction_has_the_same_shape(self):
        fake = FakeProvider(pieces(self.conflict))
        with patch.object(self.cls, "_retrieve", return_value=self.chunks), fake.patch(self.env):
            done = list(self.eng.chat_stream("¿Cuánto tarda el reintegro?"))[-1][1]
        self.assertEqual((done["conflict"], done["confidence"], done["ring"]), (True, "medium", "doubt"))
        self.assertEqual(done["contact"]["role"], "rrhh")
        self.assertEqual(len(done["sources"]), 2)

    def test_the_statistics_row_records_the_confidence_and_the_conflict_but_no_text(self):
        self.env["buddy.insight"].sudo().search([]).unlink()
        self.chat(chunks=self.chunks, output=self.conflict)
        row = self.env["buddy.insight"].sudo().search([], limit=1)
        self.assertEqual((row.conflict, row.confidence, row.needs_human), (True, "medium", False))
        self.assertFalse(row.question, "an answered question is still not stored")


@tagged("post_install", "-at_install")
class TestFeedbackRoute(HttpCase):
    """/buddy/feedback: CSRF and content type like /buddy/chat_stream, internal users, hard body cap, closed lists."""
    LOGIN = "buddy_fb_route"

    def setUp(self):
        super().setUp()
        self.user = new_test_user(self.env, login=self.LOGIN, groups="base.group_user")
        new_test_user(self.env, login="buddy_fb_route_portal", groups="base.group_portal")
        new_test_user(self.env, login="buddy_fb_route_other", groups="base.group_user")
        params = self.env["ir.config_parameter"].sudo()
        for key, value in {"provider": "custom", "api_base": "https://llm.test/v1", "api_key": "not-a-real-key",
                           "chat_model": "qa-chat", "rate_per_minute": "1000", "rate_per_day": "100000"}.items():
            params.set_param("buddy_ia." + key, value)
        for patcher in (patch.object(self.env.registry["buddy.engine"], "_fresh_engine", fresh_test_engine),
                        patch.object(self.env.registry["buddy.engine"], "_authorization_hash", return_value="authorization-A")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer(self, login=None):
        user = self.env["res.users"].search([("login", "=", login or self.LOGIN)])
        with patch.object(self.env.registry["buddy.engine"], "_retrieve", return_value=[CHUNK]), patch.object(
                self.env.registry["buddy.engine"], "_ask_model", return_value=dict(VALID)):
            return self.env["buddy.engine"].with_user(user).chat("Pregunta")["feedback_token"]

    def csrf(self):
        page = self.url_open("/odoo")
        match = re.search(r'csrf_token: "([^"]+)"', page.text)
        self.assertTrue(match)
        return match.group(1)

    def post(self, body, token="auto", content_type="application/json", login=None):
        self.authenticate(login or self.LOGIN, login or self.LOGIN)
        headers = {"Content-Type": content_type}
        if token == "auto":
            token = self.csrf()
        if token is not None:
            headers["X-CSRF-Token"] = token
        return self.url_open("/buddy/feedback", data=body if isinstance(body, (str, bytes)) else json.dumps(body), headers=headers)

    def rows(self):
        return self.env["buddy.feedback"].sudo().search([])

    def test_a_valid_vote_is_recorded(self):
        token = self.answer()
        response = self.post({"token": token, "vote": "down", "reason": "wrong", "include_question": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "vote": "down"})
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((self.rows().value, self.rows().reason, self.rows().question), ("down", "wrong", "Pregunta"))
        self.assertEqual(self.post({"token": token, "vote": "none"}).json(), {"ok": True, "vote": None})
        self.assertFalse(self.rows())

    def test_content_type_must_be_json(self):
        token = self.answer()
        for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.post({"token": token, "vote": "up"}, content_type=content_type).status_code, 415)
        self.assertFalse(self.rows())

    def test_the_csrf_token_is_required(self):
        token = self.answer()
        for csrf in (None, "", "basura", "00000o9999999999"):
            with self.subTest(csrf=csrf):
                self.assertEqual(self.post({"token": token, "vote": "up"}, token=csrf).status_code, 400)
        self.assertFalse(self.rows())

    def test_another_sessions_csrf_token_is_rejected(self):
        token = self.answer()
        stolen = self.csrf()
        self.assertEqual(self.post({"token": token, "vote": "up"}, token=stolen, login="buddy_fb_route_other").status_code, 400)
        self.assertFalse(self.rows())

    def test_only_internal_users_and_only_post(self):
        self.assertIn(self.post({"token": "A" * 22, "vote": "up"}, login="buddy_fb_route_portal").status_code, (400, 403))
        self.authenticate(None, None)
        anonymous = self.url_open("/buddy/feedback", data="{}", headers={"Content-Type": "application/json"})
        self.assertNotIn("application/json", anonymous.headers.get("Content-Type", ""), "a visitor gets the login page, not the route")
        self.authenticate(self.LOGIN, self.LOGIN)
        self.assertIn(self.url_open("/buddy/feedback").status_code, (404, 405))

    def test_somebody_elses_answer_is_not_found(self):
        token = self.answer(login="buddy_fb_route_other")
        response = self.post({"token": token, "vote": "down"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "feedback_unknown")
        self.assertFalse(self.rows())

    def test_invalid_bodies_are_refused_without_touching_the_database(self):
        token = self.answer()
        for body in ("no json", "[]", "null", "5", {}, {"vote": "up"}, {"token": token}, {"token": token, "vote": 1}, {"token": 1, "vote": "up"},
                     {"token": token, "vote": "up", "extra": 1}, {"token": token, "vote": "up", "reason": 5},
                     {"token": token, "vote": "down", "include_question": "true"}, {"token": token, "vote": "down", "include_question": 1},
                     {"token": token, "vote": "love"}, {"token": token, "vote": "down", "reason": "terrible"}):
            with self.subTest(body=str(body)[:40]):
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["ok"], False)
        self.assertFalse(self.rows())

    def test_the_body_is_capped(self):
        token = self.answer()
        big = json.dumps({"token": token, "vote": "up", "reason": "x" * 5000})
        self.assertEqual(self.post(big).status_code, 413)
        self.assertEqual(self.post(big.encode()).status_code, 413)
        chunked = [big.encode()[i:i + 700] for i in range(0, len(big), 700)]
        self.authenticate(self.LOGIN, self.LOGIN)
        response = self.url_open("/buddy/feedback", data=(piece for piece in chunked),
                                 headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf()})
        self.assertIn(response.status_code, (400, 413))
        self.assertFalse(self.rows())

    def test_the_rate_limit_answers_429_with_a_readable_message(self):
        tokens = [self.answer() for unused in range(FEEDBACK_PER_MINUTE + 1)]
        self.authenticate(self.LOGIN, self.LOGIN)
        headers = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf()}

        def vote(token):
            return self.url_open("/buddy/feedback", data=json.dumps({"token": token, "vote": "up"}), headers=headers)

        for token in tokens[:FEEDBACK_PER_MINUTE]:
            self.assertEqual(vote(token).status_code, 200)
        response = vote(tokens[-1])
        self.assertEqual(response.status_code, 429)
        self.assertIn("votando", response.json()["message"])

    def test_the_config_route_says_whether_every_question_is_kept(self):
        self.authenticate(self.LOGIN, self.LOGIN)
        patcher = patch.object(self.env.registry["buddy.engine"], "_session_generation", return_value=5)
        patcher.start()
        self.addCleanup(patcher.stop)

        def config():
            payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}})
            return self.url_open("/buddy/config", data=payload, headers={"Content-Type": "application/json"}).json()["result"]

        self.assertIs(config()["keep_questions"], False)
        self.env["ir.config_parameter"].sudo().set_param(INSIGHT_QUESTIONS, "True")
        self.assertIs(config()["keep_questions"], True)
