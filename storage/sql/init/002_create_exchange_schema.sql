-- FALCON exchange-record schema (FALCON-038).
--
-- Tables for the full exchange record: Sessions, Messages, StateSnapshots and
-- RoleHandovers. Rows are loaded from the agents' JSONL exchange logs by the
-- .NET storage service; agents never write to SQL Server directly.
--
-- hierarchy_level is a log-only value (distance from the current top, 0 = top).
-- It is stored here because it is in the log, never because it is on the wire.
-- It is nullable: a received message from a sender the agent cannot place in
-- the hierarchy is logged with a null level.
--
-- SourceHash identifies the originating log line: a SHA-256 over the line's
-- file, its position in that file and its content. Its unique index is what
-- makes ingestion idempotent: re-reading a file, or a rotated copy of it, can
-- never create a second row for the same line, while two identical lines at
-- different positions (a genuinely repeated event) stay two rows.
--
-- Idempotent: safe to run repeatedly. $(FalconDb) is the database name (see
-- 001_create_database.sql).

USE [$(FalconDb)];
GO

IF OBJECT_ID(N'dbo.Sessions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Sessions (
        SessionId   NVARCHAR(64)  NOT NULL,
        StartedAt   DATETIME2(6)  NULL,
        EndedAt     DATETIME2(6)  NULL,
        Description NVARCHAR(256) NULL,
        CreatedAt   DATETIME2(6)  NOT NULL
            CONSTRAINT DF_Sessions_CreatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_Sessions PRIMARY KEY (SessionId),
        CONSTRAINT CK_Sessions_TimeOrder
            CHECK (StartedAt IS NULL OR EndedAt IS NULL OR EndedAt >= StartedAt)
    );
END
GO

-- One row per wire message per logging agent. The same message normally
-- appears twice (SENT in the sender's log, RECEIVED in the receiver's log), and
-- a re-sent duplicate is a real wire event, so MessageId is not unique.
IF OBJECT_ID(N'dbo.Messages', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Messages (
        Id             BIGINT IDENTITY(1, 1) NOT NULL,
        SessionId      NVARCHAR(64)  NOT NULL,
        MessageId      NVARCHAR(64)  NOT NULL,
        Timestamp      DATETIME2(6)  NOT NULL,
        LoggedAt       DATETIME2(6)  NULL,
        LoggedBy       NVARCHAR(64)  NOT NULL,
        SenderId       NVARCHAR(64)  NOT NULL,
        ReceiverId     NVARCHAR(64)  NOT NULL,
        Direction      VARCHAR(8)    NOT NULL,
        MessageType    VARCHAR(32)   NOT NULL,
        SenderRole     VARCHAR(16)   NULL,
        RoleEpoch      INT           NOT NULL,
        SchemaVersion  VARCHAR(16)   NULL,
        HierarchyLevel INT           NULL,
        Payload        NVARCHAR(MAX) NULL,
        SourceHash     BINARY(32)    NOT NULL,
        IngestedAt     DATETIME2(6)  NOT NULL
            CONSTRAINT DF_Messages_IngestedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_Messages PRIMARY KEY (Id),
        CONSTRAINT FK_Messages_Sessions
            FOREIGN KEY (SessionId) REFERENCES dbo.Sessions (SessionId),
        CONSTRAINT CK_Messages_Direction CHECK (Direction IN ('SENT', 'RECEIVED')),
        CONSTRAINT CK_Messages_RoleEpoch CHECK (RoleEpoch >= 0),
        CONSTRAINT CK_Messages_HierarchyLevel
            CHECK (HierarchyLevel IS NULL OR HierarchyLevel >= 0),
        CONSTRAINT CK_Messages_PayloadJson
            CHECK (Payload IS NULL OR ISJSON(Payload) = 1)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'UX_Messages_SourceHash'
                 AND object_id = OBJECT_ID(N'dbo.Messages'))
    CREATE UNIQUE INDEX UX_Messages_SourceHash ON dbo.Messages (SourceHash);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_Messages_HierarchyLevel'
                 AND object_id = OBJECT_ID(N'dbo.Messages'))
    CREATE INDEX IX_Messages_HierarchyLevel
        ON dbo.Messages (HierarchyLevel, Timestamp)
        INCLUDE (SessionId, MessageType);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_Messages_Session_Timestamp'
                 AND object_id = OBJECT_ID(N'dbo.Messages'))
    CREATE INDEX IX_Messages_Session_Timestamp
        ON dbo.Messages (SessionId, Timestamp);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_Messages_MessageId'
                 AND object_id = OBJECT_ID(N'dbo.Messages'))
    CREATE INDEX IX_Messages_MessageId ON dbo.Messages (MessageId);
GO

-- A point-in-time copy of one agent's simulated state, stored as JSON so the
-- state model can grow without a schema change.
IF OBJECT_ID(N'dbo.StateSnapshots', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.StateSnapshots (
        Id               BIGINT IDENTITY(1, 1) NOT NULL,
        SessionId        NVARCHAR(64)  NOT NULL,
        AgentId          NVARCHAR(64)  NOT NULL,
        Timestamp        DATETIME2(6)  NOT NULL,
        LoggedBy         NVARCHAR(64)  NOT NULL,
        RoleEpoch        INT           NULL,
        HierarchyLevel   INT           NULL,
        TriggerMessageId NVARCHAR(64)  NULL,
        State            NVARCHAR(MAX) NOT NULL,
        SourceHash       BINARY(32)    NOT NULL,
        IngestedAt       DATETIME2(6)  NOT NULL
            CONSTRAINT DF_StateSnapshots_IngestedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_StateSnapshots PRIMARY KEY (Id),
        CONSTRAINT FK_StateSnapshots_Sessions
            FOREIGN KEY (SessionId) REFERENCES dbo.Sessions (SessionId),
        CONSTRAINT CK_StateSnapshots_RoleEpoch
            CHECK (RoleEpoch IS NULL OR RoleEpoch >= 0),
        CONSTRAINT CK_StateSnapshots_HierarchyLevel
            CHECK (HierarchyLevel IS NULL OR HierarchyLevel >= 0),
        CONSTRAINT CK_StateSnapshots_StateJson CHECK (ISJSON(State) = 1)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'UX_StateSnapshots_SourceHash'
                 AND object_id = OBJECT_ID(N'dbo.StateSnapshots'))
    CREATE UNIQUE INDEX UX_StateSnapshots_SourceHash
        ON dbo.StateSnapshots (SourceHash);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_StateSnapshots_Agent_Timestamp'
                 AND object_id = OBJECT_ID(N'dbo.StateSnapshots'))
    CREATE INDEX IX_StateSnapshots_Agent_Timestamp
        ON dbo.StateSnapshots (AgentId, Timestamp);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_StateSnapshots_HierarchyLevel'
                 AND object_id = OBJECT_ID(N'dbo.StateSnapshots'))
    CREATE INDEX IX_StateSnapshots_HierarchyLevel
        ON dbo.StateSnapshots (HierarchyLevel);
GO

-- Every role transition and role decision:
--   GRANTED             a commander granted a REQUEST_ROLE
--   DENIED              a commander refused a REQUEST_ROLE
--   SELF_PROMOTED       failover: the designated survivor took the role
--   STEPPED_DOWN        a revived commander saw a higher epoch and rejoined as a follower
--   PROMOTION_DECLINED  failover: the designated survivor failed its live re-check
--   VACANT              failover: no surviving agent is fit; waiting for a human
--   SUCCESSOR_ACCEPTED  failover below: a commander took the successor of its
--                       failed direct follower as its new direct follower
-- Outcomes that change who is in charge (GRANTED, SELF_PROMOTED, STEPPED_DOWN,
-- SUCCESSOR_ACCEPTED)
-- must move the epoch forward. The others leave it unchanged (NewEpoch null or
-- equal to OldEpoch). DENIED and PROMOTION_DECLINED must say why.
IF OBJECT_ID(N'dbo.RoleHandovers', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RoleHandovers (
        Id                  BIGINT IDENTITY(1, 1) NOT NULL,
        SessionId           NVARCHAR(64)  NOT NULL,
        Timestamp           DATETIME2(6)  NOT NULL,
        LoggedBy            NVARCHAR(64)  NOT NULL,
        RequesterId         NVARCHAR(64)  NOT NULL,
        TargetCommanderId   NVARCHAR(64)  NULL,
        PreviousCommanderId NVARCHAR(64)  NULL,
        PreviousRole        VARCHAR(16)   NULL,
        NewRole             VARCHAR(16)   NULL,
        OldEpoch            INT           NOT NULL,
        NewEpoch            INT           NULL,
        Outcome             VARCHAR(24)   NOT NULL,
        Reason              VARCHAR(32)   NULL,
        HierarchyLevel      INT           NULL,
        SourceHash          BINARY(32)    NOT NULL,
        IngestedAt          DATETIME2(6)  NOT NULL
            CONSTRAINT DF_RoleHandovers_IngestedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_RoleHandovers PRIMARY KEY (Id),
        CONSTRAINT FK_RoleHandovers_Sessions
            FOREIGN KEY (SessionId) REFERENCES dbo.Sessions (SessionId),
        CONSTRAINT CK_RoleHandovers_Outcome CHECK (Outcome IN (
            'GRANTED', 'DENIED', 'SELF_PROMOTED',
            'STEPPED_DOWN', 'PROMOTION_DECLINED', 'VACANT',
            'SUCCESSOR_ACCEPTED')),
        CONSTRAINT CK_RoleHandovers_OldEpoch CHECK (OldEpoch >= 0),
        CONSTRAINT CK_RoleHandovers_EpochChange CHECK (
            (Outcome IN ('GRANTED', 'SELF_PROMOTED', 'STEPPED_DOWN',
                         'SUCCESSOR_ACCEPTED')
                AND NewEpoch IS NOT NULL AND NewEpoch > OldEpoch)
            OR (Outcome IN ('DENIED', 'PROMOTION_DECLINED', 'VACANT')
                AND (NewEpoch IS NULL OR NewEpoch = OldEpoch))
        ),
        CONSTRAINT CK_RoleHandovers_DenialReason CHECK (
            Outcome NOT IN ('DENIED', 'PROMOTION_DECLINED') OR Reason IS NOT NULL),
        CONSTRAINT CK_RoleHandovers_HierarchyLevel
            CHECK (HierarchyLevel IS NULL OR HierarchyLevel >= 0)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'UX_RoleHandovers_SourceHash'
                 AND object_id = OBJECT_ID(N'dbo.RoleHandovers'))
    CREATE UNIQUE INDEX UX_RoleHandovers_SourceHash
        ON dbo.RoleHandovers (SourceHash);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_RoleHandovers_HierarchyLevel'
                 AND object_id = OBJECT_ID(N'dbo.RoleHandovers'))
    CREATE INDEX IX_RoleHandovers_HierarchyLevel
        ON dbo.RoleHandovers (HierarchyLevel);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_RoleHandovers_Session_Timestamp'
                 AND object_id = OBJECT_ID(N'dbo.RoleHandovers'))
    CREATE INDEX IX_RoleHandovers_Session_Timestamp
        ON dbo.RoleHandovers (SessionId, Timestamp);
GO
