-- Day Prep support migration.
--
-- Run once after 20260801_align_live_schema_contracts.sql. This migration:
--   1. aligns users.access_token with the live 255-character broker token;
--   2. makes optional auditlog archival idempotent by live ID + timestamp.

DROP PROCEDURE IF EXISTS `_assert_day_prep_schema_ready`;
DELIMITER //
CREATE PROCEDURE `_assert_day_prep_schema_ready`()
BEGIN
    DECLARE v_count BIGINT DEFAULT 0;

    SELECT COUNT(*) INTO v_count
    FROM (
        SELECT auditlog_id, ts
        FROM auditlog_history
        WHERE auditlog_id IS NOT NULL
        GROUP BY auditlog_id, ts
        HAVING COUNT(*) > 1
    ) AS duplicates;

    IF v_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'auditlog_history contains duplicate auditlog_id/ts rows';
    END IF;
END//
DELIMITER ;

CALL `_assert_day_prep_schema_ready`();
DROP PROCEDURE `_assert_day_prep_schema_ready`;

UPDATE users
SET access_token = ''
WHERE access_token IS NULL;

ALTER TABLE users
    MODIFY COLUMN access_token VARCHAR(255) NOT NULL DEFAULT '';

ALTER TABLE auditlog_history
    ADD UNIQUE KEY uq_auditlog_history_live_ts (auditlog_id, ts);

SELECT
    table_name,
    index_name,
    non_unique,
    GROUP_CONCAT(column_name ORDER BY seq_in_index) AS indexed_columns
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = 'auditlog_history'
GROUP BY table_name, index_name, non_unique
ORDER BY index_name;

SHOW COLUMNS FROM users WHERE Field = 'access_token';
