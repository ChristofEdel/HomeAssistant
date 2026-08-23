-- List the latest 50 states in the database

SELECT
    s.state_id,
    s.old_state_id,
    s.state, 
    datetime(s.last_reported_ts, 'unixepoch', 'localtime') AS last_reported,
    datetime(s.last_updated_ts, 'unixepoch', 'localtime') AS last_updated,
    s.attributes_id,
    s.context_id_bin
FROM states s
JOIN states_meta sm
  ON s.metadata_id = sm.metadata_id
WHERE sm.entity_id = 'sensor.backup_size_rainbow_one_gb'
ORDER BY s.last_updated_ts DESC
LIMIT 50;

BEGIN IMMEDIATE;

-- Delete some specific states
-- NOTE - it may also work to set the old_state_id to NULL where it points to one of the deleted states

UPDATE states
SET old_state_id = 36736836
WHERE old_state_id = 36740692;

DELETE FROM states
WHERE state_id IN (36740692, 36740613, 36740488);

COMMIT;