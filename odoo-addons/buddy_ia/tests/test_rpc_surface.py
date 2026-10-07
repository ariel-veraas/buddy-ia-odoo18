"""What an employee can reach through the generic RPC (/web/dataset/call_kw), and the fixes that came out of the final
security review (18.0.4.7.0): the engine is private, the plan carries no API key, the statistics never name the person,
the manager group is not implied by the administrators', archived folders are not readable, hostile Drive text."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import SUPERUSER_ID, fields
from odoo.exceptions import AccessError
from odoo.api import call_kw
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.buddy_engine import LEASE_SECONDS, BuddyBusy
from .common import FakeClient, drive_file, new_document, new_group, new_source, new_user
from .test_engine import CHUNK, VALID, EngineCase

PARAMS = "ir.config_parameter"
API_KEY = "sk-SUPER-SECRETA-NO-SALIR"

# EVERY method of a Buddy model that the generic RPC can call, written down. A new public method has to be added here on
# purpose, by somebody who asked themselves what an employee could do with it (and, if it is a button, where it checks the
# right of the person). The ones that only the controller needs are private (`@api.private` or a leading underscore).
PUBLIC_RPC = {
    "buddy.contact": {"create", "write"},          # the model's access rights (administrators only)
    "buddy.source": {"create", "action_sync", "action_refresh"},  # buttons: each one checks base.group_system
    "buddy.ticket": {"create"},                    # employees create their own (see _check_daily_cap); fields guarded in create()
    # Onboarding (E4): the record rules say who reads what (own rows only for an employee) and every writer checks the right too.
    "buddy.journey": {"create", "write", "copy", "action_assign", "action_duplicate_activate", "action_open_assignments"},
    "buddy.journey.step": {"create", "write", "unlink"},    # the ACL (managers only) + the record rules
    "buddy.journey.assignment": {"create", "write", "action_cancel", "action_reactivate"},  # managers only: checked inside
    "buddy.journey.progress": {"create", "write"},  # an employee may only tick their own step (write); create is administrators'
    "buddy.journey.assign": {"default_get", "action_assign"},  # the «Asignar recorrido» window: managers only
    "res.config.settings": {                       # every button checks base.group_system (see _buddy_require_admin)
        "set_values", "action_buddy_test_connection", "action_buddy_delete_all_conversations", "action_buddy_open_sources",
        "action_buddy_open_contacts", "action_buddy_open_unanswered", "action_buddy_delete_insights",
        "action_buddy_clear_api_key", "action_buddy_clear_drive_key", "action_buddy_purge_semantic",
        # E4b: the weekly summary. Both open or make the summary for an administrator; both start with _buddy_require_admin.
        "action_buddy_open_digests", "action_buddy_digest_now"},
    # E4b: `buddy.proactive` (suggestions of the chips) and `buddy.digest` (weekly summary) have NO public method on purpose: the
    # suggestions are `@api.private` (the controller is the only door) and everything of the summary starts with an underscore (the cron
    # and the settings button call it from Python). Its records are read through the ORM under the ACL, the record rule and the guard.
}
RPC_MODELS = ("buddy.engine", "buddy.provider", "buddy.insight", "buddy.feedback", "buddy.chunk", "buddy.embedding", "buddy.document", "buddy.message",
              "buddy.session", "buddy.usage", "buddy.contact", "buddy.source", "buddy.ticket", "res.config.settings",
              "buddy.journey", "buddy.journey.step", "buddy.journey.assignment", "buddy.journey.progress", "buddy.journey.assign",
              "buddy.journey.engine", "buddy.proactive", "buddy.digest")


def rpc_call(model, name, args):
    """What /web/dataset/call_kw does: the public-method check of the web controller, then the ORM's call_kw."""
    get_public_method(model, name)
    return call_kw(model, name, args, {})


def own_public_methods(env, model_name):
    """The names a Buddy class defines (not the ORM's) that `get_public_method` lets through."""
    model = env[model_name]
    found = set()
    for cls in type(model).mro():
        if not cls.__module__.startswith("odoo.addons.buddy_ia"):
            continue
        for name, value in vars(cls).items():
            if name.startswith("_") or not callable(value) and not isinstance(value, (staticmethod, classmethod)):
                continue
            try:
                get_public_method(model, name)
            except (AccessError, AttributeError):
                continue
            found.add(name)
    return found


@tagged("post_install", "-at_install")
class TestRpcSurface(TransactionCase):
    def setUp(self):
        super().setUp()
        self.employee = new_test_user(self.env, login="buddy_rpc_emp", groups="base.group_user")

    def test_the_public_methods_of_the_buddy_models_are_exactly_the_written_list(self):
        found = {name: own_public_methods(self.env, name) for name in RPC_MODELS}
        found = {name: methods for name, methods in found.items() if methods}
        self.assertEqual(found, PUBLIC_RPC)

    def test_the_engine_and_the_provider_have_no_public_method_at_all(self):
        for name in ("buddy.engine", "buddy.provider", "buddy.insight", "buddy.feedback", "buddy.message", "buddy.session", "buddy.usage", "buddy.embedding",
                     "buddy.journey.engine", "buddy.proactive", "buddy.digest"):
            self.assertEqual(own_public_methods(self.env, name), set(), name)

    def test_an_employee_cannot_call_the_engine_through_the_generic_rpc(self):
        engine = self.env["buddy.engine"].with_user(self.employee)
        for method, args in (("stream_prepare", ["hola"]), ("stream_events", [{"cached": {"answer": "x"}}]),
                             ("chat_stream", ["hola"]), ("chat", ["hola"]), ("history", []), ("forget", []),
                             ("feedback", ["A" * 22, "up"])):
            with self.subTest(method=method), self.assertRaises(AccessError) as caught:
                rpc_call(engine, method, args)
            self.assertIn("cannot be called remotely", str(caught.exception))

    def test_an_employee_cannot_call_the_onboarding_engine_through_the_generic_rpc(self):
        engine = self.env["buddy.journey.engine"].with_user(self.employee)
        for method, args in (("tasks", []), ("set_done", [1, True]), ("_stage_note", [])):
            with self.subTest(method=method), self.assertRaises(AccessError):
                rpc_call(engine, method, args)

    def test_an_employee_cannot_call_the_suggestions_or_the_summary_through_the_generic_rpc(self):
        proactive = self.env["buddy.proactive"].with_user(self.employee)
        for method, args in (("suggestions", []), ("_step_questions", [fields.Date.today()]), ("_topic_questions", []), ("_compute_topics", []), ("_clear_cache", [])):
            with self.subTest(method=method), self.assertRaises(AccessError):
                rpc_call(proactive, method, args)
        digest = self.env["buddy.digest"].with_user(self.employee)
        for method, args in (("_cron_weekly", []), ("_generate", []), ("_generate_now", []), ("_notify", [1]), ("_recipients", []),
                             ("_cron_purge", []), ("_delete_everything", []), ("_section_unanswered", [fields.Date.today(), fields.Date.today()])):
            with self.subTest(method=method), self.assertRaises(AccessError):
                rpc_call(digest, method, args)

    def test_what_the_summary_inherits_from_the_mail_models_is_closed_by_the_access_rights(self):
        """`buddy.digest` inherits `mail.thread` and `mail.activity.mixin`, whose public methods the generic RPC can call. The
        `own_public_methods` check above only looks at what Buddy defines, so this one asks the question the other way round: a
        manager (who can READ the summaries) cannot post on one, schedule activities, subscribe anybody or write it."""
        digest = self.env["buddy.digest"]._generate(fields.Date.today() + timedelta(days=1))[0]
        manager = new_test_user(self.env, login="buddy_rpc_manager", groups="base.group_user,buddy_ia.group_buddy_manager")
        record = digest.with_user(manager)
        self.assertTrue(record.exists())
        calls = (("message_post", [], {"body": "hola"}),
                 ("activity_schedule", [], {"act_type_xmlid": "buddy_ia.activity_type_digest", "user_id": manager.id}),
                 ("write", [{"partial": True}], {}), ("unlink", [], {}), ("copy", [], {}))
        for name, args, kwargs in calls:
            with self.subTest(method=name), self.assertRaises(AccessError):
                call_kw(self.env["buddy.digest"].with_user(manager), name, [[digest.id]] + list(args), kwargs)
        self.assertEqual(record.message_ids.ids, [], "nothing was posted")
        # `message_subscribe` is open to whoever reads (standard Odoo) but nobody follows a summary (`_message_subscribe` does nothing);
        # `message_notify` too is standard Odoo for ANY record an internal user can read (a partner, a ticket): not a Buddy right.
        call_kw(self.env["buddy.digest"].with_user(manager), "message_subscribe", [[digest.id]], {"partner_ids": [manager.partner_id.id]})
        self.assertFalse(digest.message_follower_ids, "no follower was created")

    def test_the_settings_values_are_private_too(self):
        with self.assertRaises(AccessError):
            rpc_call(self.env["res.config.settings"].with_user(self.employee), "get_values", [])
        # ...and still work from Python, which is what the settings screen uses.
        self.assertIsInstance(self.env["res.config.settings"].get_values(), dict)

    def test_the_buttons_that_are_public_check_the_right_of_the_person(self):
        Settings = self.env["res.config.settings"].with_user(self.employee).browse()
        for name in ("action_buddy_test_connection", "action_buddy_delete_all_conversations", "action_buddy_open_sources",
                     "action_buddy_open_contacts", "action_buddy_open_unanswered", "action_buddy_delete_insights",
                     "action_buddy_clear_api_key", "action_buddy_clear_drive_key", "action_buddy_open_digests", "action_buddy_digest_now", "set_values"):
            with self.subTest(button=name), self.assertRaises(AccessError):
                getattr(Settings, name)()


@tagged("post_install", "-at_install")
class TestPlanAndReservations(EngineCase):
    def setUp(self):
        super().setUp()
        self.P.set_param("buddy_ia.api_key", API_KEY)
        self.user = new_user(self.env, "plan")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        for patcher in (patch.object(self.cls, "_authorization_hash", return_value="authorization-A"),
                        patch.object(self.cls, "_retrieve", return_value=[CHUNK])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self):
        return self.env["buddy.session"].sudo().search([("user_id", "=", self.user.id)])

    def test_the_plan_never_contains_the_api_key_or_the_settings(self):
        plan = self.eng.stream_prepare("Pregunta")
        self.assertNotIn(API_KEY, json.dumps(plan, default=str))
        self.assertNotIn("settings", plan)
        self.assertNotIn("api_key", json.dumps(plan, default=str))

    def test_the_stream_reads_the_key_itself_when_it_starts(self):
        seen = []

        def fake_stream(provider, model, messages, timeout, settings=None, usage=None, **kwargs):
            seen.append(settings)
            yield json.dumps(VALID)

        plan = self.eng.stream_prepare("Pregunta")
        with patch.object(self.env.registry["buddy.provider"], "_chat_stream", fake_stream):
            events = list(self.eng.stream_events(plan))
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(seen[0]["api_key"], API_KEY, "the key is read at the start of the stream, never carried in the plan")

    def test_a_stream_that_never_started_frees_its_reservation(self):
        plan = self.eng.stream_prepare("Pregunta")
        self.assertTrue(self.session().inflight_id, "reserved while the plan waits")
        self.eng._release_unstarted(plan)
        self.assertFalse(self.session().inflight_id)
        usage = self.env["buddy.usage"].sudo().search([("user_id", "=", self.user.id)])
        self.assertEqual((usage.state, usage.error_code), ("failed", "client_disconnected"))
        insight = self.env["buddy.insight"].sudo().search([], order="id desc", limit=1)
        self.assertEqual(insight.error, "client_disconnected")
        self.eng._release_unstarted({"cached": {"answer": "x"}})  # a cached answer reserved nothing
        self.eng._release_unstarted(None)

    def test_the_person_can_ask_again_right_after_an_abandoned_stream(self):
        plan = self.eng.stream_prepare("Pregunta uno")
        with self.assertRaises(BuddyBusy) as caught:
            self.eng.stream_prepare("Pregunta dos")
        self.assertEqual(caught.exception.code, "busy")
        self.eng._release_unstarted(plan)
        with patch.object(self.cls, "_ask_model", return_value=dict(VALID)):
            self.assertEqual(self.eng.chat("Pregunta tres")["answer"], VALID["answer"])

    def test_the_lease_is_a_little_over_one_provider_call(self):
        self.assertEqual(LEASE_SECONDS, 45)
        self.eng.stream_prepare("Pregunta")
        left = (self.session().inflight_until - fields.Datetime.now()).total_seconds()
        self.assertTrue(40 < left <= LEASE_SECONDS + 1, left)


@tagged("post_install", "-at_install")
class TestStatisticsNeverNameTheAsker(EngineCase):
    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "ghost")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env["buddy.insight"].sudo().search([]).unlink()
        self.source = new_source(self.env, "Insight", [new_group(self.env, "InsG")])
        self.document = new_document(self.env, self.source, "Secreto de la carpeta", "texto")

    def chat(self):
        with patch.object(self.cls, "_retrieve", return_value=[dict(CHUNK, doc_id=self.document.id)]), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID, needs_human=True, ticket={"title": "t", "description": "d"})):
            return self.eng.chat("¿Una pregunta sin respuesta?")

    def test_the_row_is_created_by_the_superuser_not_by_the_employee(self):
        self.chat()
        (row,) = self.env["buddy.insight"].sudo().search([])
        self.assertEqual((row.create_uid.id, row.write_uid.id), (SUPERUSER_ID, SUPERUSER_ID))
        self.env.cr.execute("SELECT create_uid, write_uid FROM buddy_insight WHERE id = %s", (row.id,))
        self.assertEqual(self.env.cr.fetchone(), (SUPERUSER_ID, SUPERUSER_ID), "also in the database, not only on the record")
        self.assertNotEqual(row.create_uid, self.user)

    def test_calling_record_directly_as_an_employee_also_leaves_the_superuser(self):
        row = self.env["buddy.insight"].with_user(self.user)._record({"needs_human": True, "question": "q"})
        self.assertEqual((row.create_uid.id, row.write_uid.id), (SUPERUSER_ID, SUPERUSER_ID))

    def test_a_manager_only_sees_titles_of_documents_they_may_read(self):
        self.chat_using_document()
        manager = new_test_user(self.env, login="buddy_ins_mgr_t", groups="base.group_user,buddy_ia.group_buddy_manager")
        admin = new_test_user(self.env, login="buddy_ins_adm_t", groups="base.group_user,base.group_system")
        (row,) = self.env["buddy.insight"].sudo().search([])
        self.assertEqual(row.with_user(admin).document_titles, "Secreto de la carpeta")
        self.assertFalse(row.with_user(manager).document_titles, "the manager is not in the folder's groups: no title")
        self.assertNotIn("Secreto", json.dumps(row.with_user(manager).read(["document_titles"]), default=str))
        # the folder's group is given to the manager: now the title shows
        manager.write({"groups_id": [(4, self.source.group_ids.id)]})
        row.invalidate_recordset()
        self.assertEqual(row.with_user(manager).document_titles, "Secreto de la carpeta")

    def chat_using_document(self):
        with patch.object(self.cls, "_retrieve", return_value=[dict(CHUNK, doc_id=self.document.id)]), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID, sources_used=[1])):
            self.eng.chat("¿Cómo hago eso?")

    def test_searching_by_document_title_respects_the_viewers_rights(self):
        self.chat_using_document()
        manager = new_test_user(self.env, login="buddy_ins_mgr_s", groups="base.group_user,buddy_ia.group_buddy_manager")
        admin = new_test_user(self.env, login="buddy_ins_adm_s", groups="base.group_user,base.group_system")
        Insight = self.env["buddy.insight"]
        self.assertEqual(len(Insight.with_user(admin).search([("document_titles", "ilike", "Secreto")])), 1)
        self.assertFalse(Insight.with_user(manager).search([("document_titles", "ilike", "Secreto")]))


@tagged("post_install", "-at_install")
class TestManagerGroupIsNotImplied(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        self.Settings = self.env["res.config.settings"]
        self.manager_group = self.env.ref("buddy_ia.group_buddy_manager")

    def test_administrators_do_not_get_the_group_by_implication_and_keep_their_access(self):
        self.assertNotIn(self.manager_group, self.env.ref("base.group_system").implied_ids)
        admin = new_test_user(self.env, login="buddy_gm_adm", groups="base.group_user,base.group_system")
        self.assertFalse(admin.has_group("buddy_ia.group_buddy_manager"))
        # ...and still see the statistics, the menus and every ticket, by their own explicit access
        Insight = self.env["buddy.insight"]
        Insight._record({"needs_human": True, "question": "q"})
        self.assertTrue(Insight.with_user(admin).search([]))
        for xmlid in ("buddy_menu_insights", "buddy_menu_unanswered", "buddy_menu_usage"):
            self.assertEqual(set(self.env.ref("buddy_ia." + xmlid).groups_id.ids),
                             {self.manager_group.id, self.env.ref("base.group_system").id}, xmlid)
        ticket = self.env["buddy.ticket"].with_user(admin).create({"name": "Del admin"})
        self.assertTrue(self.env["buddy.ticket"].with_user(admin).search([("id", "=", ticket.id)]))
        self.assertTrue(ticket.with_user(admin).is_manager)

    def test_the_form_knows_who_is_a_manager(self):
        ana = new_test_user(self.env, login="buddy_gm_ana", groups="base.group_user")
        boss = new_test_user(self.env, login="buddy_gm_boss", groups="base.group_user,buddy_ia.group_buddy_manager")
        ticket = self.env["buddy.ticket"].with_user(ana).create({"name": "Mío"})
        self.assertFalse(ticket.with_user(ana).is_manager)
        self.assertTrue(ticket.with_user(boss).is_manager)
        arch = self.env["buddy.ticket"].get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn("not is_manager", arch)

    # --- the permission Buddy gives to the responsible person is the one it takes back ---------------------------
    def save(self, **values):
        self.Settings.create(values).execute()

    def test_changing_the_responsible_person_takes_the_permission_back_from_the_previous_one(self):
        first = new_test_user(self.env, login="buddy_gm_first", groups="base.group_user")
        second = new_test_user(self.env, login="buddy_gm_second", groups="base.group_user")
        self.save(buddy_ticket_user_id=first.id)
        self.assertIn(self.manager_group, first.groups_id)
        self.assertEqual(self.params.get_param("buddy_ia.ticket_user_granted"), str(first.id))
        self.save(buddy_ticket_user_id=second.id)
        self.assertNotIn(self.manager_group, first.groups_id, "Buddy gave it, Buddy takes it back")
        self.assertIn(self.manager_group, second.groups_id)
        self.assertEqual(self.params.get_param("buddy_ia.ticket_user_granted"), str(second.id))
        self.save(buddy_ticket_user_id=False)
        self.assertNotIn(self.manager_group, second.groups_id)
        self.assertFalse(self.params.get_param("buddy_ia.ticket_user_granted"))

    def test_a_permission_an_administrator_gave_by_hand_is_never_taken_away(self):
        by_hand = new_test_user(self.env, login="buddy_gm_hand", groups="base.group_user,buddy_ia.group_buddy_manager")
        other = new_test_user(self.env, login="buddy_gm_other", groups="base.group_user")
        self.save(buddy_ticket_user_id=by_hand.id)
        self.assertFalse(self.params.get_param("buddy_ia.ticket_user_granted"), "they already had it: nothing was granted")
        self.save(buddy_ticket_user_id=other.id)
        self.assertIn(self.manager_group, by_hand.groups_id)
        self.assertIn(self.manager_group, other.groups_id)

    def test_choosing_an_administrator_grants_nothing(self):
        admin = new_test_user(self.env, login="buddy_gm_adm3", groups="base.group_user,base.group_system")
        self.save(buddy_ticket_user_id=admin.id)
        self.assertFalse(self.params.get_param("buddy_ia.ticket_user_granted"))
        self.assertNotIn(self.manager_group, admin.groups_id)

    def test_the_grant_and_the_revocation_are_logged_with_who_did_it(self):
        person = new_test_user(self.env, login="buddy_gm_log", groups="base.group_user")
        with self.assertLogs("odoo.addons.buddy_ia.models.res_config_settings", "WARNING") as logs:
            self.save(buddy_ticket_user_id=person.id)
            self.save(buddy_ticket_user_id=False)
        text = "\n".join(logs.output)
        self.assertIn("le dio el permiso", text)
        self.assertIn("le quitó el permiso", text)
        self.assertIn(str(person.id), text)

    def test_saving_the_settings_checks_the_right_before_anything_else(self):
        employee = new_test_user(self.env, login="buddy_gm_emp", groups="base.group_user")
        settings = self.Settings.create({})
        with self.assertRaises(AccessError):
            settings.with_user(employee).set_values()
        self.assertFalse(self.params.get_param("buddy_ia.ticket_user_granted"))

    # --- a responsible person that cannot be notified never blocks the ticket --------------------------------------
    def test_a_failing_notification_does_not_stop_the_ticket_from_being_created(self):
        boss = new_test_user(self.env, login="buddy_gm_nf", groups="base.group_user")
        ana = new_test_user(self.env, login="buddy_gm_nfa", groups="base.group_user")
        self.params.set_param("buddy_ia.ticket_user_id", str(boss.id))
        mixin = self.env.registry["mail.activity.mixin"]
        with patch.object(mixin, "activity_schedule", side_effect=AccessError("el responsable no puede ver el ticket")), \
                self.assertLogs("odoo.addons.buddy_ia.models.buddy_ticket", "WARNING") as logs:
            ticket = self.env["buddy.ticket"].with_user(ana).create({"name": "Igual se crea", "description": "d"})
        self.assertTrue(ticket.exists())
        self.assertEqual(ticket.sudo().assigned_user_id, boss)
        self.assertIn("no pude avisarle al responsable", "\n".join(logs.output))


@tagged("post_install", "-at_install")
class TestArchivedFoldersAndHostileDriveText(TransactionCase):
    def setUp(self):
        super().setUp()
        self.group = new_group(self.env, "Arch")
        self.employee = new_user(self.env, "arch", extra_groups=[self.group])
        self.source = new_source(self.env, "Archivable", [self.group])
        self.document = new_document(self.env, self.source, "Doc", "contenido legible")
        self.chunk = self.document.chunk_ids

    def test_an_archived_folder_is_not_readable_by_the_api_only_by_administrators(self):
        for record in (self.source, self.document, self.chunk):
            self.assertTrue(record.with_user(self.employee).read(["id"]), record._name)
        self.source.active = False
        for record in (self.source, self.document, self.chunk):
            with self.subTest(model=record._name), self.assertRaises(AccessError):
                record.with_user(self.employee).read(["id"])
            self.assertFalse(self.env[record._name].with_user(self.employee).with_context(active_test=False).search([("id", "=", record.id)]))
        admin = new_test_user(self.env, login="buddy_arch_adm", groups="base.group_user,base.group_system")
        self.assertTrue(self.document.with_user(admin).read(["name"]), "administrators still see everything")
        self.source.active = True
        self.assertTrue(self.document.with_user(self.employee).read(["name"]), "restoring the folder gives the access back")

    def test_nul_and_control_characters_from_drive_never_stop_the_synchronization(self):
        text = "Pasos\u0000 para\u0001 pedir vacaciones.\nAvisá a tu líder.\u007f"
        client = FakeClient(files=[drive_file("fileNUL", "Guía\u0000 con\u0001 nulos", path="RR\u0000HH"),
                                   drive_file("fileOK", "Normal")], texts={"fileNUL": text, "fileOK": "Texto normal."})
        self.source.sudo()._sync(client=client)
        self.assertEqual(self.source.sudo().state, "ok", self.source.sudo().last_error)
        document = self.env["buddy.document"].search([("source_id", "=", self.source.id), ("drive_id", "=", "fileNUL")])
        self.assertEqual((document.name, document.path, document.state), ("Guía con nulos", "RRHH", "ready"))
        content = "".join(document.chunk_ids.mapped("content"))
        self.assertIn("Pasos para pedir vacaciones", content)
        for bad in ("\u0000", "\u0001", "\u007f"):
            self.assertNotIn(bad, content)
        self.assertEqual(self.env["buddy.document"].search_count([("source_id", "=", self.source.id), ("state", "=", "ready")]), 2)

    def test_a_file_error_message_with_control_characters_is_stored_clean(self):
        from ..models.drive_client import DriveError
        client = FakeClient(files=[drive_file("fileBAD", "Roto")], errors={"fileBAD": DriveError("bad_file", "No se pudo\u0000 leer\u0001.")})
        self.source.sudo()._sync(client=client)
        document = self.env["buddy.document"].search([("source_id", "=", self.source.id), ("drive_id", "=", "fileBAD")])
        self.assertEqual((document.state, document.sudo().error), ("error", "No se pudo leer."))
