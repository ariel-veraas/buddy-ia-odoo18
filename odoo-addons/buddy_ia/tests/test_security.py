"""Who sees which folder, document and fragment; folder validation; the sync cron and the manual sync button."""
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.buddy_source import DRIVE_KEY_PARAM, RETRY_AFTER_ERROR, STUCK_AFTER, SYNC_HOURS_PARAM
from .common import DRIVE_URL, detach_groups, new_admin, new_document, new_group, new_source, new_user

CFG = {"top_k": 4}


@tagged("post_install", "-at_install")
class TestBuddyFolderSecurity(TransactionCase):
    def setUp(self):
        super().setUp()
        self.g_sales = new_group(self.env, "Ventas")
        self.g_hr = new_group(self.env, "RRHH")
        self.ana = new_user(self.env, "ana", extra_groups=[self.g_sales])
        self.leo = new_user(self.env, "leo")
        self.hr = new_user(self.env, "hr", extra_groups=[self.g_hr])
        self.boss = new_admin(self.env)
        self.sales_source = new_source(self.env, "Ventas", [self.g_sales])
        self.hr_source = new_source(self.env, "RRHH", [self.g_hr])
        self.sales_doc = new_document(self.env, self.sales_source, "Cotizar", "Para crear una cotización abrí Ventas y elegí Nuevo.")
        self.hr_doc = new_document(self.env, self.hr_source, "Salarios", "La escala salarial del gerente es confidencial.")

    def visible(self, user, model):
        return self.env[model].with_user(user).search([("id", "in", self._ids(model))])

    def _ids(self, model):
        return self.env[model].sudo().search([]).ids

    def retrieve(self, user, message):
        return self.env["buddy.engine"].with_user(user)._retrieve(message, dict(CFG))

    # --- visibility ------------------------------------------------------------------------
    def test_employee_sees_only_folders_documents_and_chunks_of_own_groups(self):
        self.assertEqual(self.visible(self.ana, "buddy.source"), self.sales_source)
        self.assertEqual(self.visible(self.ana, "buddy.document"), self.sales_doc)
        self.assertEqual(self.visible(self.ana, "buddy.chunk").document_id, self.sales_doc)
        self.assertEqual(self.visible(self.hr, "buddy.source"), self.hr_source)
        self.assertEqual(self.visible(self.hr, "buddy.document"), self.hr_doc)

    def test_employee_of_another_group_sees_nothing_and_gets_no_fragments(self):
        for model in ("buddy.source", "buddy.document", "buddy.chunk"):
            self.assertFalse(self.visible(self.leo, model), model)
        self.assertEqual(self.retrieve(self.leo, "cotización de ventas"), [])
        self.assertEqual(self.retrieve(self.leo, "escala salarial del gerente"), [])
        self.assertEqual(self.retrieve(self.ana, "escala salarial del gerente"), [], "ana is not in RRHH")
        self.assertEqual([hit["title"] for hit in self.retrieve(self.ana, "cotización")], ["Cotizar"])

    def test_direct_reads_of_foreign_records_are_denied(self):
        for record in (self.hr_source, self.hr_doc, self.hr_doc.chunk_ids):
            with self.assertRaises(AccessError):
                record.with_user(self.ana).read(["id"])
        with self.assertRaises(AccessError):
            self.hr_doc.with_user(self.ana).name

    def test_admin_sees_everything_and_gets_every_fragment(self):
        self.assertEqual(self.visible(self.boss, "buddy.source"), self.sales_source | self.hr_source)
        self.assertEqual(self.visible(self.boss, "buddy.document"), self.sales_doc | self.hr_doc)
        self.assertEqual([hit["title"] for hit in self.retrieve(self.boss, "escala salarial")], ["Salarios"])
        self.assertEqual([hit["title"] for hit in self.retrieve(self.boss, "cotización")], ["Cotizar"])

    def test_folder_without_groups_is_invisible_to_every_employee(self):
        orphan = new_source(self.env, "Huérfana", [self.g_sales])
        doc = new_document(self.env, orphan, "Huérfano", "El manual de huérfanos describe los trámites.")
        detach_groups(self.env, orphan)
        self.assertFalse(orphan.group_ids)
        for user in (self.ana, self.leo, self.hr):
            self.assertNotIn(orphan, self.visible(user, "buddy.source"), user.name)
            self.assertNotIn(doc, self.visible(user, "buddy.document"), user.name)
            self.assertEqual(self.retrieve(user, "manual de huérfanos trámites"), [], user.name)
        self.assertEqual(len(self.retrieve(self.boss, "manual de huérfanos trámites")), 1, "only the administrator reaches it")

    def test_internal_user_group_opens_a_folder_to_every_employee(self):
        open_source = new_source(self.env, "Todos", [self.env.ref("base.group_user")])
        new_document(self.env, open_source, "Bienvenida", "Bienvenida al equipo: el wifi se llama oficina.")
        for user in (self.ana, self.leo, self.hr):
            self.assertEqual([hit["title"] for hit in self.retrieve(user, "wifi oficina bienvenida")], ["Bienvenida"])

    def test_archived_folder_is_not_searched(self):
        self.sales_source.active = False
        self.assertEqual(self.retrieve(self.ana, "cotización"), [])
        self.assertEqual(self.retrieve(self.boss, "cotización"), [])

    def test_losing_a_group_removes_access_immediately(self):
        self.assertTrue(self.retrieve(self.ana, "cotización"))
        self.ana.write({"groups_id": [(3, self.g_sales.id)]})
        self.assertEqual(self.retrieve(self.ana, "cotización"), [])
        self.assertFalse(self.visible(self.ana, "buddy.source"))

    # --- field level -------------------------------------------------------------------------
    def test_employee_cannot_read_folder_url_or_error_details(self):
        source = self.sales_source.with_user(self.ana)
        self.assertEqual(source.name, "Ventas")
        self.assertEqual(source.document_count, 1)
        for field in ("folder_url", "folder_id", "last_error", "last_summary", "last_sync", "state"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                source.read([field])
        for field in ("folder_url", "last_error"):
            with self.subTest(attribute=field), self.assertRaises(AccessError):
                getattr(source, field)

    def test_employee_cannot_search_by_protected_fields_either(self):
        for field in ("folder_url", "last_error"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.env["buddy.source"].with_user(self.ana).search([(field, "ilike", "a")])

    def test_admin_reads_everything(self):
        source = self.sales_source.with_user(self.boss)
        self.assertTrue(source.folder_url.startswith(DRIVE_URL))
        self.assertEqual(source.folder_id, self.sales_source.folder_id)
        self.assertEqual(source.state, "never")

    def test_employee_can_read_the_document_link_used_by_the_citations(self):
        document = self.sales_doc.with_user(self.ana)
        self.assertTrue(document.web_view_link.startswith("https://docs.google.com/"))
        self.assertEqual(document.source_id.name, "Ventas")

    def test_employee_cannot_create_write_or_delete_anything(self):
        Source = self.env["buddy.source"].with_user(self.ana)
        with self.assertRaises(AccessError):
            Source.create({"name": "Mía", "folder_url": DRIVE_URL + "XXXXXXXXXXXX", "group_ids": [(6, 0, self.g_sales.ids)]})
        with self.assertRaises(AccessError):
            self.sales_source.with_user(self.ana).write({"name": "Hackeada"})
        with self.assertRaises(AccessError):
            self.sales_source.with_user(self.ana).unlink()
        with self.assertRaises(AccessError):
            self.sales_doc.with_user(self.ana).write({"name": "Hackeado"})
        with self.assertRaises(AccessError):
            self.sales_doc.with_user(self.ana).unlink()
        with self.assertRaises(AccessError):
            self.env["buddy.chunk"].with_user(self.ana).create({"document_id": self.sales_doc.id, "content": "inyectado"})
        with self.assertRaises(AccessError):
            self.sales_doc.chunk_ids.with_user(self.ana).write({"content": "reescrito"})
        self.env["ir.config_parameter"].sudo().set_param(DRIVE_KEY_PARAM, '{"client_email": "x@y.z"}')
        with self.assertRaises(AccessError):
            self.sales_source.with_user(self.ana).action_sync()
        self.assertEqual(self.sales_source.name, "Ventas")

    # --- validation ------------------------------------------------------------------------
    def test_invalid_folder_urls_are_rejected(self):
        for url in ("", "   ", "no es una url", "https://example.com/algo", "https://drive.google.com/file/d/abc123456789/view",
                    "https://docs.google.com/document/d/abc123456789/edit", "https://drive.google.com/drive/folders/", "http://x/folders/a"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                new_source(self.env, "Mal", [self.g_sales], folder_url=url)
        with self.assertRaises(ValidationError):
            self.sales_source.folder_url = "https://example.com/otra"

    def test_valid_folder_urls_compute_the_folder_id(self):
        cases = {
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMn": "1AbCdEfGhIjKlMn",
            "https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMn?usp=sharing": "1AbCdEfGhIjKlMn",
            "https://drive.google.com/open?id=1AbCdEfGhIjKlMn": "1AbCdEfGhIjKlMn",
            "  1AbCdEfGhIjKlMn  ": "1AbCdEfGhIjKlMn",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(new_source(self.env, "Ok", [self.g_sales], folder_url=url).folder_id, expected)
        self.sales_source.folder_url = DRIVE_URL + "OtraCarpeta12345"
        self.assertEqual(self.sales_source.folder_id, "OtraCarpeta12345")

    def test_a_folder_needs_at_least_one_group(self):
        with self.assertRaises(ValidationError):
            self.env["buddy.source"].create({"name": "Sin grupos", "folder_url": DRIVE_URL + "SinGrupos12345"})
        with self.assertRaises(ValidationError):
            self.sales_source.group_ids = [(5, 0, 0)]
        self.assertEqual(self.sales_source.group_ids, self.g_sales)

    def test_a_document_belongs_to_one_folder_once(self):
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.env["buddy.document"].create({"source_id": self.sales_source.id, "drive_id": self.sales_doc.drive_id, "name": "Dup"})


@tagged("post_install", "-at_install")
class TestBuddySyncCron(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.group = new_group(self.env, "Cron")
        self.calls = []
        self.params.set_param(DRIVE_KEY_PARAM, '{"client_email": "x@y.z"}')
        self.params.set_param(SYNC_HOURS_PARAM, "24")
        # A clean slate: only the folders of each test are considered.
        self.env["buddy.source"].sudo().with_context(active_test=False).search([]).unlink()

        def fake_sync(source, client=None, commit=False, deadline=None):
            self.calls.append(source.id)

        # The cron commits after each folder; inside the test transaction that must be a no-op.
        for patcher in (patch.object(self.env.registry["buddy.source"], "_sync", fake_sync),
                        patch.object(self.env.cr, "commit", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def folder(self, name="C", state="never", ago=None, written_ago=None, auto_sync=True, active=True):
        source = new_source(self.env, name, [self.group], auto_sync=auto_sync, active=active)
        values = {"state": state}
        if ago is not None:
            values["last_sync"] = fields.Datetime.now() - ago
        source.write(values)
        if written_ago is not None:
            self.env.cr.execute("UPDATE buddy_source SET write_date = %s WHERE id = %s", (fields.Datetime.now() - written_ago, source.id))
            source.invalidate_recordset(["write_date"])
        return source

    def run_cron(self):
        self.calls.clear()
        self.env["buddy.source"]._cron_sync()
        return list(self.calls)

    def test_cron_does_nothing_without_a_drive_key(self):
        queued, fresh = self.folder("Q", state="queued"), self.folder("N")
        self.params.set_param(DRIVE_KEY_PARAM, False)
        self.assertEqual(self.run_cron(), [])
        self.assertEqual((queued.state, fresh.state), ("queued", "never"))

    def test_queued_folders_always_run(self):
        queued = self.folder("Q", state="queued", auto_sync=False)
        self.assertEqual(self.run_cron(), [queued.id])

    def test_never_synced_folder_runs_when_auto_sync_is_on(self):
        auto, manual = self.folder("A"), self.folder("M", auto_sync=False)
        self.assertEqual(self.run_cron(), [auto.id])
        self.assertNotIn(manual.id, self.calls)

    def test_stale_folder_runs_after_the_interval_and_not_before(self):
        old = self.folder("Old", state="ok", ago=timedelta(hours=25))
        recent = self.folder("Recent", state="ok", ago=timedelta(hours=23))
        self.assertEqual(self.run_cron(), [old.id])
        self.assertNotIn(recent.id, self.calls)

    def test_interval_follows_the_setting(self):
        folder = self.folder("F", state="ok", ago=timedelta(hours=5))
        self.assertEqual(self.run_cron(), [])
        self.params.set_param(SYNC_HOURS_PARAM, "4")
        self.assertEqual(self.run_cron(), [folder.id])

    def test_interval_is_never_shorter_than_an_hour(self):
        self.folder("F", state="ok", ago=timedelta(minutes=30))
        self.params.set_param(SYNC_HOURS_PARAM, "0")
        self.assertEqual(self.run_cron(), [])

    def test_stale_folder_with_auto_sync_off_is_left_alone(self):
        self.folder("Manual", state="ok", ago=timedelta(days=30), auto_sync=False)
        self.assertEqual(self.run_cron(), [])

    def test_failed_folder_retries_after_an_hour_only_with_auto_sync(self):
        retry = self.folder("Retry", state="error", ago=RETRY_AFTER_ERROR + timedelta(minutes=1))
        wait = self.folder("Wait", state="error", ago=RETRY_AFTER_ERROR - timedelta(minutes=10))
        manual = self.folder("Manual", state="error", ago=timedelta(days=2), auto_sync=False)
        self.assertEqual(self.run_cron(), [retry.id])
        self.assertNotIn(wait.id, self.calls)
        self.assertNotIn(manual.id, self.calls)

    def test_stuck_folder_is_recovered_but_a_running_one_is_not(self):
        stuck = self.folder("Stuck", state="syncing", written_ago=STUCK_AFTER + timedelta(minutes=1))
        running = self.folder("Running", state="syncing", written_ago=timedelta(minutes=2))
        self.assertEqual(self.run_cron(), [stuck.id])
        self.assertNotIn(running.id, self.calls)

    def test_archived_folders_are_skipped(self):
        self.folder("Archived", state="queued", active=False)
        self.assertEqual(self.run_cron(), [])

    def test_up_to_date_folders_are_not_touched(self):
        self.folder("Fresh", state="ok", ago=timedelta(minutes=5))
        self.assertEqual(self.run_cron(), [])

    def test_cron_job_is_registered_and_calls_the_method(self):
        cron = self.env.ref("buddy_ia.cron_sync")
        self.assertIn("_cron_sync", cron.code)
        self.assertEqual(cron.model_id.model, "buddy.source")
        self.assertTrue(cron.active)


@tagged("post_install", "-at_install")
class TestBuddyActionSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.source = new_source(self.env, "Manual", [new_group(self.env, "Act")])

    def test_sync_button_without_a_key_explains_what_to_do(self):
        self.params.set_param(DRIVE_KEY_PARAM, False)
        with self.assertRaises(UserError) as caught:
            self.source.action_sync()
        self.assertTrue(str(caught.exception))
        self.assertEqual(self.source.state, "never")

    def test_sync_button_queues_the_folder_and_wakes_the_cron(self):
        self.params.set_param(DRIVE_KEY_PARAM, '{"client_email": "x@y.z"}')
        cron_model = self.env.registry["ir.cron"]
        with patch.object(cron_model, "_trigger") as trigger:
            action = self.source.action_sync()
        self.assertEqual(self.source.state, "queued")
        trigger.assert_called_once()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "info")
