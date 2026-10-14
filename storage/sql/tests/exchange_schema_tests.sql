-- Schema tests for the exchange-record tables (002_create_exchange_schema.sql).
--
-- Runs inside one transaction that is always rolled back, so it leaves no data
-- behind. Any failed check raises an error; with sqlcmd -b that becomes a
-- non-zero exit code. Run with scripts/test-db.sh.

USE Falcon;
GO

SET NOCOUNT ON;
SET XACT_ABORT OFF;
GO

-- Runs a statement that must be rejected by a constraint. Errors 50000+ are the
-- test's own failures and are re-raised; anything else is the expected
-- rejection.
CREATE OR ALTER PROCEDURE #ExpectRejected
    @Name NVARCHAR(200),
    @Sql  NVARCHAR(MAX)
AS
BEGIN
    BEGIN TRY
        EXEC sp_executesql @Sql;
        DECLARE @Msg NVARCHAR(400) = CONCAT(N'FAIL: expected rejection: ', @Name);
        THROW 50001, @Msg, 1;
    END TRY
    BEGIN CATCH
        IF ERROR_NUMBER() >= 50000 THROW;
        PRINT CONCAT(N'ok   rejected: ', @Name, N' (error ', ERROR_NUMBER(), N')');
    END CATCH
END
GO

BEGIN TRANSACTION;
GO

-- 1. Tables and hierarchy_level columns exist -------------------------------
DECLARE @Missing NVARCHAR(400) = (
    SELECT STRING_AGG(Expected.Name, N', ')
    FROM (VALUES (N'Messages'), (N'StateSnapshots'), (N'RoleHandovers')) AS Expected (Name)
    WHERE COL_LENGTH(N'dbo.' + Expected.Name, N'HierarchyLevel') IS NULL
);
IF OBJECT_ID(N'dbo.Sessions', N'U') IS NULL
    THROW 50002, N'FAIL: dbo.Sessions is missing', 1;
IF @Missing IS NOT NULL
BEGIN
    SET @Missing = CONCAT(N'FAIL: HierarchyLevel column missing on: ', @Missing);
    THROW 50003, @Missing, 1;
END
PRINT N'ok   tables and HierarchyLevel columns exist';
GO

-- 2. hierarchy_level indexes exist ------------------------------------------
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes AS i
    JOIN sys.index_columns AS ic
        ON ic.object_id = i.object_id AND ic.index_id = i.index_id
    JOIN sys.columns AS c
        ON c.object_id = ic.object_id AND c.column_id = ic.column_id
    WHERE i.object_id = OBJECT_ID(N'dbo.Messages')
      AND c.name = N'HierarchyLevel'
      AND ic.key_ordinal = 1
)
    THROW 50004, N'FAIL: no index leads with Messages.HierarchyLevel', 1;
IF INDEXPROPERTY(OBJECT_ID(N'dbo.StateSnapshots'), N'IX_StateSnapshots_HierarchyLevel', N'IndexID') IS NULL
    THROW 50005, N'FAIL: IX_StateSnapshots_HierarchyLevel missing', 1;
IF INDEXPROPERTY(OBJECT_ID(N'dbo.RoleHandovers'), N'IX_RoleHandovers_HierarchyLevel', N'IndexID') IS NULL
    THROW 50006, N'FAIL: IX_RoleHandovers_HierarchyLevel missing', 1;
PRINT N'ok   hierarchy_level indexes exist';
GO

-- 3. Valid rows are accepted ------------------------------------------------
INSERT INTO dbo.Sessions (SessionId, StartedAt) VALUES (N'test-session', SYSUTCDATETIME());

INSERT INTO dbo.Messages
    (SessionId, MessageId, Timestamp, LoggedBy, SenderId, ReceiverId, Direction,
     MessageType, SenderRole, RoleEpoch, SchemaVersion, HierarchyLevel, Payload, SourceHash)
VALUES
    (N'test-session', N'm-1', SYSUTCDATETIME(), N'cmd-1', N'cmd-1', N'fol-1', 'SENT',
     'HOLD', 'COMMANDER', 1, '1.0', 0, N'{}', HASHBYTES('SHA2_256', 'line-1')),
    -- Same message seen by the receiver: allowed, different source line.
    (N'test-session', N'm-1', SYSUTCDATETIME(), N'fol-1', N'cmd-1', N'fol-1', 'RECEIVED',
     'HOLD', 'COMMANDER', 1, '1.0', 0, N'{}', HASHBYTES('SHA2_256', 'line-2')),
    -- Unknown sender: hierarchy level is null.
    (N'test-session', N'm-2', SYSUTCDATETIME(), N'fol-1', N'stranger', N'fol-1', 'RECEIVED',
     'HOLD', NULL, 1, '1.0', NULL, NULL, HASHBYTES('SHA2_256', 'line-3'));

INSERT INTO dbo.StateSnapshots
    (SessionId, AgentId, Timestamp, LoggedBy, RoleEpoch, HierarchyLevel, State, SourceHash)
VALUES
    (N'test-session', N'fol-1', SYSUTCDATETIME(), N'fol-1', 1, 1,
     N'{"battery": 80}', HASHBYTES('SHA2_256', 'line-4'));

INSERT INTO dbo.RoleHandovers
    (SessionId, Timestamp, LoggedBy, RequesterId, OldEpoch, NewEpoch, Outcome, Reason,
     HierarchyLevel, SourceHash)
VALUES
    (N'test-session', SYSUTCDATETIME(), N'cmd-1', N'fol-1', 1, 2, 'GRANTED', NULL, 0,
     HASHBYTES('SHA2_256', 'line-5')),
    (N'test-session', SYSUTCDATETIME(), N'cmd-1', N'fol-2', 2, NULL, 'DENIED', 'BUSY', 0,
     HASHBYTES('SHA2_256', 'line-6')),
    (N'test-session', SYSUTCDATETIME(), N'fol-1', N'fol-1', 2, 3, 'SELF_PROMOTED', NULL, 0,
     HASHBYTES('SHA2_256', 'line-7')),
    -- Revived commander adopts the newer epoch and rejoins as a follower.
    (N'test-session', SYSUTCDATETIME(), N'cmd-1', N'cmd-1', 1, 3, 'STEPPED_DOWN', 'STALE_EPOCH', 1,
     HASHBYTES('SHA2_256', 'line-8')),
    (N'test-session', SYSUTCDATETIME(), N'fol-2', N'fol-2', 3, NULL, 'PROMOTION_DECLINED',
     'BATTERY_CRITICAL', 1, HASHBYTES('SHA2_256', 'line-9')),
    (N'test-session', SYSUTCDATETIME(), N'fol-3', N'fol-3', 3, 3, 'VACANT', NULL, 1,
     HASHBYTES('SHA2_256', 'line-10'));

IF (SELECT COUNT(*) FROM dbo.RoleHandovers WHERE SessionId = N'test-session') <> 6
    THROW 50010, N'FAIL: valid role handover rows were not all stored', 1;

IF (SELECT COUNT(*) FROM dbo.Messages WHERE SessionId = N'test-session') <> 3
    THROW 50007, N'FAIL: valid message rows were not all stored', 1;
PRINT N'ok   valid rows accepted';
GO

-- 4. Invalid rows are rejected ----------------------------------------------
DECLARE @Msg NVARCHAR(400) = N'INSERT INTO dbo.Messages (SessionId, MessageId, '
    + N'Timestamp, LoggedBy, SenderId, ReceiverId, Direction, MessageType, RoleEpoch, '
    + N'HierarchyLevel, Payload, SourceHash) VALUES ';
DECLARE @Role NVARCHAR(400) = N'INSERT INTO dbo.RoleHandovers (SessionId, Timestamp, '
    + N'LoggedBy, RequesterId, OldEpoch, NewEpoch, Outcome, Reason, SourceHash) VALUES ';

DECLARE @Cases TABLE (Seq INT IDENTITY, Name NVARCHAR(200), Sql NVARCHAR(MAX));
INSERT INTO @Cases (Name, Sql) VALUES
    (N'message for an unknown session (foreign key)', @Msg
        + N'(N''no-such-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SENT'', ''HOLD'', 1, 0, NULL, HASHBYTES(''SHA2_256'', ''bad-1''))'),
    (N'invalid direction', @Msg
        + N'(N''test-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SIDEWAYS'', ''HOLD'', 1, 0, NULL, HASHBYTES(''SHA2_256'', ''bad-2''))'),
    (N'payload that is not JSON', @Msg
        + N'(N''test-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SENT'', ''HOLD'', 1, 0, N''not json'', HASHBYTES(''SHA2_256'', ''bad-3''))'),
    (N'negative hierarchy level', @Msg
        + N'(N''test-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SENT'', ''HOLD'', 1, -1, NULL, HASHBYTES(''SHA2_256'', ''bad-4''))'),
    (N'negative role epoch', @Msg
        + N'(N''test-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SENT'', ''HOLD'', -1, 0, NULL, HASHBYTES(''SHA2_256'', ''bad-5''))'),
    (N'duplicate source line (idempotency key)', @Msg
        + N'(N''test-session'', N''x'', SYSUTCDATETIME(), N''a'', N''a'', N''b'', '
        + N'''SENT'', ''HOLD'', 1, 0, NULL, HASHBYTES(''SHA2_256'', ''line-1''))'),
    (N'state snapshot that is not JSON',
        N'INSERT INTO dbo.StateSnapshots (SessionId, AgentId, Timestamp, LoggedBy, State, '
        + N'SourceHash) VALUES (N''test-session'', N''a'', SYSUTCDATETIME(), N''a'', '
        + N'N''{broken'', HASHBYTES(''SHA2_256'', ''bad-6''))'),
    (N'grant that does not raise the epoch', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''b'', 2, 2, '
        + N'''GRANTED'', NULL, HASHBYTES(''SHA2_256'', ''bad-7''))'),
    (N'self-promotion without a new epoch', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''b'', 2, NULL, '
        + N'''SELF_PROMOTED'', NULL, HASHBYTES(''SHA2_256'', ''bad-8''))'),
    (N'denial that changes the epoch', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''b'', 2, 3, '
        + N'''DENIED'', ''BUSY'', HASHBYTES(''SHA2_256'', ''bad-9''))'),
    (N'denial without a reason', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''b'', 2, NULL, '
        + N'''DENIED'', NULL, HASHBYTES(''SHA2_256'', ''bad-10''))'),
    (N'unknown handover outcome', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''b'', 2, 3, '
        + N'''VOTED'', NULL, HASHBYTES(''SHA2_256'', ''bad-11''))'),
    (N'step-down that does not adopt a newer epoch', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''a'', 3, 3, '
        + N'''STEPPED_DOWN'', NULL, HASHBYTES(''SHA2_256'', ''bad-12''))'),
    (N'declined promotion without a reason', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''a'', 3, NULL, '
        + N'''PROMOTION_DECLINED'', NULL, HASHBYTES(''SHA2_256'', ''bad-13''))'),
    (N'vacancy that changes the epoch', @Role
        + N'(N''test-session'', SYSUTCDATETIME(), N''a'', N''a'', 3, 4, '
        + N'''VACANT'', NULL, HASHBYTES(''SHA2_256'', ''bad-14''))'),
    (N'session that ends before it starts',
        N'INSERT INTO dbo.Sessions (SessionId, StartedAt, EndedAt) VALUES (N''bad-session'', '
        + N'''2026-01-02'', ''2026-01-01'')');

DECLARE @Seq INT = 1, @Name NVARCHAR(200), @Sql NVARCHAR(MAX);
WHILE @Seq <= (SELECT MAX(Seq) FROM @Cases)
BEGIN
    SELECT @Name = Name, @Sql = Sql FROM @Cases WHERE Seq = @Seq;
    EXEC #ExpectRejected @Name, @Sql;
    SET @Seq += 1;
END
GO

-- 5. hierarchy_level filtering uses the stored values ------------------------
IF (SELECT COUNT(*) FROM dbo.Messages
    WHERE SessionId = N'test-session' AND HierarchyLevel = 0) <> 2
    THROW 50008, N'FAIL: hierarchy_level = 0 filter returned the wrong rows', 1;
IF (SELECT COUNT(*) FROM dbo.Messages
    WHERE SessionId = N'test-session' AND HierarchyLevel IS NULL) <> 1
    THROW 50009, N'FAIL: unknown-sender rows not stored with a null level', 1;
PRINT N'ok   hierarchy_level filtering';
GO

ROLLBACK TRANSACTION;
GO

PRINT N'All exchange schema tests passed.';
GO
