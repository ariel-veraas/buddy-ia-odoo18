# NOTA: este test usa el framework de Odoo y NO se pudo ejecutar en el entorno donde se generó el módulo.
# Correr con: docker compose run --rm odoo odoo -d buddy_test -i buddy_ia --test-tags /buddy_ia --stop-after-init
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError, ValidationError
from uuid import uuid4


@tagged("post_install", "-at_install")
class TestBuddyTicket(TransactionCase):
    def setUp(self):
        super().setUp()
        group = self.env.ref("base.group_user")
        Users = self.env["res.users"].with_context(no_reset_password=True)
        self.ana = Users.create({"name": "Ana", "login": "ana_buddy", "groups_id": [(6, 0, [group.id])]})
        self.leo = Users.create({"name": "Leo", "login": "leo_buddy", "groups_id": [(6, 0, [group.id])]})

    def test_each_user_sees_only_own_tickets(self):
        self.env["buddy.ticket"].with_user(self.ana).create({"name": "De Ana"})
        self.env["buddy.ticket"].with_user(self.leo).create({"name": "De Leo"})
        visibles = self.env["buddy.ticket"].with_user(self.ana).search([])
        self.assertEqual(visibles.mapped("name"), ["De Ana"])

    def test_default_state_is_new(self):
        ticket = self.env["buddy.ticket"].with_user(self.ana).create({"name": "X"})
        self.assertEqual(ticket.state, "new")

    def test_new_keys_are_per_record_without_legacy_column_default(self):
        Ticket = self.env["buddy.ticket"].with_user(self.ana)
        self.assertFalse(Ticket._fields["request_key"].default)
        records = Ticket.create([{"name": "Primero"}, {"name": "Segundo"}])
        self.assertEqual(len(set(records.mapped("request_key"))), 2)

    def test_employee_cannot_create_resolved_or_other_users_ticket(self):
        Ticket = self.env["buddy.ticket"].with_user(self.ana)
        with self.assertRaises(AccessError):
            Ticket.create({"name": "X", "state": "done"})
        with self.assertRaises(AccessError):
            Ticket.create({"name": "X", "user_id": self.leo.id})

    def test_request_key_is_idempotent_for_same_payload(self):
        Ticket = self.env["buddy.ticket"].with_user(self.ana)
        vals = {"name": "X", "description": "Detalle", "request_key": str(uuid4())}
        first, second = Ticket.create(vals), Ticket.create(vals)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Ticket.search_count([("request_key", "=", vals["request_key"])]), 1)
        with self.assertRaises(ValidationError):
            Ticket.create(dict(vals, name="Otro asunto"))

    def test_direct_rpc_uses_same_content_limits(self):
        Ticket = self.env["buddy.ticket"].with_user(self.ana)
        for vals in ({"name": " "}, {"name": "x" * 121}, {"name": "X", "description": "x" * 2001},
                     {"name": "X", "request_key": "invalid"}):
            with self.assertRaises(ValidationError):
                Ticket.create(vals)

    def test_daily_cap_applies_to_direct_orm(self):
        self.env["ir.config_parameter"].sudo().set_param("buddy_ia.tickets_per_day", "2")
        Ticket = self.env["buddy.ticket"].with_user(self.ana)
        Ticket.create({"name": "Uno"})
        Ticket.create({"name": "Dos"})
        with self.assertRaises(ValidationError):
            Ticket.create({"name": "Tres"})
        # el tope es por usuario: Leo no queda bloqueado
        self.env["buddy.ticket"].with_user(self.leo).create({"name": "De Leo"})
