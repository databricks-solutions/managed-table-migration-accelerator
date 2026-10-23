-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Team priorities: the ordered pin list (focus_pins JSON, each pin with its status) and the pinned
-- scope that chose the latest recommendation, from the LATEST migration_next_actions row. Read
-- separately from migration_next_actions.sql so a deploy before the first conductor run (columns
-- not yet added) only degrades this panel. The app never reads the pin event log itself.
SELECT
  as_of,
  focus_pins,
  pin_applied_scope
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND as_of = (SELECT MAX(as_of) FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions'))
