-- Neutralization (odoo-bin neutralize / «Duplicate Database» with neutralize): a copy of the database made for tests or
-- staging must not keep talking to the AI provider or to Google Drive, nor start sending the documents' text anywhere:
-- the keys and the switches of the search by meaning and of the stage line (the name of an onboarding journey goes to the
-- provider with it) are removed, and have to be loaded again on purpose.
DELETE FROM ir_config_parameter
 WHERE key IN ('buddy_ia.api_key', 'buddy_ia.drive_key', 'buddy_ia.semantic_search', 'buddy_ia.semantic_state',
               'buddy_ia.journey_stage');
