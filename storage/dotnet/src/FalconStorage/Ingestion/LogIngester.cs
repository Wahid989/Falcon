using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using Dapper;
using FalconStorage.Data;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Options;

namespace FalconStorage.Ingestion;

/// <summary>
/// Loads the agents' JSONL exchange logs into SQL Server.
///
/// A file is identified by the hash of its first non-blank line (its "file key"),
/// not its name, so a log renamed during rotation resumes where it left off.
/// Each row carries a hash of (file key, line position, line content) under a
/// unique index: re-reading a file never duplicates a row, while two identical
/// lines at different positions stay two rows. Each batch of lines commits in
/// one transaction together with the file's new read position.
/// </summary>
internal sealed class LogIngester
{
    // Serializes ingestion across every process using the same database, so two
    // service instances never interleave inserts for the same lines.
    private const string IngestLockName = "falcon-log-ingestion";

    private readonly SqlConnectionFactory _connections;
    private readonly StorageOptions _options;
    private readonly ILogger<LogIngester> _logger;
    private readonly SemaphoreSlim _gate = new(1, 1);

    public LogIngester(
        SqlConnectionFactory connections,
        IOptions<StorageOptions> options,
        ILogger<LogIngester> logger)
    {
        _connections = connections;
        _options = options.Value;
        _logger = logger;
    }

    /// <summary>Called after each line is written inside a batch transaction. Tests use it to inject failures.</summary>
    internal Action<LogLine>? AfterLineWritten { get; set; }

    public async Task<IngestionResult> IngestAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            var result = new IngestionResult();
            var directory = _options.LogDirectory;
            if (string.IsNullOrWhiteSpace(directory) || !Directory.Exists(directory))
            {
                return result;
            }

            // Oldest first, so rotated copies are finished before the live file.
            var files = new DirectoryInfo(directory)
                .EnumerateFiles(_options.LogFilePattern)
                .OrderBy(file => file.LastWriteTimeUtc)
                .ThenBy(file => file.Name, StringComparer.Ordinal)
                .ToList();

            foreach (var file in files)
            {
                try
                {
                    await IngestFileAsync(file.FullName, result, cancellationToken);
                }
                catch (FileNotFoundException)
                {
                    // Rotation deleted the oldest file between listing and opening it.
                    _logger.LogDebug("Log file {File} disappeared before it was read", file.Name);
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    // One file failing (for example the database dropping mid-batch)
                    // must not stop the other files; this one is retried next pass.
                    _logger.LogError(ex, "Ingesting {File} failed; it will be retried", file.Name);
                    result.FailedFiles.Add(new FailedFile(file.Name, ex.Message));
                }
            }

            return result;
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task IngestFileAsync(string path, IngestionResult total, CancellationToken cancellationToken)
    {
        await using var stream = new FileStream(
            path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);

        var keyLine = LogLineReader.ReadCompleteLines(stream, 0, 0)
            .FirstOrDefault(line => !IsBlank(line.Bytes));
        if (keyLine.Bytes is null)
        {
            return;
        }

        total.FilesScanned++;
        var fileKey = SHA256.HashData(keyLine.Bytes);

        long offset = 0;
        long lineNumber = 0;
        await using (var connection = _connections.Create())
        {
            var cursor = await connection.QuerySingleOrDefaultAsync<Cursor>(
                "SELECT ByteOffset, LineNumber FROM dbo.IngestionCursors WHERE FileKey = @fileKey",
                new { fileKey });

            if (cursor is not null && cursor.ByteOffset <= stream.Length)
            {
                offset = cursor.ByteOffset;
                lineNumber = cursor.LineNumber;
            }
            else if (cursor is not null)
            {
                // The file is shorter than the saved position, so it was rewritten
                // rather than appended to. Start over; rows already stored are skipped.
                _logger.LogWarning("Log file {File} was rewritten; reading it again from the start", path);
                await connection.ExecuteAsync(
                    "UPDATE dbo.IngestionCursors SET ByteOffset = 0, LineNumber = 0 WHERE FileKey = @fileKey",
                    new { fileKey });
            }
        }

        var batch = new List<LogLine>(_options.BatchSize);
        foreach (var line in LogLineReader.ReadCompleteLines(stream, offset, lineNumber))
        {
            batch.Add(line);
            if (batch.Count >= _options.BatchSize)
            {
                total.Add(await CommitWithFallbackAsync(fileKey, path, batch, cancellationToken));
                batch.Clear();
            }
        }

        if (batch.Count > 0)
        {
            total.Add(await CommitWithFallbackAsync(fileKey, path, batch, cancellationToken));
        }
    }

    /// <summary>
    /// Commits a batch. If the database refuses it, retries one line at a time so
    /// a single line the database will not take is stored as a reject instead of
    /// blocking the file forever. If the database is unreachable, the retry fails
    /// too and the error reaches the caller.
    /// </summary>
    private async Task<IngestionResult> CommitWithFallbackAsync(
        byte[] fileKey, string path, List<LogLine> batch, CancellationToken cancellationToken)
    {
        try
        {
            return await CommitBatchAsync(fileKey, path, batch, rejectReason: null, cancellationToken);
        }
        catch (SqlException ex) when (IsDataError(ex))
        {
            _logger.LogWarning(
                "Database refused a batch from {File} ({Message}); retrying line by line",
                Path.GetFileName(path), ex.Message);
        }

        var result = new IngestionResult();
        foreach (var line in batch)
        {
            try
            {
                result.Add(await CommitBatchAsync(fileKey, path, [line], rejectReason: null, cancellationToken));
            }
            catch (SqlException ex) when (IsDataError(ex))
            {
                result.Add(await CommitBatchAsync(
                    fileKey, path, [line], $"database rejected the line: {ex.Message}", cancellationToken));
            }
        }

        return result;
    }

    /// <summary>Errors caused by the data itself (constraint, truncation, conversion), not the connection.</summary>
    private static bool IsDataError(SqlException ex) =>
        ex.Number is 547 or 2601 or 2627 or 2628 or 8152 or 245 or 241 or 242 or 8114 or 13609;

    private async Task<IngestionResult> CommitBatchAsync(
        byte[] fileKey, string path, IReadOnlyList<LogLine> batch, string? rejectReason,
        CancellationToken cancellationToken)
    {
        var result = new IngestionResult();
        var fileName = Path.GetFileName(path);
        var parsed = batch
            .Select(line => (Line: line, Text: Encoding.UTF8.GetString(line.Bytes)))
            .Select(item => (
                item.Line,
                item.Text,
                Parsed: rejectReason is null ? LogRecordParser.Parse(item.Text) : ParseResult.Fail(rejectReason)))
            .ToList();

        await using var connection = _connections.Create();
        await connection.OpenAsync(cancellationToken);
        await using var transaction = (SqlTransaction)await connection.BeginTransactionAsync(cancellationToken);

        await connection.ExecuteAsync(
            """
            DECLARE @status INT;
            EXEC @status = sp_getapplock @Resource = @name, @LockMode = 'Exclusive',
                @LockOwner = 'Transaction', @LockTimeout = 60000;
            IF @status < 0 THROW 51000, 'Timed out waiting for the ingestion lock', 1;
            """,
            new { name = IngestLockName },
            transaction);

        var records = parsed.Where(p => p.Parsed.Record is not null).Select(p => p.Parsed.Record!).ToList();
        foreach (var session in records.GroupBy(r => r.SessionId))
        {
            await UpsertSessionAsync(
                connection, transaction, session.Key,
                session.Min(r => r.Timestamp), session.Max(r => r.Timestamp));
        }

        foreach (var (line, text, outcome) in parsed)
        {
            result.LinesRead++;
            var hash = LineHash(fileKey, line);

            if (outcome.IsBlank)
            {
                result.BlankLines++;
            }
            else if (outcome.Error is not null)
            {
                var inserted = await InsertRejectAsync(
                    connection, transaction, fileName, line.LineNumber, outcome.Error, text, hash);
                if (inserted) result.Rejected++; else result.Duplicates++;
            }
            else
            {
                var inserted = await InsertRecordAsync(connection, transaction, outcome.Record!, hash);
                if (!inserted)
                {
                    result.Duplicates++;
                }
                else
                {
                    switch (outcome.Record)
                    {
                        case MessageRecord: result.Messages++; break;
                        case StateSnapshotRecord: result.StateSnapshots++; break;
                        case RoleChangeRecord: result.RoleHandovers++; break;
                        case EventRecord: result.Events++; break;
                    }
                }
            }

            AfterLineWritten?.Invoke(line);
        }

        var last = batch[^1];
        await connection.ExecuteAsync(
            """
            MERGE dbo.IngestionCursors AS target
            USING (SELECT @fileKey AS FileKey) AS source ON target.FileKey = source.FileKey
            WHEN MATCHED AND target.ByteOffset < @offset THEN
                UPDATE SET LastPath = @fileName, ByteOffset = @offset, LineNumber = @lineNumber,
                           UpdatedAt = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (FileKey, LastPath, ByteOffset, LineNumber)
                VALUES (@fileKey, @fileName, @offset, @lineNumber);
            """,
            new { fileKey, fileName, offset = last.EndOffset, lineNumber = last.LineNumber },
            transaction);

        await transaction.CommitAsync(cancellationToken);
        return result;
    }

    /// <summary>Identity of one line: which file, where in it, and what it says.</summary>
    internal static byte[] LineHash(byte[] fileKey, LogLine line)
    {
        var input = new byte[fileKey.Length + sizeof(long) + line.Bytes.Length];
        fileKey.CopyTo(input, 0);
        BinaryPrimitives.WriteInt64BigEndian(input.AsSpan(fileKey.Length), line.EndOffset);
        line.Bytes.CopyTo(input, fileKey.Length + sizeof(long));
        return SHA256.HashData(input);
    }

    private static bool IsBlank(byte[] bytes) => bytes.All(b => b is (byte)' ' or (byte)'\t');

    private static Task UpsertSessionAsync(
        SqlConnection connection, SqlTransaction transaction, string sessionId, DateTime first, DateTime last) =>
        connection.ExecuteAsync(
            """
            MERGE dbo.Sessions AS target
            USING (SELECT @sessionId AS SessionId) AS source ON target.SessionId = source.SessionId
            WHEN MATCHED THEN UPDATE SET
                StartedAt = CASE WHEN target.StartedAt IS NULL OR @first < target.StartedAt
                                 THEN @first ELSE target.StartedAt END,
                EndedAt = CASE WHEN target.EndedAt IS NULL OR @last > target.EndedAt
                               THEN @last ELSE target.EndedAt END
            WHEN NOT MATCHED THEN
                INSERT (SessionId, StartedAt, EndedAt) VALUES (@sessionId, @first, @last);
            """,
            new { sessionId, first, last },
            transaction);

    private static async Task<bool> InsertRejectAsync(
        SqlConnection connection, SqlTransaction transaction,
        string fileName, long lineNumber, string reason, string rawLine, byte[] hash)
    {
        var rows = await connection.ExecuteAsync(
            """
            INSERT INTO dbo.IngestionRejects (SourceFile, LineNumber, Reason, RawLine, SourceHash)
            SELECT @fileName, @lineNumber, @reason, @rawLine, @hash
            WHERE NOT EXISTS (SELECT 1 FROM dbo.IngestionRejects WHERE SourceHash = @hash);
            """,
            new { fileName, lineNumber, reason = Truncate(reason, 400), rawLine, hash },
            transaction);
        return rows > 0;
    }

    private static async Task<bool> InsertRecordAsync(
        SqlConnection connection, SqlTransaction transaction, LogRecord record, byte[] hash)
    {
        var (sql, parameters) = record switch
        {
            MessageRecord m => (
                """
                INSERT INTO dbo.Messages (SessionId, MessageId, Timestamp, LoggedAt, LoggedBy, SenderId,
                    ReceiverId, Direction, MessageType, SenderRole, RoleEpoch, SchemaVersion,
                    HierarchyLevel, Payload, SourceHash)
                SELECT @SessionId, @MessageId, @Timestamp, @LoggedAt, @LoggedBy, @SenderId,
                    @ReceiverId, @Direction, @MessageType, @SenderRole, @RoleEpoch, @SchemaVersion,
                    @HierarchyLevel, @Payload, @Hash
                WHERE NOT EXISTS (SELECT 1 FROM dbo.Messages WHERE SourceHash = @Hash);
                """,
                (object)new
                {
                    m.SessionId, m.MessageId, m.Timestamp, m.LoggedAt, m.LoggedBy, m.SenderId,
                    m.ReceiverId, m.Direction, m.MessageType, m.SenderRole, m.RoleEpoch,
                    m.SchemaVersion, m.HierarchyLevel, m.Payload, Hash = hash,
                }),
            StateSnapshotRecord s => (
                """
                INSERT INTO dbo.StateSnapshots (SessionId, AgentId, Timestamp, LoggedBy, RoleEpoch,
                    HierarchyLevel, TriggerMessageId, State, SourceHash)
                SELECT @SessionId, @AgentId, @Timestamp, @LoggedBy, @RoleEpoch,
                    @HierarchyLevel, @TriggerMessageId, @State, @Hash
                WHERE NOT EXISTS (SELECT 1 FROM dbo.StateSnapshots WHERE SourceHash = @Hash);
                """,
                new
                {
                    s.SessionId, s.AgentId, s.Timestamp, s.LoggedBy, s.RoleEpoch,
                    s.HierarchyLevel, s.TriggerMessageId, s.State, Hash = hash,
                }),
            RoleChangeRecord r => (
                """
                INSERT INTO dbo.RoleHandovers (SessionId, Timestamp, LoggedBy, RequesterId,
                    TargetCommanderId, PreviousCommanderId, PreviousRole, NewRole, OldEpoch, NewEpoch,
                    Outcome, Reason, HierarchyLevel, SourceHash)
                SELECT @SessionId, @Timestamp, @LoggedBy, @RequesterId,
                    @TargetCommanderId, @PreviousCommanderId, @PreviousRole, @NewRole, @OldEpoch, @NewEpoch,
                    @Outcome, @Reason, @HierarchyLevel, @Hash
                WHERE NOT EXISTS (SELECT 1 FROM dbo.RoleHandovers WHERE SourceHash = @Hash);
                """,
                new
                {
                    r.SessionId, r.Timestamp, r.LoggedBy, r.RequesterId, r.TargetCommanderId,
                    r.PreviousCommanderId, r.PreviousRole, r.NewRole, r.OldEpoch, r.NewEpoch,
                    r.Outcome, r.Reason, r.HierarchyLevel, Hash = hash,
                }),
            EventRecord e => (
                """
                INSERT INTO dbo.Events (SessionId, Timestamp, LoggedBy, Severity, EventType,
                    HierarchyLevel, Detail, SourceHash)
                SELECT @SessionId, @Timestamp, @LoggedBy, @Severity, @EventType,
                    @HierarchyLevel, @Detail, @Hash
                WHERE NOT EXISTS (SELECT 1 FROM dbo.Events WHERE SourceHash = @Hash);
                """,
                new
                {
                    e.SessionId, e.Timestamp, e.LoggedBy, e.Severity, e.EventType,
                    e.HierarchyLevel, e.Detail, Hash = hash,
                }),
            _ => throw new ArgumentOutOfRangeException(nameof(record), record.GetType().Name, null),
        };

        return await connection.ExecuteAsync(sql, parameters, transaction) > 0;
    }

    private static string Truncate(string text, int maxLength) =>
        text.Length <= maxLength ? text : text[..maxLength];

    private sealed class Cursor
    {
        public long ByteOffset { get; set; }

        public long LineNumber { get; set; }
    }
}
