"""Administration and first use: tickets with chatter and a responsible person, the Drive key as a file, the contacts step
of the checklist, the persistent result of «Probar conexión», the service-account e-mail and the sync refresh."""
import base64
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.buddy_contact import is_placeholder_email
from ..models.buddy_source import DRIVE_KEY_PARAM, service_account_email
from .common import local_test_transaction, new_group, new_source
from .test_settings import EMAIL, KEY_FRAGMENT, service_account

PARAMS = "ir.config_parameter"


@tagged("post_install", "-at_install")
class TestTicketResponsibleAndChatter(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        self.ana = new_test_user(self.env, login="buddy_tk_ana", groups="base.group_user")
        self.leo = new_test_user(self.env, login="buddy_tk_leo", groups="base.group_user")
        self.boss = new_test_user(self.env, login="buddy_tk_boss", groups="base.group_user,buddy_ia.group_buddy_manager")
        self.Ticket = self.env["buddy.ticket"]
        self.params.set_param("buddy_ia.ticket_user_id", str(self.boss.id))

    def new_ticket(self, user=None, **values):
        return self.Ticket.with_user(user or self.ana).create(dict({"name": "Impresora", "description": "No imprime"}, **values))

    # --- a ticket is a conversation with a person in charge ------------------------------------------------
    def test_a_ticket_has_a_chatter_and_activities(self):
        for name in ("message_ids", "message_follower_ids", "activity_ids"):
            self.assertIn(name, self.Ticket._fields)
        views = self.Ticket.with_user(self.ana).get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn("chatter", views.lower())

    def test_the_responsible_person_gets_a_todo_and_a_seat_in_the_chatter(self):
        ticket = self.new_ticket()
        self.assertEqual(ticket.sudo().assigned_user_id, self.boss)
        (activity,) = ticket.sudo().activity_ids
        self.assertEqual(activity.user_id, self.boss)
        self.assertEqual(activity.activity_type_id, self.env.ref("mail.mail_activity_data_todo"))
        self.assertIn("Impresora", activity.summary)
        self.assertIn("No imprime", activity.note)
        self.assertIn(self.boss.partner_id, ticket.sudo().message_partner_ids)
        self.assertIn(self.ana.partner_id, ticket.sudo().message_partner_ids, "the person who asked follows their own ticket")

    def test_without_a_responsible_nobody_is_assigned_and_nothing_is_scheduled(self):
        self.params.set_param("buddy_ia.ticket_user_id", False)
        ticket = self.new_ticket()
        self.assertFalse(ticket.sudo().assigned_user_id)
        self.assertFalse(ticket.sudo().activity_ids)

    def test_a_responsible_that_cannot_work_is_ignored(self):
        portal = new_test_user(self.env, login="buddy_tk_portal", groups="base.group_portal")
        archived = new_test_user(self.env, login="buddy_tk_gone", groups="base.group_user")
        archived.active = False
        for bad in (str(portal.id), str(archived.id), "999999999", "no-es-un-id", "0"):
            self.params.set_param("buddy_ia.ticket_user_id", bad)
            ticket = self.new_ticket(name="Con %s" % bad)
            self.assertFalse(ticket.sudo().assigned_user_id, bad)

    def test_the_person_who_asks_never_chooses_who_answers(self):
        ticket = self.new_ticket(assigned_user_id=self.leo.id)
        self.assertEqual(ticket.sudo().assigned_user_id, self.boss, "the setting decides, not the requester")
        admin = new_test_user(self.env, login="buddy_tk_admin", groups="base.group_user,base.group_system")
        chosen = self.Ticket.with_user(admin).create({"name": "Asignado a mano", "assigned_user_id": self.leo.id})
        self.assertEqual(chosen.assigned_user_id, self.leo, "an administrator may")

    def test_a_retry_returns_the_same_ticket_and_schedules_nothing_more(self):
        key = "0f4c6a3e-8d0b-4a58-9d57-3c2f5a1b9e77"
        first = self.new_ticket(request_key=key)
        second = self.new_ticket(request_key=key)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(first.sudo().activity_ids), 1)

    def test_the_engine_creates_tickets_with_the_same_effects(self):
        engine = self.env["buddy.engine"].with_user(self.ana)
        with patch.object(self.env.registry["buddy.engine"], "_fresh_engine", local_test_transaction), \
                patch.object(self.env.registry["buddy.engine"], "_authorization_hash", return_value="x"):
            result = engine._create_ticket("Desde el chat", "Detalle", None, None)
        ticket = self.Ticket.sudo().browse(result["id"])
        self.assertEqual((ticket.assigned_user_id, ticket.user_id), (self.boss, self.ana))
        self.assertEqual(len(ticket.activity_ids), 1)

    # --- who sees and who can change what --------------------------------------------------------------------
    def test_the_manager_sees_every_ticket_and_handles_it_while_the_requester_sees_only_their_own(self):
        mine, theirs = self.new_ticket(self.ana, name="De Ana"), self.new_ticket(self.leo, name="De Leo")
        self.assertEqual(self.Ticket.with_user(self.ana).search([]), mine)
        self.assertEqual(self.Ticket.with_user(self.boss).search([]), mine | theirs)
        theirs.with_user(self.boss).write({"state": "progress", "assigned_user_id": self.boss.id})
        self.assertEqual(theirs.state, "progress")
        with self.assertRaises(AccessError):
            mine.with_user(self.ana).write({"state": "done"})
        with self.assertRaises(AccessError):
            mine.with_user(self.boss).unlink()
        with self.assertRaises(AccessError):
            theirs.with_user(self.ana).read(["name"])

    def test_the_requester_can_answer_in_the_chatter_of_their_own_ticket_only(self):
        mine, theirs = self.new_ticket(self.ana), self.new_ticket(self.leo, name="De Leo")
        message = mine.with_user(self.ana).message_post(body="Gracias, ya probé reiniciar", message_type="comment")
        self.assertIn(message, mine.sudo().message_ids)
        with self.assertRaises(AccessError):
            theirs.with_user(self.ana).message_post(body="Intruso", message_type="comment")
        reply = mine.with_user(self.boss).message_post(body="Pasá por IT", message_type="comment")
        self.assertIn(reply, mine.with_user(self.ana).message_ids, "the requester reads the answer")

    def test_status_and_assignment_changes_are_tracked_in_the_chatter(self):
        # The tracking messages themselves are written at commit time, which a test transaction never reaches:
        # what is checked is that Odoo was told to track both fields.
        self.assertTrue(self.Ticket._fields["state"].tracking)
        self.assertTrue(self.Ticket._fields["assigned_user_id"].tracking)
        ticket = self.new_ticket()
        ticket.with_user(self.boss).write({"state": "done", "assigned_user_id": self.boss.id})
        self.assertEqual((ticket.state, ticket.assigned_user_id), ("done", self.boss))

    def test_the_menu_views_and_filters_support_the_new_workflow(self):
        views = self.Ticket.get_views([(False, "search"), (False, "list")])["views"]
        for name in ("assigned_to_me", "unassigned", "group_assigned"):
            self.assertIn(name, views["search"]["arch"])
        self.assertIn("assigned_user_id", views["list"]["arch"])
        form = self.Ticket.with_user(self.boss).get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn("clickable", form, "the manager moves the ticket along the status bar")
        self.assertIn("not is_manager", form, "...and whoever is not a manager sees every field read-only")


@tagged("post_install", "-at_install")
class TestSettingsForFirstUse(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        self.Settings = self.env["res.config.settings"]

    def save(self, **values):
        settings = self.Settings.create(values)
        settings.execute()
        return settings

    @staticmethod
    def upload(text, name="clave.json"):
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        return {"buddy_drive_key_file": base64.b64encode(data), "buddy_drive_key_filename": name}

    # --- the Drive key as a file --------------------------------------------------------------------------
    def test_a_key_file_is_stored_like_the_pasted_text_and_never_comes_back(self):
        self.params.set_param(DRIVE_KEY_PARAM, False)
        settings = self.save(**self.upload(service_account()))
        stored = json.loads(self.params.get_param(DRIVE_KEY_PARAM))
        self.assertEqual(stored["client_email"], EMAIL)
        self.assertIn("private_key", stored)
        self.assertNotIn("client_x509_cert_url", stored, "only what Buddy needs is kept")
        self.assertFalse(settings.buddy_drive_key_file, "the temporary record forgets the file")
        self.assertFalse(settings.buddy_drive_key_filename)
        fresh = self.Settings.create({})
        self.assertEqual((fresh.buddy_drive_email, fresh.buddy_drive_loaded), (EMAIL, True))
        for dump in (json.dumps(fresh.read(), default=str), json.dumps(self.Settings.get_values(), default=str),
                     json.dumps(self.Settings.default_get(list(self.Settings._fields)), default=str)):
            self.assertNotIn(KEY_FRAGMENT, dump)
            self.assertNotIn("PRIVATE KEY", dump)
        self.assertEqual(self.Settings.create({}).buddy_drive_key_file, False)

    def test_the_file_is_not_an_attachment(self):
        self.assertFalse(self.Settings._fields["buddy_drive_key_file"].attachment)
        before = self.env["ir.attachment"].sudo().search_count([("res_model", "=", "res.config.settings")])
        self.save(**self.upload(service_account()))
        self.assertEqual(self.env["ir.attachment"].sudo().search_count([("res_model", "=", "res.config.settings")]), before)

    def test_a_file_with_a_byte_order_mark_works(self):
        self.save(**self.upload(b"\xef\xbb\xbf" + service_account().encode("utf-8")))
        self.assertEqual(json.loads(self.params.get_param(DRIVE_KEY_PARAM))["client_email"], EMAIL)

    def test_a_wrong_file_is_rejected_without_echoing_it_and_keeps_the_stored_key(self):
        self.save(buddy_drive_key_input=service_account())
        stored = self.params.get_param(DRIVE_KEY_PARAM)
        cases = {"binario": b"\x00\x01\x02\xff\xfe", "otro json": b'{"hola": "mundo"}',
                 "no es una cuenta de servicio": service_account(type="authorized_user").encode(),
                 "texto": b"esto no es una clave",
                 "enorme": b'{"type": "service_account", "pad": "' + b"x" * (70 * 1024) + b'"}'}
        for label, content in cases.items():
            with self.subTest(case=label), self.assertRaises(ValidationError) as caught:
                self.save(**self.upload(content))
            self.assertNotIn(KEY_FRAGMENT, str(caught.exception), label)
            self.assertNotIn("PRIVATE KEY", str(caught.exception), label)
        self.assertEqual(self.params.get_param(DRIVE_KEY_PARAM), stored)

    def test_file_and_pasted_text_together_are_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self.save(buddy_drive_key_input=service_account(), **self.upload(service_account()))
        self.assertIn("una sola forma", str(caught.exception))

    def test_saving_without_a_file_keeps_the_key(self):
        self.save(**self.upload(service_account()))
        stored = self.params.get_param(DRIVE_KEY_PARAM)
        self.save(buddy_chat_model="otro")
        self.assertEqual(self.params.get_param(DRIVE_KEY_PARAM), stored)

    def test_the_settings_form_offers_the_file_and_the_copy_button_and_a_short_guide(self):
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        for expected in ("buddy_drive_key_file", "buddy_drive_key_filename", "CopyClipboardChar", "console.cloud.google.com", "Google Drive API",
                         "buddy_ticket_user_id", "buddy_dialect", "buddy_failures_text", "buddy_insight_questions", "buddy_insight_days",
                         "buddy_price_in", "buddy_price_out", "buddy_max_daily_total", "buddy_max_daily_cost",
                         "action_buddy_open_contacts", "action_buddy_open_unanswered", "action_buddy_delete_insights",
                         "buddy_last_test_detail", "buddy_ready_contacts", "buddy_usage_text"):
            self.assertIn(expected, arch)

    # --- contacts step -------------------------------------------------------------------------------------
    def test_placeholder_addresses(self):
        for sample in ("ayuda@ejemplo.test", "it@ejemplo.com", "x@example.com", "x@sub.example.org", "x@algo.test", "x@mail.invalid",
                       "x@localhost", "sin-arroba", "", None, " ", "@dominio.com", "x@", "x@sinpunto", "X@EJEMPLO.COM", "x@empresa.example"):
            self.assertTrue(is_placeholder_email(sample), repr(sample))
        for real in ("ayuda@miempresa.com", "it@miempresa.com.ar", "rrhh@grupo-ejemplar.com", "a.b+c@sub.dominio.io", " ana@acme.co "):
            self.assertFalse(is_placeholder_email(real), repr(real))

    def test_the_contacts_step_wants_real_addresses(self):
        Contact = self.env["buddy.contact"]
        Contact.search([]).unlink()
        settings = self.Settings.create({})
        self.assertFalse(settings.buddy_ready_contacts, "no contacts: nobody to send people to")
        self.assertIn("No hay contactos", settings.buddy_contacts_text)
        sample = Contact.create({"code": "general", "label": "Mesa de ayuda", "name": "Mesa", "email": "ayuda@ejemplo.test"})
        real = Contact.create({"code": "it", "label": "Mesa de IT", "name": "IT", "email": "it@miempresa.com"})
        settings = self.Settings.create({})
        self.assertFalse(settings.buddy_ready_contacts)
        self.assertIn("Mesa de ayuda", settings.buddy_contacts_text)
        self.assertNotIn("Mesa de IT", settings.buddy_contacts_text)
        sample.email = "ayuda@miempresa.com"
        settings = self.Settings.create({})
        self.assertTrue(settings.buddy_ready_contacts)
        real.unlink()
        self.assertTrue(self.Settings.create({}).buddy_ready_contacts, "deleting the contacts you do not use is a valid way out")

    def test_the_sample_contacts_installed_with_the_module_do_not_pass_the_step(self):
        for xmlid in ("general", "it", "rrhh", "ventas", "admin"):
            contact = self.env.ref("buddy_ia.buddy_contact_" + xmlid)
            self.assertTrue(is_placeholder_email(contact.email), contact.email)

    def test_the_contacts_button_is_for_administrators(self):
        action = self.Settings.create({}).action_buddy_open_contacts()
        self.assertEqual(action["res_model"], "buddy.contact")
        employee = new_test_user(self.env, login="buddy_fu_emp", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.Settings.with_user(employee).browse().action_buddy_open_contacts()

    # --- the test result stays on the screen -------------------------------------------------------------------
    def test_the_lines_of_the_last_test_are_kept_for_the_checklist(self):
        cls = self.env.registry["buddy.engine"]
        lines = ["Todavía no elegiste un modelo. Los que tu cuenta puede usar: a, b.", "Falta la clave de Google Drive."]
        with patch.object(cls, "_self_test", return_value=(False, lines)):
            self.Settings.create({}).action_buddy_test_connection()
        self.assertEqual(self.params.get_param("buddy_ia.last_test_detail"), "\n".join(lines))
        shown = self.Settings.create({}).buddy_last_test_detail
        self.assertIn("Todavía no elegiste un modelo", shown)
        self.assertIn("Google Drive", shown)
        with patch.object(cls, "_self_test", return_value=(True, ["Proveedor de IA OK"])):
            self.Settings.create({}).action_buddy_test_connection()
        self.assertEqual(self.Settings.create({}).buddy_last_test_detail, "Proveedor de IA OK", "the newest result replaces the old one")

    def test_a_throttled_test_does_not_overwrite_the_last_real_result(self):
        cls = self.env.registry["buddy.engine"]
        self.params.set_param("buddy_ia.last_test_detail", "resultado anterior")
        from ..models.buddy_engine import BuddyBusy
        with patch.object(cls, "_self_test", side_effect=BuddyBusy("rate_limited")):
            self.Settings.create({}).action_buddy_test_connection()
        self.assertEqual(self.params.get_param("buddy_ia.last_test_detail"), "resultado anterior")

    def test_the_detail_is_bounded(self):
        cls = self.env.registry["buddy.engine"]
        with patch.object(cls, "_self_test", return_value=(False, ["x" * 500] * 40)):
            self.Settings.create({}).action_buddy_test_connection()
        self.assertLessEqual(len(self.params.get_param("buddy_ia.last_test_detail")), 3000)

    # --- responsible person -------------------------------------------------------------------------------
    def test_saving_a_responsible_gives_them_the_manager_permission_and_nothing_else(self):
        person = new_test_user(self.env, login="buddy_fu_resp", groups="base.group_user")
        manager = self.env.ref("buddy_ia.group_buddy_manager")
        self.assertNotIn(manager, person.groups_id)
        self.save(buddy_ticket_user_id=person.id)
        self.assertEqual(self.params.get_param("buddy_ia.ticket_user_id"), str(person.id))
        self.assertIn(manager, person.groups_id)
        self.assertNotIn(self.env.ref("base.group_system"), person.groups_id)
        self.assertEqual(self.Settings.create({}).buddy_ticket_user_id, person)
        self.save(buddy_ticket_user_id=False)
        self.assertFalse(self.params.get_param("buddy_ia.ticket_user_id"))

    # --- statistics, prices and limits -----------------------------------------------------------------------
    def test_defaults_of_the_new_settings(self):
        defaults = self.Settings.default_get(["buddy_insight_days", "buddy_insight_questions", "buddy_price_in", "buddy_price_out",
                                              "buddy_max_daily_total", "buddy_max_daily_cost", "buddy_rate_per_day"])
        self.assertEqual(defaults["buddy_insight_days"], 90)
        self.assertFalse(defaults["buddy_insight_questions"], "questions are NOT stored by default")
        self.assertEqual((defaults["buddy_price_in"], defaults["buddy_price_out"], defaults["buddy_max_daily_cost"]), (0, 0, 0))
        self.assertEqual((defaults["buddy_max_daily_total"], defaults["buddy_rate_per_day"]), (1000, 40))

    def test_new_settings_are_stored_as_parameters(self):
        self.save(buddy_insight_questions=True, buddy_insight_days=30, buddy_price_in=3.0, buddy_price_out=15.0,
                  buddy_max_daily_total=500, buddy_max_daily_cost=12.5)
        get = self.params.get_param
        self.assertEqual((get("buddy_ia.insight_questions"), get("buddy_ia.insight_days"), get("buddy_ia.max_daily_total")),
                         ("True", "30", "500"))
        self.assertEqual((float(get("buddy_ia.price_in_per_m")), float(get("buddy_ia.price_out_per_m")),
                          float(get("buddy_ia.max_daily_cost"))), (3.0, 15.0, 12.5))
        self.save(buddy_insight_questions=False)
        self.assertFalse(get("buddy_ia.insight_questions"), "unticking forgets the setting: questions stop being stored")

    def test_the_new_settings_are_validated(self):
        for field, value in (("buddy_insight_days", 0), ("buddy_insight_days", 3651), ("buddy_max_daily_total", 0),
                             ("buddy_max_daily_total", 1_000_001), ("buddy_price_in", -1.0), ("buddy_price_out", 1_000_001.0),
                             ("buddy_max_daily_cost", -0.5)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.Settings.create({field: value})
        with self.assertRaises(ValidationError) as caught:
            self.Settings.create({"buddy_max_daily_cost": 10.0})
        self.assertIn("precios", str(caught.exception), "a cost cap without prices could never trigger")
        self.Settings.create({"buddy_max_daily_cost": 10.0, "buddy_price_in": 1.0})

    def test_usage_text_shows_cost_only_when_prices_are_set(self):
        Insight = self.env["buddy.insight"].sudo()
        Insight.search([]).unlink()
        for key in ("price_in_per_m", "price_out_per_m"):
            self.params.set_param("buddy_ia." + key, False)
        self.assertIn("Todavía no", self.Settings.create({}).buddy_usage_text)
        Insight._record({"tokens_in": 2000, "tokens_out": 300, "needs_human": False})
        text = self.Settings.create({}).buddy_usage_text
        self.assertIn("Consultas: 1", text)
        self.assertIn("2000", text)
        self.assertNotIn("costo", text)
        self.params.set_param("buddy_ia.price_in_per_m", "10")
        self.assertIn("costo estimado USD", self.Settings.create({}).buddy_usage_text)

    # --- the folder form ---------------------------------------------------------------------------------------
    def test_the_service_account_email_is_exposed_without_the_private_key(self):
        self.params.set_param(DRIVE_KEY_PARAM, False)
        source = new_source(self.env, "Con correo", [new_group(self.env, "Eml")])
        self.assertFalse(source.service_account_email)
        self.save(buddy_drive_key_input=service_account())
        source.invalidate_recordset()
        self.assertEqual(source.service_account_email, EMAIL)
        self.assertNotIn(KEY_FRAGMENT, json.dumps(source.read(["service_account_email"]), default=str))
        employee = new_test_user(self.env, login="buddy_fu_emp2", groups="base.group_user")
        with self.assertRaises(AccessError):
            source.with_user(employee).read(["service_account_email"])

    def test_the_email_helper_survives_garbage(self):
        for raw in (None, False, "", "{no json", "[]", "7", '{"client_email": 5}', '{"x": 1}'):
            self.assertEqual(service_account_email(raw), "", repr(raw))
        self.assertEqual(service_account_email(service_account()), EMAIL)

    def test_the_folder_form_has_the_email_the_copy_button_and_the_auto_refresh(self):
        form = self.env["buddy.source"].get_views([(False, "form")])["views"]["form"]["arch"]
        for expected in ("service_account_email", "CopyClipboardChar", "buddy_sync_refresh", "action_refresh", "Lector"):
            self.assertIn(expected, form)
        self.assertNotIn("docs/", form, "the guide is on the screen, not in a file of the repository")
        self.assertNotIn("<details", form, "...and it is not hidden")

    def test_refresh_is_for_administrators_and_reloads_the_screen(self):
        source = new_source(self.env, "Refresco", [new_group(self.env, "Ref")])
        self.assertEqual(source.action_refresh()["tag"], "soft_reload")
        employee = new_test_user(self.env, login="buddy_fu_emp3", groups="base.group_user")
        with self.assertRaises(AccessError):
            source.with_user(employee).action_refresh()
