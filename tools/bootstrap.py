"""Run through Odoo shell; credentials arrive through the local environment."""
import os
import re


def bootstrap(environment):
    expected = os.environ.get("BUDDY_BOOTSTRAP_DATABASE")
    if not expected or environment.cr.dbname != expected:
        raise ValueError("Bootstrap database did not match the selected installation.")
    password = os.environ.get("BUDDY_ADMIN_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise ValueError("A generated administrator password is required; no default password is accepted.")
    admin = environment.ref("base.user_admin")
    admin.with_context(no_reset_password=True).write({"login": "admin", "password": password, "active": True})
    # Odoo can leave public self-registration open ("b2c"); an internal-docs tool should be invitation-only.
    environment["ir.config_parameter"].sudo().set_param("auth_signup.invitation_scope", "b2b")
    environment.cr.commit()
    print("Administrator bootstrap completed; credentials were not printed.")


if __name__ == "__main__":
    if "env" not in globals():
        raise SystemExit("Run this file through tools/manage.py or Odoo shell.")
    bootstrap(env)
