from . import controllers
from . import models

# Secrets stored as system parameters. They are erased on uninstall: a removed module must not leave an API key or a
# Google service-account key behind in the database (backups, copies for testing).
SECRET_PARAMETERS = ("buddy_ia.api_key", "buddy_ia.drive_key")
# The switch of the search by meaning is erased too: a copy of the database (or a reinstall) with a new API key must not
# start sending the documents' text to a provider without anybody turning it on again.
UNINSTALL_PARAMETERS = SECRET_PARAMETERS + ("buddy_ia.semantic_search", "buddy_ia.semantic_state", "buddy_ia.journey_stage")


def uninstall_hook(env):
    env["ir.config_parameter"].sudo().search([("key", "in", list(UNINSTALL_PARAMETERS))]).unlink()
