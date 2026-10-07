"""Retrieval quality against a real PostgreSQL: vocabulary mismatch, indexed context, neighbours and noise.

The first class reproduces a real report: the same question with other words got a full answer once and "it is not in
the documents" twice, because the right document said "solicitud" and the question said "ticket".
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models import buddy_engine
from ..models.buddy_chunker import strip_toc
from ..models.buddy_text import all_synonyms
from .common import FakeClient, drive_file, new_document, new_group, new_source, new_user

QUESTIONS = ("Como genero un ticket?", "Podrias indicarme como generar un ticket?", "Me decis como subir un ticket?")
KEY_TEXT = ("Nota: Es importante recalcar que el usuario se DEBE loguear al portal, caso contrario las solicitudes "
            "se generarán pero no se podrá dar un seguimiento adecuado.")
COMPANY_SYNONYMS = "generar, crear, cargar, subir"


def multi_document(env, source, name, texts, **values):
    """A ready document with one chunk per text, in order (sequence 0, 1, 2...)."""
    document = new_document(env, source, name, texts[0], **values)
    for sequence, text in enumerate(texts[1:], 1):
        env["buddy.chunk"].create({"document_id": document.id, "sequence": sequence, "content": text})
    return document


class RetrievalCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env["buddy.source"].with_context(active_test=False).search([]).unlink()
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("buddy_ia.api_key", "not-a-real-key")
        params.set_param("buddy_ia.chat_model", "qa-chat")
        self.params = params
        self.group = new_group(self.env, "T Quality")
        self.ana = new_user(self.env, "t_quality_ana", extra_groups=[self.group])
        self.source = new_source(self.env, "Procedimientos", [self.group])

    def cfg(self, **over):
        cfg = self.env["buddy.engine"].with_user(self.ana)._cfg()
        cfg.update(over)
        return cfg

    def retrieve(self, message, user=None, **over):
        engine = self.env["buddy.engine"].with_user(user or self.ana)
        return engine._retrieve(message, self.cfg(**over))

    def texts(self, hits):
        return [hit["text"] for hit in hits]


@tagged("post_install", "-at_install")
class TestTicketVersusSolicitud(RetrievalCase):
    """Doc A repeats 'ticket' everywhere; doc B (the right one) says 'solicitud' and never 'ticket' in its key chunk."""

    def setUp(self):
        super().setUp()
        users = [
            "El equipo de Selección carga el ticket de Alta de usuario con los accesos requeridos. Cada ticket llega a Infraestructura.",
            "Infraestructura atiende el ticket de Baja y cierra el ticket cuando se quitan los accesos. El ticket de Baja es urgente.",
            "Para una modificación se abre un ticket nuevo. El ticket indica qué cambia y quién lo aprobó. Sin ticket no hay cambios.",
            "Cada ticket de usuario queda registrado. Revisá el estado del ticket en el tablero y respondé el ticket si te piden datos.",
            "Los tickets de proveedores se validan con el manager del área. Un ticket sin validación vuelve al solicitante, ticket por ticket.",
            "Registro de tickets: Alta, Baja y Modificación. Archivo de cada ticket por tres años. Ticket cerrado, ticket archivado.",
            "El ticket de contraseñas nunca incluye la contraseña. Si el ticket la incluye, se rechaza el ticket y se abre otro ticket.",
            "Control mensual: Infraestructura revisa los tickets abiertos y los tickets vencidos. Cada ticket pendiente tiene responsable.",
        ]
        helpdesk = [
            "Objetivo: definir un esquema de servicio ágil para la asistencia a los usuarios finales y el seguimiento de sus solicitudes.",
            "Technical Support recibe y registra todas las solicitudes que surjan por las diferentes vías de comunicación.",
            "Plataforma: se utiliza Odoo, módulo de HelpDesk. El usuario ingresa al sitio y se loguea con su usuario para solicitar soporte.",
            KEY_TEXT,
            "Carga de solicitudes: el usuario puede cargar una solicitud por el chat de soporte, con un agente o por correo electrónico.",
            "Seguimiento: cada actualización del estado de la solicitud se comunica por correo. Estados: New, Assigned, Solved.",
        ]
        self.doc_a = multi_document(self.env, self.source, "PR 38 Gestión de usuarios V7.docx", users)
        self.doc_b = multi_document(self.env, self.source, "PR 46 HelpDesk V2.docx", helpdesk)

    def assertKeyChunkFound(self, question):
        hits = self.retrieve(question)
        found = [hit for hit in hits if hit["text"] == KEY_TEXT]
        self.assertTrue(found, "%r did not bring the HelpDesk chunk. Got: %r" % (
            question, [(hit["title"], hit["sequence"]) for hit in hits]))
        self.assertEqual(found[0]["title"], "PR 46 HelpDesk V2.docx")
        return hits

    def test_the_three_phrasings_find_the_helpdesk_chunk_with_the_default_synonyms(self):
        for question in QUESTIONS:
            with self.subTest(question=question):
                self.assertKeyChunkFound(question)

    def test_when_no_word_pins_the_answer_both_documents_are_represented(self):
        # "subir" appears in neither document: every chunk that mentions a ticket/solicitud ties, and both documents show up.
        hits = self.assertKeyChunkFound("Me decis como subir un ticket?")
        self.assertEqual(len({hit["doc_id"] for hit in hits}), 2)

    def test_and_with_the_company_synonyms_on_top(self):
        self.params.set_param("buddy_ia.synonyms", COMPANY_SYNONYMS)
        for question in QUESTIONS:
            with self.subTest(question=question):
                self.assertKeyChunkFound(question)

    def test_without_synonyms_the_gap_is_real(self):
        """Control: this is the report. With no synonym groups at all the HelpDesk chunk is not retrieved."""
        hits = self.env["buddy.engine"].with_user(self.ana)._retrieve("Como genero un ticket?", self.cfg(synonyms=[], top_k=3))
        self.assertNotIn(KEY_TEXT, self.texts(hits))

    def test_one_repetitive_document_cannot_fill_every_slot(self):
        hits = self.retrieve("ticket", top_k=4)
        primary = [hit for hit in hits if not hit["neighbor"]]
        self.assertLessEqual(len([hit for hit in primary if hit["doc_id"] == self.doc_a.id]), buddy_engine.MAX_CHUNKS_PER_DOCUMENT)
        self.assertIn(self.doc_b.id, {hit["doc_id"] for hit in hits})

    def test_sources_stay_one_per_document(self):
        engine = self.env["buddy.engine"].with_user(self.ana)
        hits = self.retrieve(QUESTIONS[2])
        sources = engine._dedupe_sources(hits)
        self.assertEqual(sorted(source["title"] for source in sources),
                         ["PR 38 Gestión de usuarios V7.docx", "PR 46 HelpDesk V2.docx"])


@tagged("post_install", "-at_install")
class TestCompanySynonyms(RetrievalCase):
    def test_company_synonyms_alone_bridge_a_vocabulary_gap_the_defaults_do_not_know(self):
        new_document(self.env, self.source, "Licencias", "Los días libres por estudio se solicitan a RR.HH. con el comprobante.")
        self.assertEqual(self.retrieve("¿Cómo pido vacaciones?"), [])
        self.params.set_param("buddy_ia.synonyms", "vacaciones, licencia, días libres")
        hits = self.retrieve("¿Cómo pido vacaciones?")
        self.assertEqual([hit["title"] for hit in hits], ["Licencias"])


@tagged("post_install", "-at_install")
class TestTolerantRetrieval(RetrievalCase):
    """Typos, -ción versus -ciones and "ERP" for Odoo: the second try that only runs when the first search is weak."""

    def setUp(self):
        super().setUp()
        self.backups = new_document(self.env, self.source, "Procedimiento de backups", "Los respaldos se hacen cada noche en el servidor.")
        self.users = new_document(self.env, self.source, "Alta de usuarios", "Pedí el alta con un ticket a Infraestructura.")
        self.engine = self.env["buddy.engine"].with_user(self.ana)

    def primary(self, hits):
        return [hit["doc_id"] for hit in hits if not hit["neighbor"]]

    def test_singular_and_plural_of_a_cion_word_find_each_other(self):
        sales = new_document(self.env, self.source, "Guía de ventas", "Para crear una cotización abrí Ventas y elegí Nuevo.")
        purchases = new_document(self.env, self.source, "Guía de compras", "Las cotizaciones de proveedores se comparan en Compras.")
        for question in ("cotizacion", "cotizaciones", "las cotizaciones", "COTIZACIÓN"):
            with self.subTest(question=question):
                self.assertEqual(set(self.primary(self.retrieve(question))), {sales.id, purchases.id})
        # With more words in the question the chunk that matches most of them leads (the share rule), as always.
        self.assertEqual(self.primary(self.retrieve("como creo una cotizacion"))[0], sales.id)

    def test_a_typo_is_fixed_only_because_the_first_search_found_nothing(self):
        self.assertEqual(self.primary(self.retrieve("como hago bakups")), [self.backups.id])
        self.assertEqual(self.primary(self.retrieve("tiket")), [self.users.id], "the fixed word brings its synonym group")
        self.assertEqual(self.primary(self.retrieve("procedimeinto de respaldo")), [self.backups.id])

    def test_erp_is_odoo_and_backup_is_respaldo_out_of_the_box(self):
        odoo = new_document(self.env, self.source, "Sistema", "Todo se carga en Odoo: pedidos, clientes y facturas.")
        self.assertEqual(self.primary(self.retrieve("que es el ERP")), [odoo.id])
        self.assertIn(self.backups.id, self.primary(self.retrieve("como hago un backup")))
        self.assertIn(self.backups.id, self.primary(self.retrieve("copia de seguridad")))

    def test_a_strong_first_search_is_not_reformulated(self):
        new_document(self.env, self.source, "Cotizaciones", "Abrí Ventas y elegí Nuevo.")
        with patch.object(self.env.registry["buddy.engine"], "_widened_groups", return_value=None) as widened:
            self.assertTrue(self.retrieve("cotizaciones"))
            widened.assert_not_called()
            self.assertEqual(self.retrieve("capital de Australia"), [])
            widened.assert_called_once()

    def test_the_vocabulary_only_has_what_the_person_may_read(self):
        group = new_group(self.env, "T Secret")
        secret = new_source(self.env, "Dirección", [group])
        new_document(self.env, secret, "Escala salarial gerencial", "Los gerentes cobran según la escala.")
        self.assertEqual(self.retrieve("salarail"), [], "she cannot see that folder, so nothing is 'fixed' towards it")
        director = new_user(self.env, "t_quality_dir", extra_groups=[group])
        self.assertTrue(self.retrieve("salarail", user=director), "the same typo is fixed for who may read it")

    def test_nonsense_and_unrelated_questions_still_find_nothing(self):
        for question in ("capital de Australia", "zzzzzz qqqqqq", "¿Cuál es la receta del asado?", "xyxyxyxyxy"):
            with self.subTest(question=question):
                self.assertEqual(self.retrieve(question), [])

    def test_widening_never_costs_a_provider_call(self):
        with patch.object(self.env.registry["buddy.provider"], "_chat", side_effect=AssertionError("no provider call")):
            self.assertTrue(self.retrieve("como hago bakups"))

    def test_weakness_and_quality_of_a_search(self):
        engine = self.env["buddy.engine"]
        groups = [[("a",)], [("b",)]]
        weak, better = [(1, 1, 0, 0.09, 1)], [(2, 2, 0, 0.23, 2)]
        self.assertTrue(engine._weak([]))
        self.assertTrue(engine._weak(weak))
        self.assertFalse(engine._weak(better))
        self.assertEqual(engine._quality([], groups), (0.0, 0.0))
        self.assertGreater(engine._quality(better, groups), engine._quality(weak, groups))
        self.assertGreater(engine._quality(weak, groups), engine._quality([], groups))


@tagged("post_install", "-at_install")
class TestIndexedContext(RetrievalCase):
    def test_a_query_by_the_document_title_finds_chunks_that_never_say_it(self):
        guide = multi_document(self.env, self.source, "PR 46 Onboarding V2.docx", [
            "Pasos para tu primer día: pedí tu usuario y tu correo.", "Después presentate con tu líder directo."])
        new_document(self.env, self.source, "Otra guía", "En el onboarding de ventas se usa otra lista de pasos.")
        hits = self.retrieve("onboarding")
        self.assertEqual(hits[0]["doc_id"], guide.id, "the title weighs more than a body mention")
        self.assertEqual(len({hit["doc_id"] for hit in hits}), 2)
        self.assertEqual(len([hit for hit in hits if hit["doc_id"] == guide.id]), 2)

    def test_sub_folders_and_folder_name_are_indexed(self):
        new_document(self.env, self.source, "Plantilla", "Completá el formulario con tres presupuestos.", path="Compras/Proveedores")
        self.assertEqual([hit["title"] for hit in self.retrieve("proveedores")], ["Plantilla"])
        self.assertEqual([hit["title"] for hit in self.retrieve("procedimientos")], ["Plantilla"], "the connected folder's name")

    def test_the_context_follows_a_rename_and_has_no_file_extension(self):
        document = new_document(self.env, self.source, "Informe.pdf", "Resultados del trimestre.")
        self.assertEqual(document.chunk_ids.context_text, "Informe · Procedimientos")
        document.name = "Balance anual.docx"
        self.assertEqual(document.chunk_ids.context_text, "Balance anual · Procedimientos")
        self.assertEqual([hit["title"] for hit in self.retrieve("balance")], ["Balance anual.docx"])
        self.assertEqual(self.retrieve("informe"), [])

    def test_accents_are_folded_in_the_context_too(self):
        new_document(self.env, self.source, "Gestión de cotizaciones", "Abrí la aplicación y elegí Nuevo.")
        for question in ("gestion", "GESTIÓN", "cotizaciones"):
            self.assertEqual(len(self.retrieve(question)), 1, question)

    def test_the_search_column_is_weighted_and_init_is_idempotent(self):
        document = new_document(self.env, self.source, "Zeta", "Alfa beta.")
        self.env.flush_all()
        cr = self.env.cr
        for unused in range(2):
            self.env["buddy.chunk"].init()
        cr.execute("SELECT generation_expression FROM information_schema.columns "
                   "WHERE table_name = 'buddy_chunk' AND column_name = 'buddy_fts'")
        expression = cr.fetchone()[0]
        self.assertIn("setweight", expression)
        self.assertIn("context_text", expression)
        cr.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'buddy_chunk_fts_idx'")
        self.assertEqual(cr.fetchone()[0], 1)
        cr.execute("SELECT buddy_fts::text FROM buddy_chunk WHERE document_id = %s", (document.id,))
        vector = cr.fetchone()[0]
        self.assertIn("'zet':1A", vector, "title: weight A")
        self.assertIn("'alfa':3B", vector, "body: weight B, after the context words")

    def test_a_column_from_the_previous_version_is_rebuilt_without_losing_chunks(self):
        document = multi_document(self.env, self.source, "Manual de caja", ["Abrir la caja con la llave.", "Cerrar la caja."])
        cr = self.env.cr
        cr.execute("DROP INDEX IF EXISTS buddy_chunk_fts_idx")
        cr.execute("ALTER TABLE buddy_chunk DROP COLUMN buddy_fts")
        cr.execute("ALTER TABLE buddy_chunk ADD COLUMN buddy_fts tsvector GENERATED ALWAYS AS "
                   "(to_tsvector('spanish'::regconfig, coalesce(content, ''))) STORED")
        self.assertEqual(self.retrieve("manual"), [], "the old column ignores the title")
        self.env["buddy.chunk"].init()
        self.assertEqual(len(document.chunk_ids), 2)
        self.assertEqual({hit["sequence"] for hit in self.retrieve("manual")}, {0, 1})
        self.env["buddy.chunk"].init()
        self.assertEqual({hit["sequence"] for hit in self.retrieve("manual")}, {0, 1})


@tagged("post_install", "-at_install")
class TestNeighboursAndBudget(RetrievalCase):
    def setUp(self):
        super().setUp()
        self.filler = "Texto de relleno sin relación alguna. "
        self.doc = multi_document(self.env, self.source, "Procedimiento largo", [
            "Paso cero: preparación general.", "Paso uno: abrí la aplicación Tesorería.",
            "Paso dos: elegí la opción conciliar cuentas bancarias.", "Paso tres: confirmá los movimientos.",
            "Paso cuatro: archivá el comprobante."])

    def test_the_next_and_previous_chunks_come_with_the_best_one_in_reading_order(self):
        hits = self.retrieve("conciliar cuentas bancarias")
        self.assertEqual([hit["sequence"] for hit in hits], [1, 2, 3])
        self.assertEqual([hit["neighbor"] for hit in hits], [True, False, True])
        self.assertEqual(len({hit["id"] for hit in hits}), 3, "no duplicates")

    def test_a_chunk_at_the_edge_only_gets_the_neighbour_that_exists(self):
        self.assertEqual([hit["sequence"] for hit in self.retrieve("preparación general")], [0, 1])
        self.assertEqual([hit["sequence"] for hit in self.retrieve("archivá comprobante")], [3, 4])

    def test_neighbours_are_not_added_twice_when_they_are_results_themselves(self):
        hits = self.retrieve("aplicación Tesorería conciliar cuentas bancarias", top_k=6)
        self.assertEqual([hit["sequence"] for hit in hits], [0, 1, 2, 3])
        self.assertEqual([hit["neighbor"] for hit in hits], [True, False, False, True])

    def test_the_context_budget_is_respected_and_the_best_chunk_always_enters(self):
        big = "conciliatorio " + "x" * 3400
        doc = multi_document(self.env, self.source, "Gigante", ["Anterior " + "y" * 3000, big, "Siguiente " + "z" * 3000])
        hits = self.retrieve("conciliatorio", top_k=1)
        gigante = [hit for hit in hits if hit["doc_id"] == doc.id]
        total = sum(len(hit["text"]) for hit in hits)
        self.assertLessEqual(total, buddy_engine.CONTEXT_BUDGET)
        self.assertIn(big, [hit["text"] for hit in hits], "the matched chunk is never dropped")
        # 3400 + 3000 fits (6400); a third chunk would not (9400): exactly one neighbour came along.
        self.assertEqual([hit["sequence"] for hit in gigante], [1, 2], "the next chunk is preferred to the previous one")

    def test_a_small_top_k_budget_drops_extra_primary_chunks_not_the_first(self):
        for number in range(4):
            multi_document(self.env, self.source, "Larga %d" % number, ["tesorería " + "w" * 2900])
        hits = self.retrieve("tesorería", top_k=10)
        self.assertLessEqual(sum(len(hit["text"]) for hit in hits), buddy_engine.CONTEXT_BUDGET)
        self.assertGreaterEqual(len(hits), 2)

    def test_neighbours_are_read_with_the_user_rights(self):
        hidden = self.doc.chunk_ids.filtered(lambda chunk: chunk.sequence == 3)
        self.env["ir.rule"].create({"name": "QA hide one chunk", "model_id": self.env["ir.model"]._get("buddy.chunk").id,
                                    "domain_force": "[('id', '!=', %s)]" % hidden.id})
        hits = self.retrieve("conciliar cuentas bancarias")
        self.assertEqual([hit["sequence"] for hit in hits], [1, 2])
        self.assertNotIn(hidden.content, self.texts(hits))

    def test_neighbours_never_cross_into_another_users_folders(self):
        other = new_source(self.env, "Dirección", [new_group(self.env, "T Quality Dir")])
        multi_document(self.env, other, "Secreto", ["Primero.", "SECRETO-VECINO conciliar cuentas bancarias.", "Tercero SECRETO-TERCERO."])
        hits = self.retrieve("conciliar cuentas bancarias")
        self.assertTrue(all("SECRETO" not in hit["text"] for hit in hits))
        self.assertEqual({hit["title"] for hit in hits}, {"Procedimiento largo"})


@tagged("post_install", "-at_install")
class TestCatalog(RetrievalCase):
    def test_catalog_lists_only_what_the_user_can_read(self):
        secret = new_source(self.env, "Dirección", [new_group(self.env, "T Quality Dir 2")])
        new_document(self.env, secret, "Sueldos de gerentes", "Confidencial.")
        new_document(self.env, self.source, "Cómo cotizar.docx", "Abrí Ventas.")
        new_document(self.env, self.source, "Sin texto", "x", state="empty")
        catalog = self.env["buddy.engine"].with_user(self.ana)._catalog()
        self.assertEqual(catalog["folders"], ["Procedimientos"])
        self.assertEqual(catalog["titles"], ["Cómo cotizar"])
        self.assertEqual(catalog["total"], 1)

    def test_catalog_is_bounded_and_names_are_cleaned(self):
        for number in range(45):
            new_document(self.env, self.source, "Doc %02d %s.pdf" % (number, "n" * 100), "texto %d" % number)
        catalog = self.env["buddy.engine"].with_user(self.ana)._catalog()
        self.assertEqual(len(catalog["titles"]), 40)
        self.assertEqual(catalog["total"], 45)
        self.assertTrue(all(len(title) <= 80 for title in catalog["titles"]))
        self.assertFalse(any(title.endswith(".pdf") for title in catalog["titles"]))

    def test_a_user_without_folders_gets_an_empty_catalog(self):
        new_document(self.env, self.source, "Guía", "Texto.")
        leo = new_user(self.env, "t_quality_leo")
        self.assertEqual(self.env["buddy.engine"].with_user(leo)._catalog(), {"folders": [], "titles": [], "total": 0})


@tagged("post_install", "-at_install")
class TestTableOfContents(RetrievalCase):
    TEXT = ("PR 46 HelpDesk\nÍndice\nOBJETIVO\t2\nALCANCE\t2\n1) Plataforma\t4\n2) Carga de solicitudes\t6\n"
            "Revisiones\t12\nOBJETIVO\nDefinir un esquema de servicio ágil.\n\nPLATAFORMA\nSe utiliza Odoo.")

    def sync(self, text):
        client = FakeClient(files=[drive_file("toc1", "PR 46 HelpDesk V2.docx")], texts={"toc1": text})
        self.source._sync(client=client)
        return self.env["buddy.document"].search([("source_id", "=", self.source.id)]).chunk_ids

    def test_the_index_is_not_indexed_and_the_body_is(self):
        chunks = self.sync(self.TEXT)
        joined = " ".join(chunks.mapped("content"))
        self.assertNotIn("Índice", joined)
        self.assertNotIn("Revisiones 12", joined)
        self.assertIn("Definir un esquema de servicio ágil.", joined)
        hits = self.retrieve("Plataforma Revisiones")
        self.assertTrue(hits)
        self.assertTrue(all("Índice" not in hit["text"] for hit in hits))

    def test_a_document_without_an_index_is_indexed_whole(self):
        text = "Contenido\nProducto A 120\nProducto B 80\nProducto C 300\nProducto D 20\nTotal de la lista."
        self.assertIn("Producto D 20", " ".join(self.sync(text).mapped("content")))

    def test_upgrade_cleans_existing_chunks_and_fills_the_context(self):
        document = new_document(self.env, self.source, "PR 46 HelpDesk V2.docx",
                                "Índice OBJETIVO 2 ALCANCE 2 ROLES 2 DESARROLLO 4 Revisiones 12 OBJETIVO El objetivo es definir el servicio.")
        only_index = self.env["buddy.chunk"].create({
            "document_id": document.id, "sequence": 1, "content": "Índice OBJETIVO 2 ALCANCE 3 ROLES 4 FIN 5"})
        legit = self.env["buddy.chunk"].create({
            "document_id": document.id, "sequence": 5, "content": "Índice OBJETIVO 2 ALCANCE 2 ROLES 2 DESARROLLO 4 texto"})
        self.env.cr.execute("UPDATE buddy_chunk SET context_text = NULL WHERE document_id = %s", (document.id,))
        self.env.invalidate_all()
        self.env["buddy.chunk"]._rebuild_search_data()
        first = document.chunk_ids.filtered(lambda chunk: chunk.sequence == 0)
        self.assertEqual(first.content, "OBJETIVO El objetivo es definir el servicio.")
        self.assertFalse(only_index.exists(), "a chunk that was only an index is dropped")
        self.assertEqual(legit.content, "Índice OBJETIVO 2 ALCANCE 2 ROLES 2 DESARROLLO 4 texto", "only the start of the document is checked")
        self.assertEqual(set(document.chunk_ids.mapped("context_text")), {"PR 46 HelpDesk V2 · Procedimientos"})
        self.assertEqual([hit["sequence"] for hit in self.retrieve("helpdesk")][:1], [0])
        self.env["buddy.chunk"]._rebuild_search_data()  # idempotent
        self.assertEqual(first.content, "OBJETIVO El objetivo es definir el servicio.")

    def test_the_migration_script_runs_the_rebuild_and_moves_the_old_default(self):
        path = Path(__file__).resolve().parents[1] / "migrations" / "18.0.4.1.0" / "post-migration.py"
        spec = importlib.util.spec_from_file_location("buddy_post_migration_4_1_0", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        document = new_document(self.env, self.source, "Guía", "Texto de la guía.")
        self.env.cr.execute("UPDATE buddy_chunk SET context_text = NULL WHERE document_id = %s", (document.id,))
        self.params.set_param("buddy_ia.top_k", "4")
        module.migrate(self.env.cr, "18.0.4.0.0")
        self.env.invalidate_all()
        self.assertEqual(document.chunk_ids.context_text, "Guía · Procedimientos")
        self.assertEqual(self.params.get_param("buddy_ia.top_k"), "6")
        self.params.set_param("buddy_ia.top_k", "3")
        module.migrate(self.env.cr, "18.0.4.0.0")
        self.assertEqual(self.params.get_param("buddy_ia.top_k"), "3", "a value the administrator chose stays")

    def test_strip_is_what_indexing_uses(self):
        self.assertNotIn("Índice", strip_toc(self.TEXT))


@tagged("post_install", "-at_install")
class TestSynonymSetting(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.Settings = self.env["res.config.settings"]

    def test_round_trip_through_the_settings_screen(self):
        self.Settings.create({"buddy_synonyms": "vacaciones, licencia, días libres\n\nsueldo, haberes\n"}).execute()
        self.assertEqual(self.params.get_param("buddy_ia.synonyms"), "vacaciones, licencia, días libres\nsueldo, haberes")
        self.assertEqual(self.Settings.create({}).buddy_synonyms, "vacaciones, licencia, días libres\nsueldo, haberes")
        self.Settings.create({"buddy_synonyms": ""}).execute()
        self.assertFalse(self.params.get_param("buddy_ia.synonyms"))

    def test_invalid_synonyms_are_rejected_with_a_readable_message(self):
        from odoo.exceptions import ValidationError
        for invalid in ("ticket & caso, otra", "sola", "x" * 2001, "\n".join("a%d, b%d" % (n, n) for n in range(41))):
            with self.subTest(invalid=invalid[:20]), self.assertRaises(ValidationError):
                self.Settings.create({"buddy_synonyms": invalid})

    def test_the_engine_merges_defaults_and_company_groups_and_survives_garbage_in_the_parameter(self):
        self.params.set_param("buddy_ia.api_key", "not-a-real-key")
        self.params.set_param("buddy_ia.chat_model", "qa-chat")
        self.params.set_param("buddy_ia.synonyms", "vacaciones, licencia\nbasura & | !\n")
        cfg = self.env["buddy.engine"]._cfg()
        self.assertEqual(cfg["synonyms"], all_synonyms("vacaciones, licencia"))
        self.assertEqual(cfg["top_k"], 6, "the default moved from 4 to 6")
        self.params.set_param("buddy_ia.synonyms", False)
        self.assertEqual(self.Settings.create({}).buddy_top_k, 6)


@tagged("post_install", "-at_install")
class TestTyposAgainstTheTextOfTheDocuments(RetrievalCase):
    """A typo is fixed against the words the documents really use, not only against titles, folders and synonyms."""

    def setUp(self):
        super().setUp()
        buddy_engine._CONTENT_VOCABULARIES.clear()
        self.addCleanup(buddy_engine._CONTENT_VOCABULARIES.clear)
        self.quotes = new_document(self.env, self.source, "Guía comercial", (
            "Las cotizaciones se aprueban antes de enviarlas al cliente. Una cotización vencida se vuelve a emitir con el precio vigente."))
        new_document(self.env, self.source, "Otra guía", "Los reembolsos de gastos se piden con el comprobante original adjunto.")

    def engine(self, user=None):
        return self.env["buddy.engine"].with_user(user or self.ana)

    def sources(self, user=None):
        return self.env["buddy.source"].with_user(user or self.ana).search([("active", "=", True)])

    def test_the_vocabulary_has_the_words_of_the_text_folded_and_without_short_ones(self):
        words = self.engine()._content_vocabulary(self.sources())
        for expected in ("cotizaciones", "cotizacion", "reembolsos", "comprobante", "vigente"):
            self.assertIn(expected, words)
        self.assertNotIn("cotización", words, "accents are folded")
        self.assertFalse([word for word in words if len(word) < 4 or not word.isalpha()])

    def test_a_word_in_no_document_is_a_probable_typo_and_a_known_one_is_not(self):
        engine, sources, cfg = self.engine(), self.sources(), {"synonyms": all_synonyms("")}
        self.assertTrue(engine._has_unknown_word("como apruebo una cotisación", sources, cfg))
        self.assertTrue(engine._has_unknown_word("como hago los bakups", sources, cfg), "backups is a synonym: bakups is neither")
        self.assertFalse(engine._has_unknown_word("como se aprueban las cotizaciones", sources, cfg))
        self.assertFalse(engine._has_unknown_word("las cotizaciones vencidas", sources, cfg), "plurals and other forms are the same word")
        self.assertFalse(engine._has_unknown_word("hola que tal", sources, cfg), "no long word: nothing to look at")

    def test_the_typo_is_fixed_even_when_the_first_search_was_not_weak(self):
        # "apruebo" and "enviarlas" match (a decent first search) but "cotisación" matches nothing: it is fixed anyway.
        hits = self.retrieve("¿cómo apruebo la cotisación antes de enviarla?")
        self.assertIn(self.quotes.id, {hit["doc_id"] for hit in hits if not hit["neighbor"]})
        self.assertIn("cotizaciones", " ".join(self.texts(hits)))

    def test_only_the_text_the_user_may_read_teaches_the_corrector(self):
        secret_group = new_group(self.env, "T Secret")
        secret = new_source(self.env, "Reservado", [secret_group])
        new_document(self.env, secret, "Acta", "El presupuesto confidencial incluye indemnizaciones extraordinarias.")
        engine = self.engine()
        self.assertNotIn("indemnizaciones", engine._content_vocabulary(self.sources()))
        director = new_user(self.env, "t_quality_dir", extra_groups=[self.group, secret_group])
        self.assertIn("indemnizaciones", self.engine(director)._content_vocabulary(self.sources(director)))
        self.assertFalse(self.retrieve("indemnisaciones"), "a typo of a word only a restricted folder has is not fixed for this person")

    def test_the_cache_follows_the_chunks_and_stays_small(self):
        engine, sources = self.engine(), self.sources()
        first = engine._content_vocabulary(sources)
        self.assertIs(engine._content_vocabulary(sources), first, "same chunks: the cached set")
        new_document(self.env, self.source, "Nueva", "La vacunación obligatoria se informa por correo interno.")
        refreshed = engine._content_vocabulary(sources)
        self.assertIn("vacunacion", refreshed)
        self.assertNotIn("vacunacion", first)
        with patch.object(buddy_engine, "CONTENT_VOCABULARY_CACHE", 2):
            for number in range(4):  # every new document changes the folder's chunks: a new entry each time
                new_document(self.env, self.source, "Extra %d" % number, "Texto adicional número %d de la carpeta." % number)
                engine._content_vocabulary(sources)
            self.assertLessEqual(len(buddy_engine._CONTENT_VOCABULARIES), 2)

    def test_no_folders_no_words_and_a_failing_query_is_not_fatal(self):
        engine = self.engine()
        self.assertEqual(engine._content_vocabulary(self.env["buddy.source"]), frozenset())
        with patch.object(buddy_engine, "content_words", side_effect=RuntimeError("boom")):
            self.assertEqual(engine._content_vocabulary(self.sources()), frozenset())
