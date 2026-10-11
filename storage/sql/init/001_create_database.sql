-- FALCON database initialization (FALCON-007).
-- Creates the development database. Table schemas are added in FALCON-038.
-- Idempotent: safe to run repeatedly.

IF DB_ID('Falcon') IS NULL
BEGIN
    CREATE DATABASE Falcon;
END
GO
