"""Folder synchronization (buddy.source._sync) with a fake Drive client: no network, no Google account."""
import time

from odoo.tests.common import TransactionCase, tagged

from ..models.buddy_source import DRIVE_KEY_PARAM
from ..models.drive_client import DriveError
from .common import FakeClient, drive_file, new_group, new_source

LONG = "\n\n".join("Párrafo %d sobre procedimientos internos. " % n + "Detalle del paso. " * 12 for n in range(20))


@tagged("post_install", "-at_install")
class TestBuddySync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.group = new_group(self.env, "Sync")
        self.source = new_source(self.env, "Docs", [self.group])
        self.Document = self.env["buddy.document"]
        self.Chunk = self.env["buddy.chunk"]

    def docs(self):
        return {document.drive_id: document for document in self.Document.search([("source_id", "=", self.source.id)])}

    def chunks_of(self, document):
        return self.Chunk.search([("document_id", "=", document.id)], order="sequence")

    def client(self):
        return FakeClient(
            files=[drive_file("fileA", "Guía A", path="RRHH", md5="a1"), drive_file("fileB", "Guía B", modified="2026-02-01T00:00:00.000Z")],
            texts={"fileA": LONG, "fileB": "Cómo pedir vacaciones: avisá a tu líder."})

    def synced(self, client=None):
        client = client or self.client()
        self.source._sync(client=client)
        return client

    def test_first_sync_creates_documents_and_chunks(self):
        client = self.synced()
        self.assertEqual(client.listed, [self.source.folder_id])
        self.assertEqual(self.source.state, "ok")
        self.assertTrue(self.source.last_sync)
        self.assertFalse(self.source.last_error)
        self.assertIn("2", self.source.last_summary)
        docs = self.docs()
        self.assertEqual(set(docs), {"fileA", "fileB"})
        a = docs["fileA"]
        self.assertEqual((a.name, a.path, a.state, a.md5, a.mime_type), ("Guía A", "RRHH", "ready", "a1", "text/plain"))
        self.assertEqual(a.web_view_link, "https://docs.google.com/document/d/fileA/edit")
        self.assertEqual(a.modified_time, "2026-01-01T10:00:00.000Z")
        self.assertGreater(len(self.chunks_of(a)), 1, "a long text is split")
        self.assertEqual(self.chunks_of(a).mapped("sequence"), list(range(len(self.chunks_of(a)))))
        self.assertEqual(len(self.chunks_of(docs["fileB"])), 1)
        self.assertEqual(self.chunks_of(docs["fileB"]).source_id, self.source, "the chunk carries its folder")
        self.assertEqual(self.source.document_count, 2)

    def test_second_run_without_changes_downloads_nothing(self):
        self.synced()
        before = {code: self.chunks_of(document).ids for code, document in self.docs().items()}
        again = self.client()
        self.synced(again)
        self.assertEqual(again.fetched, [])
        self.assertEqual(self.source.state, "ok")
        self.assertEqual({code: self.chunks_of(document).ids for code, document in self.docs().items()}, before)

    def test_modified_file_replaces_its_chunks_only(self):
        self.synced()
        docs = self.docs()
        old_a_chunks, old_b_chunks = self.chunks_of(docs["fileA"]).ids, self.chunks_of(docs["fileB"]).ids
        changed = FakeClient(
            files=[drive_file("fileA", "Guía A v2", modified="2026-03-01T00:00:00.000Z", md5="a2"),
                   drive_file("fileB", "Guía B", modified="2026-02-01T00:00:00.000Z")],
            texts={"fileA": "Texto nuevo y corto sobre teletrabajo.", "fileB": "no se descarga"})
        self.synced(changed)
        self.assertEqual(changed.fetched, ["fileA"])
        docs = self.docs()
        self.assertEqual(len(docs), 2, "the same document is updated, not duplicated")
        self.assertEqual(docs["fileA"].name, "Guía A v2")
        self.assertEqual(self.chunks_of(docs["fileA"]).mapped("content"), ["Texto nuevo y corto sobre teletrabajo."])
        self.assertFalse(self.Chunk.search([("id", "in", old_a_chunks)]), "the old fragments are gone")
        self.assertEqual(self.chunks_of(docs["fileB"]).ids, old_b_chunks)

    def test_changed_checksum_with_same_timestamp_is_reindexed(self):
        self.synced()
        changed = FakeClient(files=[drive_file("fileA", "Guía A", path="RRHH", md5="OTRO"),
                                    drive_file("fileB", "Guía B", modified="2026-02-01T00:00:00.000Z")],
                             texts={"fileA": "contenido distinto"})
        self.synced(changed)
        self.assertEqual(changed.fetched, ["fileA"])
        self.assertEqual(self.chunks_of(self.docs()["fileA"]).mapped("content"), ["contenido distinto"])

    def test_file_deleted_in_drive_removes_document_and_chunks(self):
        self.synced()
        gone = self.chunks_of(self.docs()["fileB"]).ids
        remaining = FakeClient(files=[drive_file("fileA", "Guía A", path="RRHH", md5="a1")], texts={"fileA": LONG})
        self.synced(remaining)
        self.assertEqual(set(self.docs()), {"fileA"})
        self.assertEqual(remaining.fetched, [])
        self.assertFalse(self.Chunk.search([("id", "in", gone)]))
        self.assertEqual(self.source.state, "ok")

    def test_folder_emptied_in_drive_removes_everything(self):
        self.synced()
        self.synced(FakeClient())
        self.assertFalse(self.docs())
        self.assertFalse(self.Chunk.search([("source_id", "=", self.source.id)]))
        self.assertEqual(self.source.state, "ok")

    def test_file_without_text_is_marked_empty_and_not_downloaded_again(self):
        first = FakeClient(files=[drive_file("blank", "Escaneo"), drive_file("spaces", "Vacío")], texts={"blank": "", "spaces": "   \n\n "})
        self.synced(first)
        for code in ("blank", "spaces"):
            self.assertEqual(self.docs()[code].state, "empty", code)
            self.assertFalse(self.chunks_of(self.docs()[code]), code)
        self.assertEqual(self.source.state, "ok")
        again = FakeClient(files=first.files, texts=first.texts)
        self.synced(again)
        self.assertEqual(again.fetched, [])

    def test_text_added_to_an_empty_file_becomes_ready(self):
        self.synced(FakeClient(files=[drive_file("blank", "Doc")], texts={}))
        self.assertEqual(self.docs()["blank"].state, "empty")
        self.synced(FakeClient(files=[drive_file("blank", "Doc", modified="2026-05-01T00:00:00.000Z")], texts={"blank": "ahora sí hay texto"}))
        self.assertEqual(self.docs()["blank"].state, "ready")
        self.assertEqual(len(self.chunks_of(self.docs()["blank"])), 1)

    def test_non_fatal_file_error_is_recorded_and_the_rest_continues(self):
        client = self.client()
        client.errors["fileA"] = DriveError("too_large", "El archivo es demasiado grande.")
        self.synced(client)
        docs = self.docs()
        self.assertEqual((docs["fileA"].state, docs["fileA"].error), ("error", "El archivo es demasiado grande."))
        self.assertFalse(self.chunks_of(docs["fileA"]))
        self.assertEqual(docs["fileB"].state, "ready")
        self.assertEqual(len(self.chunks_of(docs["fileB"])), 1)
        self.assertEqual(self.source.state, "ok", "one bad file does not fail the folder")
        self.assertFalse(self.source.last_error)
        # A failed file is retried on the next run; the healthy one is not downloaded again.
        retry = self.client()
        self.synced(retry)
        self.assertEqual(retry.fetched, ["fileA"])
        self.assertEqual(self.docs()["fileA"].state, "ready")
        self.assertFalse(self.docs()["fileA"].error)

    def test_fatal_listing_error_marks_the_folder_and_keeps_the_index(self):
        self.synced()
        indexed = {code: self.chunks_of(document).ids for code, document in self.docs().items()}
        broken = FakeClient()
        broken.list_error = DriveError("auth_failed", "Google rechazó las credenciales.")
        self.synced(broken)
        self.assertEqual(self.source.state, "error")
        self.assertIn("rechazó", self.source.last_error)
        self.assertEqual({code: self.chunks_of(document).ids for code, document in self.docs().items()}, indexed)

    def test_fatal_download_error_stops_without_deleting_anything(self):
        self.synced()
        indexed = {code: self.chunks_of(document).ids for code, document in self.docs().items()}
        # fileB disappeared from the listing and a new file hits a fatal error: nothing may be removed.
        client = FakeClient(files=[drive_file("fileA", "Guía A", path="RRHH", md5="a1"), drive_file("fileNew", "Nuevo")],
                            errors={"fileNew": DriveError("rate_limited", "Google limitó las consultas.")})
        self.synced(client)
        self.assertEqual(self.source.state, "error")
        self.assertIn("limitó", self.source.last_error)
        self.assertEqual({code: self.chunks_of(document).ids for code, document in self.docs().items()}, indexed)

    def test_every_fatal_code_aborts_the_run(self):
        for code in ("invalid_key", "auth_failed", "api_disabled", "rate_limited", "network"):
            client = FakeClient(files=[drive_file("f-" + code)], errors={"f-" + code: DriveError(code, "falla " + code)})
            self.source._sync(client=client)
            self.assertEqual(self.source.state, "error", code)
            self.assertFalse(self.Document.search([("drive_id", "=", "f-" + code)]), code)

    def test_unexpected_exception_is_generic_and_never_leaks_details(self):
        client = FakeClient(files=[drive_file("fileA")], errors={"fileA": RuntimeError("SECRETO-interno-123")})
        self.synced(client)
        self.assertEqual(self.source.state, "error")
        self.assertTrue(self.source.last_error)
        self.assertNotIn("SECRETO", self.source.last_error)

    def test_missing_drive_key_is_a_clear_error_not_a_crash(self):
        self.env["ir.config_parameter"].sudo().set_param(DRIVE_KEY_PARAM, False)
        self.source._sync()
        self.assertEqual(self.source.state, "error")
        self.assertTrue(self.source.last_error)

    def test_next_successful_run_clears_the_error(self):
        broken = FakeClient()
        broken.list_error = DriveError("network", "Sin conexión con Google.")
        self.synced(broken)
        self.assertEqual(self.source.state, "error")
        self.synced()
        self.assertEqual((self.source.state, self.source.last_error), ("ok", False))

    def test_exhausted_time_budget_queues_the_folder_and_deletes_nothing(self):
        self.synced()
        indexed = {code: self.chunks_of(document).ids for code, document in self.docs().items()}
        client = FakeClient(
            files=[drive_file("fileA", "Guía A", modified="2026-03-01T00:00:00.000Z", md5="a2"), drive_file("fileNew", "Nuevo")],
            texts={"fileA": "versión nueva", "fileNew": "texto nuevo"})
        self.source._sync(client=client, deadline=time.monotonic() - 10)
        self.assertEqual(self.source.state, "queued")
        self.assertFalse(self.source.last_error)
        self.assertEqual(client.fetched, [], "no new download starts after the deadline")
        # fileB is missing from the listing, but a partial run must not delete it.
        self.assertEqual({code: self.chunks_of(document).ids for code, document in self.docs().items()}, indexed)
        # The next pass has time: it finishes the job.
        self.source._sync(client=client)
        self.assertEqual(self.source.state, "ok")
        self.assertEqual(set(self.docs()), {"fileA", "fileNew"})
        self.assertEqual(self.chunks_of(self.docs()["fileA"]).mapped("content"), ["versión nueva"])

    def test_nothing_to_download_finishes_even_after_the_deadline(self):
        self.synced()
        again = self.client()
        self.source._sync(client=again, deadline=time.monotonic() - 10)
        self.assertEqual(self.source.state, "ok")
        self.assertEqual(again.fetched, [])

    def test_file_limit_never_deletes_documents_left_out_of_the_listing(self):
        self.synced()
        indexed = {code: self.chunks_of(document).ids for code, document in self.docs().items()}
        cut = FakeClient(files=[drive_file("fileA", "Guía A", path="RRHH", md5="a1")])
        cut.truncated = True  # DriveClient stopped at max_files: fileB may still exist in Drive
        self.synced(cut)
        self.assertEqual(set(self.docs()), {"fileA", "fileB"}, "a truncated listing is not proof that fileB was deleted")
        self.assertEqual({code: self.chunks_of(document).ids for code, document in self.docs().items()}, indexed)
        self.assertEqual(self.source.state, "ok")
        # With a complete listing the deletion happens.
        cut.truncated = False
        self.synced(cut)
        self.assertEqual(set(self.docs()), {"fileA"})

    def test_deleting_a_folder_deletes_its_documents_and_chunks(self):
        self.synced()
        ids = self.Chunk.search([("source_id", "=", self.source.id)]).ids
        self.assertTrue(ids)
        self.source.unlink()
        self.assertFalse(self.Document.search([("drive_id", "in", ["fileA", "fileB"])]))
        self.assertFalse(self.Chunk.search([("id", "in", ids)]))

    def test_two_folders_may_contain_the_same_drive_file(self):
        other = new_source(self.env, "Otra", [self.group])
        self.synced()
        other._sync(client=self.client())
        self.assertEqual(self.Document.search_count([("drive_id", "=", "fileA")]), 2)
        self.source._sync(client=FakeClient())
        self.assertEqual(self.Document.search_count([("drive_id", "=", "fileA")]), 1, "only the synced folder lost its copy")
