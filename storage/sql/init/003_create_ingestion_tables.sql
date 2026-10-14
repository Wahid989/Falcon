-- FALCON log-ingestion tables (FALCON-042).
--
-- Events        error/event records from the exchange log.
-- IngestionRejects  log lines that could not be parsed. Every line ends up as a
--                   row somewhere, so nothing in a log is silently dropped.
-- IngestionCursors  how far each log file has been read. A file is identified
--                   by the hash of its first non-blank line rather than its
--                   name, so a rotated (renamed) file resumes where it left off.
--
-- Idempotent: safe to run repeatedly. $(FalconDb) is the database name (see
-- 001_create_database.sql).

USE [$(FalconDb)];
GO

IF OBJECT_ID(N'dbo.Events', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Events (
        Id             BIGINT IDENTITY(1, 1) NOT NULL,
        SessionId      NVARCHAR(64)  NOT NULL,
        Timestamp      DATETIME2(6)  NOT NULL,
        LoggedBy       NVARCHAR(64)  NOT NULL,
        Severity       VARCHAR(8)    NOT NULL,
        EventType      VARCHAR(64)   NOT NULL,
        HierarchyLevel INT           NULL,
        Detail         NVARCHAR(MAX) NULL,
        SourceHash     BINARY(32)    NOT NULL,
        IngestedAt     DATETIME2(6)  NOT NULL
            CONSTRAINT DF_Events_IngestedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_Events PRIMARY KEY (Id),
        CONSTRAINT FK_Events_Sessions
            FOREIGN KEY (SessionId) REFERENCES dbo.Sessions (SessionId),
        CONSTRAINT CK_Events_Severity
            CHECK (Severity IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
        CONSTRAINT CK_Events_HierarchyLevel
            CHECK (HierarchyLevel IS NULL OR HierarchyLevel >= 0),
        CONSTRAINT CK_Events_DetailJson CHECK (Detail IS NULL OR ISJSON(Detail) = 1)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'UX_Events_SourceHash'
                 AND object_id = OBJECT_ID(N'dbo.Events'))
    CREATE UNIQUE INDEX UX_Events_SourceHash ON dbo.Events (SourceHash);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_Events_Session_Timestamp'
                 AND object_id = OBJECT_ID(N'dbo.Events'))
    CREATE INDEX IX_Events_Session_Timestamp ON dbo.Events (SessionId, Timestamp);
GO

IF OBJECT_ID(N'dbo.IngestionRejects', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.IngestionRejects (
        Id         BIGINT IDENTITY(1, 1) NOT NULL,
        SourceFile NVARCHAR(400) NOT NULL,
        LineNumber BIGINT        NOT NULL,
        Reason     NVARCHAR(400) NOT NULL,
        RawLine    NVARCHAR(MAX) NOT NULL,
        SourceHash BINARY(32)    NOT NULL,
        RejectedAt DATETIME2(6)  NOT NULL
            CONSTRAINT DF_IngestionRejects_RejectedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_IngestionRejects PRIMARY KEY (Id)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'UX_IngestionRejects_SourceHash'
                 AND object_id = OBJECT_ID(N'dbo.IngestionRejects'))
    CREATE UNIQUE INDEX UX_IngestionRejects_SourceHash
        ON dbo.IngestionRejects (SourceHash);
GO

IF OBJECT_ID(N'dbo.IngestionCursors', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.IngestionCursors (
        FileKey    BINARY(32)    NOT NULL,
        LastPath   NVARCHAR(400) NOT NULL,
        ByteOffset BIGINT        NOT NULL,
        LineNumber BIGINT        NOT NULL,
        UpdatedAt  DATETIME2(6)  NOT NULL
            CONSTRAINT DF_IngestionCursors_UpdatedAt DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_IngestionCursors PRIMARY KEY (FileKey),
        CONSTRAINT CK_IngestionCursors_Offsets CHECK (ByteOffset >= 0 AND LineNumber >= 0)
    );
END
GO
