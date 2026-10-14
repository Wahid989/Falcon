-- FALCON database initialization (FALCON-007).
-- Creates the database. Table schemas are added in FALCON-038.
-- Idempotent: safe to run repeatedly.
--
-- $(FalconDb) is the database name: a sqlcmd variable (scripts/init-db.sh passes
-- -v FalconDb=Falcon), substituted the same way by the storage service.

IF DB_ID(N'$(FalconDb)') IS NULL
BEGIN
    CREATE DATABASE [$(FalconDb)];
END
GO
