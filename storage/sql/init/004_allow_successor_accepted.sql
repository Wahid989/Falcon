-- Allow the SUCCESSOR_ACCEPTED role outcome in a database created before it
-- existed.
--
-- A fresh database already gets it from 002_create_exchange_schema.sql; this
-- script only replaces the two RoleHandovers checks that list the outcomes when
-- they don't mention it yet. Idempotent: safe to run repeatedly.
-- $(FalconDb) is the database name (see 001_create_database.sql).

USE [$(FalconDb)];
GO

IF EXISTS (SELECT 1 FROM sys.check_constraints
           WHERE name = N'CK_RoleHandovers_Outcome'
             AND parent_object_id = OBJECT_ID(N'dbo.RoleHandovers')
             AND definition NOT LIKE N'%SUCCESSOR_ACCEPTED%')
BEGIN
    ALTER TABLE dbo.RoleHandovers DROP CONSTRAINT CK_RoleHandovers_Outcome;
    ALTER TABLE dbo.RoleHandovers ADD CONSTRAINT CK_RoleHandovers_Outcome CHECK (Outcome IN (
        'GRANTED', 'DENIED', 'SELF_PROMOTED',
        'STEPPED_DOWN', 'PROMOTION_DECLINED', 'VACANT',
        'SUCCESSOR_ACCEPTED'));
END
GO

IF EXISTS (SELECT 1 FROM sys.check_constraints
           WHERE name = N'CK_RoleHandovers_EpochChange'
             AND parent_object_id = OBJECT_ID(N'dbo.RoleHandovers')
             AND definition NOT LIKE N'%SUCCESSOR_ACCEPTED%')
BEGIN
    ALTER TABLE dbo.RoleHandovers DROP CONSTRAINT CK_RoleHandovers_EpochChange;
    ALTER TABLE dbo.RoleHandovers ADD CONSTRAINT CK_RoleHandovers_EpochChange CHECK (
        (Outcome IN ('GRANTED', 'SELF_PROMOTED', 'STEPPED_DOWN',
                     'SUCCESSOR_ACCEPTED')
            AND NewEpoch IS NOT NULL AND NewEpoch > OldEpoch)
        OR (Outcome IN ('DENIED', 'PROMOTION_DECLINED', 'VACANT')
            AND (NewEpoch IS NULL OR NewEpoch = OldEpoch))
    );
END
GO
