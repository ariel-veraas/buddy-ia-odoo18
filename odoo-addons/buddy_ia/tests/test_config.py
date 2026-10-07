from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBuddyConfigSecurity(TransactionCase):
    def test_employee_cannot_call_diagnostic_on_empty_recordset(self):
        employee = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Empleado diagnóstico", "login": "buddy_diag_test",
            "groups_id": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        cls = self.env.registry["buddy.engine"]
        with patch.object(cls, "_self_test") as diagnostic:
            with self.assertRaises(AccessError):
                self.env["res.config.settings"].with_user(employee).browse().action_buddy_test_connection()
            diagnostic.assert_not_called()
