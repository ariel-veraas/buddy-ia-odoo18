"""Robustness and security of 18.0.4.4.0 against a real Odoo registry: poison-pill files in the sync, the cron's fairness
and re-triggering, the global concurrency limit, field-level privacy, message ceiling, uninstall hook and settings."""
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.modules.module import get_manifest
from odoo.tests.common import TransactionCase, tagged

from .. import SECRET_PARAMETERS, uninstall_hook
from ..models import buddy_source
from ..models.buddy_chunker import MAX_CHUNKS_PER_DOCUMENT
from ..models.buddy_engine import BuddyBusy
from ..models.buddy_message import MAX_MESSAGE_AGE_DAYS
from ..models.buddy_source import DRIVE_KEY_PARAM, MIN_FOLDER_SECONDS, SYNC_BUDGET_SECONDS, SYNC_HOURS_PARAM
from ..models.drive_client import DriveError
from .common import FakeClient, drive_file, new_admin, new_document, new_group, new_source, new_user
from .test_engine import VALID, EngineCase

KEY = '{"client_email": "x@y.z"}'
STAMP_A = "2026-01-01T10:00:00.000Z|a1"
BIG = "\n\n".join("Párrafo %d " % number + "palabra " * 150 for number in range(500))  # far more than 400 chunks


@tagged("post_install", "-at_install")
class TestBuddyPoisonPill(TransactionCase):
    """A file that kills the worker (corrupt PDF, memory bomb) must not be retried forever."""

    def setUp(self):
        super().setUp()
        self.source = new_source(self.env, "Docs", [new_group(self.env, "Poison")])
        self.Document = self.env["buddy.document"]

    def docs(self):
        return {document.drive_id: document for document in self.Document.search([("source_id", "=", self.source.id)])}

    def files(self, **kwargs):
        return [drive_file("fileA", "Guía A", md5="a1", **kwargs), drive_file("fileB", "Guía B")]

    def client(self, **kwargs):
        return FakeClient(files=self.files(**kwargs), texts={"fileA": "Texto de A.", "fileB": "Texto de B."})

    def test_the_file_is_recorded_before_it_is_processed_and_cleared_after(self):
        seen = []
        source = self.source

        class Spy(FakeClient):
            def fetch_text(self, drive_file_):
                seen.append((drive_file_.id, source.processing_id, source.processing_stamp))
                return super().fetch_text(drive_file_)

        client = Spy(files=self.files(), texts={"fileA": "Texto de A.", "fileB": "Texto de B."})
        self.source._sync(client=client)
        self.assertEqual(seen, [("fileA", "fileA", STAMP_A), ("fileB", "fileB", "2026-01-01T10:00:00.000Z|")])
        self.assertEqual((self.source.processing_id, self.source.processing_stamp), (False, False))
        self.assertEqual(self.source.state, "ok")

    def test_a_file_that_killed_the_previous_pass_is_marked_error_and_the_pass_goes_on(self):
        self.source.write({"processing_id": "fileA", "processing_stamp": STAMP_A})  # what a dead worker leaves behind
        client = self.client()
        result = self.source._sync(client=client)
        self.assertEqual(client.fetched, ["fileB"], "the culprit is not downloaded again")
        document = self.docs()["fileA"]
        self.assertEqual((document.state, document.interrupted), ("error", True))
        self.assertIn("interrumpió", document.error)
        self.assertFalse(document.chunk_ids)
        self.assertEqual(self.docs()["fileB"].state, "ready")
        self.assertEqual(self.source.state, "ok", "one bad file does not fail the folder")
        self.assertEqual((self.source.processing_id, self.source.processing_stamp), (False, False))
        self.assertIn("1 con error", self.source.last_summary)
        self.assertEqual(result, {"pending": False, "progress": 2})

    def test_it_is_not_retried_on_later_passes_until_it_changes_in_drive(self):
        self.source.write({"processing_id": "fileA", "processing_stamp": STAMP_A})
        self.source._sync(client=self.client())
        for unused in range(3):
            again = self.client()
            self.source._sync(client=again)
            self.assertEqual(again.fetched, [], "no infinite retry")
            self.assertEqual(self.docs()["fileA"].state, "error")
            self.assertEqual(self.source.state, "ok")
        edited = FakeClient(files=[drive_file("fileA", "Guía A", modified="2027-02-02T00:00:00.000Z", md5="a2"), drive_file("fileB", "Guía B")],
                            texts={"fileA": "Texto nuevo de A.", "fileB": "Texto de B."})
        self.source._sync(client=edited)
        self.assertEqual(edited.fetched, ["fileA"], "an edited file gets another chance")
        document = self.docs()["fileA"]
        self.assertEqual((document.state, document.interrupted, document.error), ("ready", False, False))
        self.assertEqual(document.chunk_ids.mapped("content"), ["Texto nuevo de A."])

    def test_a_marker_for_another_version_or_a_vanished_file_is_just_cleared(self):
        self.source.write({"processing_id": "fileA", "processing_stamp": "2025-01-01T00:00:00.000Z|viejo"})
        client = self.client()
        self.source._sync(client=client)
        self.assertEqual(client.fetched, ["fileA", "fileB"], "the file changed since: it is processed")
        self.assertEqual(self.docs()["fileA"].state, "ready")
        self.source.write({"processing_id": "gone", "processing_stamp": STAMP_A})
        self.source._sync(client=self.client())
        self.assertNotIn("gone", self.docs())
        self.assertFalse(self.source.processing_id)

    def test_an_unexpected_failure_keeps_the_marker_so_the_next_pass_blames_that_file(self):
        crashing = FakeClient(files=self.files(), texts={"fileB": "Texto de B."}, errors={"fileA": RuntimeError("memoria")})
        self.source._sync(client=crashing)
        self.assertEqual(self.source.state, "error")
        self.assertEqual((self.source.processing_id, self.source.processing_stamp), ("fileA", STAMP_A))
        healthy = self.client()
        self.source._sync(client=healthy)
        self.assertEqual(healthy.fetched, ["fileB"])
        self.assertEqual(self.docs()["fileA"].interrupted, True)
        self.assertEqual(self.source.state, "ok")

    def test_a_fatal_drive_error_is_not_blamed_on_the_file(self):
        client = FakeClient(files=self.files(), errors={"fileA": DriveError("rate_limited", "Google limitó las consultas.")})
        self.source._sync(client=client)
        self.assertEqual(self.source.state, "error")
        self.assertFalse(self.source.processing_id, "rate limits say nothing about the file")

    def test_interrupted_documents_are_marked_in_every_state_the_view_needs(self):
        self.source.write({"processing_id": "fileA", "processing_stamp": STAMP_A})
        self.source._sync(client=self.client())
        document = self.docs()["fileA"]
        self.assertEqual(document.chunk_count, 0)
        self.assertTrue(document.error)


@tagged("post_install", "-at_install")
class TestBuddyTruncationIsVisible(TransactionCase):
    def setUp(self):
        super().setUp()
        self.source = new_source(self.env, "Docs", [new_group(self.env, "Trunc")])

    def test_a_document_cut_at_400_chunks_says_so(self):
        client = FakeClient(files=[drive_file("big", "Manual enorme"), drive_file("small", "Corto")],
                            texts={"big": BIG, "small": "Un texto corto."})
        self.source._sync(client=client)
        documents = {document.drive_id: document for document in self.env["buddy.document"].search([("source_id", "=", self.source.id)])}
        big = documents["big"]
        self.assertEqual(big.state, "ready")
        self.assertEqual(big.chunk_count, MAX_CHUNKS_PER_DOCUMENT)
        self.assertIn("truncado a 400 fragmentos", big.error)
        self.assertFalse(documents["small"].error)
        self.assertIn("1 documentos truncados", self.source.last_summary)

    def test_a_document_cut_by_the_page_or_size_limit_says_so(self):
        client = FakeClient(files=[drive_file("pdf", "Libro")], texts={"pdf": "Contenido de las primeras páginas."})
        client.last_fetch_truncated = True
        self.source._sync(client=client)
        document = self.env["buddy.document"].search([("source_id", "=", self.source.id)])
        self.assertIn("truncado", document.error)
        self.assertIn("1 documentos truncados", self.source.last_summary)

    def test_the_note_goes_away_when_the_document_is_shortened(self):
        self.source._sync(client=FakeClient(files=[drive_file("big", "Manual")], texts={"big": BIG}))
        document = self.env["buddy.document"].search([("source_id", "=", self.source.id)])
        self.assertTrue(document.error)
        self.source._sync(client=FakeClient(files=[drive_file("big", "Manual", modified="2027-01-01T00:00:00.000Z")],
                                            texts={"big": "Ahora es corto."}))
        self.assertFalse(document.error)
        self.assertEqual(document.chunk_count, 1)


@tagged("post_install", "-at_install")
class TestBuddySyncCronFairness(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.group = new_group(self.env, "Fair")
        self.params.set_param(SYNC_HOURS_PARAM, "24")
        self.params.set_param(DRIVE_KEY_PARAM, False)  # folders are created before the key exists: no auto-queue noise
        self.env["buddy.source"].sudo().with_context(active_test=False).search([]).unlink()
        self.calls, self.shares, self.replies = [], [], []
        test = self

        def fake_sync(source, client=None, commit=False, deadline=None):
            test.calls.append(source.name)
            test.shares.append(deadline - time.monotonic())
            return test.replies.pop(0) if test.replies else None

        # The cron commits after each folder; inside the test transaction that must be a no-op.
        for patcher in (patch.object(self.env.registry["buddy.source"], "_sync", fake_sync),
                        patch.object(self.env.cr, "commit", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def folder(self, name, state="never", ago=None):
        source = new_source(self.env, name, [self.group])
        values = {"state": state}
        if ago is not None:
            values["last_sync"] = fields.Datetime.now() - ago
        source.write(values)
        return source

    def run_cron(self, *replies):
        """Run the cron with these answers from `_sync` (in order); returns the mock of the cron wake-up call."""
        self.calls.clear()
        self.shares.clear()
        self.replies[:] = replies
        self.params.set_param(DRIVE_KEY_PARAM, KEY)
        with patch.object(self.env.registry["ir.cron"], "_trigger") as trigger:
            self.env["buddy.source"]._cron_sync()
        return trigger

    def test_queued_folders_go_first_and_each_group_by_whoever_waited_longest(self):
        self.folder("nunca")  # never synchronized
        self.folder("cola reciente", state="queued", ago=timedelta(days=1))
        self.folder("cola vieja", state="queued", ago=timedelta(days=3))
        self.folder("vencida", state="ok", ago=timedelta(hours=25))
        self.run_cron()
        self.assertEqual(self.calls, ["cola vieja", "cola reciente", "nunca", "vencida"])

    def test_each_folder_gets_a_fair_share_of_the_budget_not_all_of_it(self):
        for number in range(4):
            self.folder("F%d" % number, state="queued")
        self.run_cron()
        self.assertEqual(len(self.shares), 4)
        self.assertLess(self.shares[0], SYNC_BUDGET_SECONDS / 4 + 1)
        self.assertGreater(self.shares[0], SYNC_BUDGET_SECONDS / 4 - 3)
        self.env["buddy.source"].sudo().search([]).unlink()
        self.folder("sola", state="queued")
        self.run_cron()
        self.assertGreater(self.shares[0], SYNC_BUDGET_SECONDS - 3, "a single folder may use the whole budget")

    def test_many_folders_still_get_a_minimum_share(self):
        for number in range(30):
            self.folder("F%02d" % number, state="queued")
        self.run_cron()
        self.assertGreater(self.shares[0], MIN_FOLDER_SECONDS - 2)

    def test_the_cron_calls_itself_again_only_when_work_is_left_and_progress_was_made(self):
        self.folder("Grande", state="queued")
        for reply, expected in (({"pending": True, "progress": 3}, True), ({"pending": True, "progress": 0}, False),
                                ({"pending": False, "progress": 5}, False), ({"pending": False, "progress": 0}, False),
                                (None, False)):
            with self.subTest(reply=reply):
                self.assertEqual(self.run_cron(reply).called, expected)

    def test_progress_is_added_up_over_all_folders(self):
        self.folder("A", state="queued")
        self.folder("B", state="queued")
        trigger = self.run_cron({"pending": True, "progress": 0}, {"pending": False, "progress": 2})
        self.assertTrue(trigger.called, "one folder has work left and the pass as a whole moved forward")
        trigger = self.run_cron({"pending": True, "progress": 0}, {"pending": False, "progress": 0})
        self.assertFalse(trigger.called)

    def test_when_the_budget_runs_out_the_rest_waits_for_the_next_run(self):
        for number in range(3):
            self.folder("F%d" % number, state="queued")
        with patch.object(buddy_source, "SYNC_BUDGET_SECONDS", 0):
            trigger = self.run_cron()
        self.assertEqual(self.calls, [], "no time at all: nothing starts")
        self.assertFalse(trigger.called, "and with no progress it does not spin")
        self.run_cron()
        self.assertEqual(len(self.calls), 3, "the next run takes them")


@tagged("post_install", "-at_install")
class TestBuddyNewFolderStartsSyncing(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.group = new_group(self.env, "Auto")

    def test_with_a_drive_key_a_new_folder_is_queued_and_the_cron_wakes_up(self):
        self.params.set_param(DRIVE_KEY_PARAM, KEY)
        with patch.object(self.env.registry["ir.cron"], "_trigger") as trigger:
            source = new_source(self.env, "Nueva", [self.group])
        self.assertEqual(source.state, "queued")
        trigger.assert_called_once()

    def test_without_a_key_nothing_is_queued(self):
        self.params.set_param(DRIVE_KEY_PARAM, False)
        with patch.object(self.env.registry["ir.cron"], "_trigger") as trigger:
            source = new_source(self.env, "Sin clave", [self.group])
        self.assertEqual(source.state, "never")
        trigger.assert_not_called()

    def test_archived_folders_and_batches_are_handled(self):
        self.params.set_param(DRIVE_KEY_PARAM, KEY)
        with patch.object(self.env.registry["ir.cron"], "_trigger") as trigger:
            archived = new_source(self.env, "Archivada", [self.group], active=False)
            first, second = (new_source(self.env, "Lote %d" % number, [self.group]) for number in range(2))
        self.assertEqual(archived.state, "never")
        self.assertEqual((first.state, second.state), ("queued", "queued"))
        self.assertEqual(trigger.call_count, 2)


@tagged("post_install", "-at_install")
class TestBuddyGlobalConcurrency(EngineCase):
    """`buddy_ia.max_concurrent` questions at a time across ALL users; the next one waits and pays nothing."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.users = [new_user(self.env, "conc%d" % number) for number in range(5)]
        self.P.set_param("buddy_ia.max_concurrent", "2")

    def engine(self, user):
        return self.env["buddy.engine"].with_user(user)

    def start(self, user):
        engine = self.engine(user)
        return engine._start_request("Pregunta de " + user.name, engine._cfg(), engine._session_generation(), str(uuid.uuid4()))

    def usage(self, user):
        return self.env["buddy.usage"].sudo().search_count([("user_id", "=", user.id)])

    def test_the_default_is_three_and_it_is_read_from_the_setting(self):
        self.P.search([("key", "=", "buddy_ia.max_concurrent")]).unlink()
        self.assertEqual(self.engine(self.users[0])._cfg()["max_concurrent"], 3)
        self.P.set_param("buddy_ia.max_concurrent", "7")
        self.assertEqual(self.engine(self.users[0])._cfg()["max_concurrent"], 7)

    def test_over_the_limit_the_newcomer_is_told_to_wait_and_nothing_is_spent(self):
        first, second = self.start(self.users[0]), self.start(self.users[1])
        late = self.users[2]
        with self.assertRaises(BuddyBusy) as caught:
            self.start(late)
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self.usage(late), 0, "no quota was reserved")
        self.assertEqual(self.env["buddy.message"].sudo().search_count([("user_id", "=", late.id)]), 0, "the question was not stored")
        self.assertFalse(self.env["buddy.session"].sudo().search([("user_id", "=", late.id)]).inflight_id, "no lease was taken")
        # The same user can ask as soon as a slot frees up, with the quota intact.
        self.engine(self.users[0])._request_live(first, response=dict(VALID, session_generation=first["generation"]))
        token = self.start(late)
        self.assertEqual(self.usage(late), 1)
        self.assertTrue(token["usage_id"])
        self.assertTrue(second["usage_id"])

    def test_a_failed_or_released_question_frees_its_slot(self):
        tokens = [self.start(self.users[0]), self.start(self.users[1])]
        with self.assertRaises(BuddyBusy):
            self.start(self.users[2])
        self.engine(self.users[1])._request_live(tokens[1], error="provider_error")
        self.start(self.users[2])

    def test_an_expired_lease_does_not_count(self):
        self.start(self.users[0])
        self.start(self.users[1])
        stale = self.env["buddy.session"].sudo().search([("user_id", "=", self.users[0].id)])
        stale.write({"inflight_until": fields.Datetime.now() - timedelta(seconds=5)})
        self.start(self.users[2])  # the crashed question of user 0 no longer holds a slot

    def test_a_user_with_a_question_in_flight_still_gets_the_per_user_message(self):
        self.start(self.users[0])
        with self.assertRaises(BuddyBusy) as caught:
            self.start(self.users[0])
        self.assertEqual(caught.exception.code, "busy")

    def test_the_limit_applies_to_streamed_questions_too(self):
        self.P.set_param("buddy_ia.max_concurrent", "1")
        self.start(self.users[0])
        with self.assertRaises(BuddyBusy) as caught:
            self.engine(self.users[1]).stream_prepare("Hola", None, str(uuid.uuid4()))
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self.usage(self.users[1]), 0)

    def test_the_limit_is_validated(self):
        for bad in ("0", "101", "-1", "tres"):
            self.P.set_param("buddy_ia.max_concurrent", bad)
            with self.subTest(bad=bad), self.assertRaises(BuddyBusy) as caught:
                self.engine(self.users[0])._cfg()
            self.assertEqual(caught.exception.code, "not_configured")


@tagged("post_install", "-at_install")
class TestBuddyFieldPrivacy(TransactionCase):
    def setUp(self):
        super().setUp()
        self.group = new_group(self.env, "Priv")
        self.ana = new_user(self.env, "ana", extra_groups=[self.group])
        self.boss = new_admin(self.env)
        self.source = new_source(self.env, "Ventas", [self.group])
        self.document = new_document(self.env, self.source, "Cotizar", "Texto.", md5="abc123", error="detalle interno")

    def test_technical_fields_are_for_administrators_only(self):
        document = self.document.with_user(self.ana)
        self.assertEqual(document.name, "Cotizar", "employees still read what the citations need")
        self.assertTrue(document.web_view_link.startswith("https://docs.google.com/"))
        self.assertEqual(document.state, "ready")
        for field in ("drive_id", "md5", "error", "interrupted"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                document.read([field])
            with self.subTest(attribute=field), self.assertRaises(AccessError):
                getattr(document, field)

    def test_employees_cannot_search_by_them_either(self):
        for field in ("drive_id", "md5", "error"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.env["buddy.document"].with_user(self.ana).search([(field, "ilike", "a")])

    def test_administrators_read_them(self):
        document = self.document.with_user(self.boss)
        self.assertEqual((document.md5, document.error, document.interrupted), ("abc123", "detalle interno", False))
        self.assertTrue(document.drive_id)

    def test_the_sync_marker_is_private_too(self):
        for field in ("processing_id", "processing_stamp"):
            with self.subTest(field=field), self.assertRaises(AccessError):
                getattr(self.source.with_user(self.ana), field)

    def test_the_retrieval_still_works_for_employees(self):
        hits = self.env["buddy.engine"].with_user(self.ana)._retrieve("cotizar texto", {"top_k": 4})
        self.assertEqual([hit["title"] for hit in hits], ["Cotizar"])
        catalog = self.env["buddy.engine"].with_user(self.ana)._catalog()
        self.assertEqual(catalog["titles"], ["Cotizar"])


@tagged("post_install", "-at_install")
class TestBuddyMaxAge(EngineCase):
    """No message outlives 180 days, whatever the retention setting and however active the conversation is."""

    def setUp(self):
        super().setUp()
        self.P.set_param("buddy_ia.retention_days", "30")
        self.user = new_user(self.env, "viejo")
        self.Message = self.env["buddy.message"].sudo()

    def message(self, days, text="m"):
        row = self.Message.create({"user_id": self.user.id, "role": "user", "content": text, "generation": 1})
        self.env.cr.execute("UPDATE buddy_message SET create_date = now() at time zone 'utc' - %s * interval '1 day' WHERE id = %s",
                            (days, row.id))
        row.invalidate_recordset()
        return row

    def texts(self):
        return sorted(self.Message.search([("user_id", "=", self.user.id)]).mapped("content"))

    def test_an_old_message_goes_even_in_a_conversation_that_is_still_active(self):
        self.message(200, "viejo")
        self.message(1, "reciente")
        self.Message._cron_purge()
        self.assertEqual(self.texts(), ["reciente"])

    def test_the_ceiling_is_180_days_and_applies_with_retention_zero(self):
        self.assertEqual(MAX_MESSAGE_AGE_DAYS, 180)
        self.P.set_param("buddy_ia.retention_days", "0")
        self.message(181, "pasado")
        self.message(179, "justo")
        self.Message._cron_purge()
        self.assertEqual(self.texts(), ["justo"])

    def test_it_also_applies_when_retention_is_longer_than_the_ceiling(self):
        self.P.set_param("buddy_ia.retention_days", "365")
        self.message(190, "pasado")
        self.message(100, "bien")
        self.Message._cron_purge()
        self.assertEqual(self.texts(), ["bien"])

    def test_large_purges_run_in_batches(self):
        for unused in range(7):
            self.message(300)
        with patch("odoo.addons.buddy_ia.models.buddy_message.PURGE_BATCH", 3):
            self.Message._cron_purge()
        self.assertEqual(self.texts(), [])


@tagged("post_install", "-at_install")
class TestBuddyAuditAndUninstall(EngineCase):
    def test_wiping_every_conversation_leaves_a_warning_with_the_uid(self):
        admin = new_admin(self.env)
        ana = new_user(self.env, "ana")
        engine = self.env["buddy.engine"].with_user(ana)
        engine._remember("user", "hola", engine._session_generation())
        with self.assertLogs("odoo.addons.buddy_ia.models.buddy_engine", level="WARNING") as captured:
            count = self.env["buddy.engine"].with_user(admin)._delete_all_conversations()
        self.assertEqual(count, 1)
        text = "\n".join(captured.output)
        self.assertIn("uid %s" % admin.id, text)
        self.assertIn("1 mensajes", text)
        self.assertNotIn("hola", text, "the log never carries conversation text")

    def test_uninstall_hook_erases_the_secrets_and_only_the_secrets(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("buddy_ia.api_key", "sk-secreto")
        params.set_param(DRIVE_KEY_PARAM, KEY)
        params.set_param("buddy_ia.buddy_name", "Buddy")
        params.set_param("web.base.url", "http://localhost:8069")
        self.assertEqual(set(SECRET_PARAMETERS), {"buddy_ia.api_key", "buddy_ia.drive_key"})
        uninstall_hook(self.env)
        for key in SECRET_PARAMETERS:
            self.assertFalse(params.search([("key", "=", key)]), key)
            self.assertFalse(params.get_param(key))
        self.assertEqual(params.get_param("buddy_ia.buddy_name"), "Buddy")
        self.assertTrue(params.get_param("web.base.url"))
        uninstall_hook(self.env)  # nothing left to erase: harmless

    def test_the_manifest_registers_the_hook_and_the_new_version(self):
        manifest = get_manifest("buddy_ia")
        self.assertEqual(manifest["uninstall_hook"], "uninstall_hook")
        self.assertEqual(manifest["version"], "18.0.5.5.0")


@tagged("post_install", "-at_install")
class TestBuddySettings4_4(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.Settings = self.env["res.config.settings"]

    def test_max_concurrent_defaults_to_three_and_is_saved(self):
        self.params.search([("key", "=", "buddy_ia.max_concurrent")]).unlink()
        self.assertEqual(self.Settings.create({}).buddy_max_concurrent, 3)
        self.Settings.create({"buddy_max_concurrent": 5}).execute()
        self.assertEqual(self.params.get_param("buddy_ia.max_concurrent"), "5")

    def test_max_concurrent_is_bounded(self):
        for bad in (0, -1, 101):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.Settings.create({"buddy_max_concurrent": bad})

    def test_the_setting_is_visible_in_the_settings_screen(self):
        view = self.env.ref("buddy_ia.res_config_settings_view_form_buddy")
        self.assertIn('name="buddy_max_concurrent"', view.arch_db)

    def test_metadata_addresses_cannot_be_saved_as_the_provider_address(self):
        for base in ("http://169.254.169.254/latest", "http://metadata.google.internal/v1", "http://[fe80::1]/v1", "http://2852039166/"):
            with self.subTest(base=base), self.assertRaises(ValidationError):
                self.Settings.create({"buddy_provider": "custom", "buddy_api_base": base})
        self.Settings.create({"buddy_provider": "custom", "buddy_api_base": "http://stub-llm:9100/v1"})
        self.Settings.create({"buddy_provider": "custom", "buddy_api_base": "http://localhost:11434/v1"})
