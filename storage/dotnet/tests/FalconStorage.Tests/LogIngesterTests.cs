using System.Security.Cryptography;
using System.Text;
using Dapper;
using Xunit;

namespace FalconStorage.Tests;

/// <summary>
/// Ingestion against a real SQL Server. Each test uses its own session id and
/// log directory, so tests do not see each other's rows.
/// </summary>
[Collection("database")]
public class LogIngesterTests
{
    private sealed record Counts(int Messages, int Snapshots, int Handovers, int Events, int Rejects);

    private static async Task<Counts> CountRowsAsync(string session)
    {
        await using var connection = TestDatabase.Connections().Create();
        return await connection.QuerySingleAsync<Counts>(
            """
            SELECT
                (SELECT COUNT(*) FROM dbo.Messages WHERE SessionId = @session) AS Messages,
                (SELECT COUNT(*) FROM dbo.StateSnapshots WHERE SessionId = @session) AS Snapshots,
                (SELECT COUNT(*) FROM dbo.RoleHandovers WHERE SessionId = @session) AS Handovers,
                (SELECT COUNT(*) FROM dbo.Events WHERE SessionId = @session) AS Events,
                (SELECT COUNT(*) FROM dbo.IngestionRejects WHERE RawLine LIKE '%' + @session + '%') AS Rejects
            """,
            new { session });
    }

    private static string[] MixedLog(string session) =>
    [
        LogLines.Message(session, "m-1", "sent", level: 0, second: 1),
        LogLines.Message(session, "m-1", "received", level: 0, loggedBy: "drone-cmd", second: 1),
        LogLines.Message(session, "m-2", "received", level: null, loggedBy: "drone-cmd", sender: "stranger", second: 2),
        LogLines.StateSnapshot(session, "drone-cmd", battery: 75, second: 3),
        LogLines.RoleChange(session, "granted", 1, 2, second: 4),
        LogLines.RoleChange(session, "denied", 2, null, "BUSY", second: 5),
        LogLines.Event(session, "STALE_EPOCH_REJECTED", second: 6),
        $$"""{"record_type":"message","session_id":"{{session}}","broken":true}""",
    ];

    [SkippableFact]
    public async Task EveryLogLineBecomesExactlyOneRow()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("ground.jsonl", MixedLog(session));

        var result = await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(new Counts(3, 1, 2, 1, 1), await CountRowsAsync(session));
        Assert.Equal(8, result.LinesRead);
        Assert.Equal(1, result.Rejected);
    }

    [SkippableFact]
    public async Task IngestingTwiceCreatesNoDuplicates()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("ground.jsonl", MixedLog(session));

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);
        // A fresh ingester has no in-memory state; only the database prevents duplicates.
        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(new Counts(3, 1, 2, 1, 1), await CountRowsAsync(session));
    }

    [SkippableFact]
    public async Task ReingestingAfterCursorLossCreatesNoDuplicates()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var lines = MixedLog(session);
        logs.Append("ground.jsonl", lines);
        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        await using (var connection = TestDatabase.Connections().Create())
        {
            var fileKey = SHA256.HashData(Encoding.UTF8.GetBytes(lines[0]));
            await connection.ExecuteAsync("DELETE FROM dbo.IngestionCursors WHERE FileKey = @fileKey", new { fileKey });
        }

        var second = await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(8, second.LinesRead);
        Assert.Equal(8, second.Duplicates);
        Assert.Equal(new Counts(3, 1, 2, 1, 1), await CountRowsAsync(session));
    }

    [SkippableFact]
    public async Task ARepeatedMessageIdIsAWireEventAndIsKept()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append(
            "ground.jsonl",
            LogLines.Message(session, "m-1", second: 1),
            LogLines.Message(session, "m-1", second: 2));

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(2, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task IdenticalLinesAtDifferentPositionsAreBothKept()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var resend = LogLines.Message(session, "m-1", "received", loggedBy: "drone-cmd", second: 1);
        logs.Append("drone-cmd.jsonl", LogLines.Message(session, "m-0"), resend, resend);

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);
        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(3, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task FilesStartingWithTheSameBlankLineKeepSeparateProgress()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("a.jsonl", "", LogLines.Message(session, "a-1"), LogLines.Message(session, "a-2"));
        logs.Append("b.jsonl", "", LogLines.Message(session, "b-1"));

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(3, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task ARewrittenFileIsReadAgainFromTheStart()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var ingester = TestDatabase.Ingester(logs.Path);
        var first = LogLines.Message(session, "m-1");
        logs.Append(
            "ground.jsonl",
            first,
            LogLines.Message(session, "m-2-with-a-long-id-to-pad-the-file"),
            LogLines.Message(session, "m-3"));
        await ingester.IngestAsync(CancellationToken.None);

        File.Delete(logs.File("ground.jsonl"));
        logs.Append("ground.jsonl", first, LogLines.Message(session, "m-4"));
        var result = await ingester.IngestAsync(CancellationToken.None);

        Assert.Equal(1, result.Messages);
        Assert.Equal(1, result.Duplicates);
        Assert.Equal(4, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task AFileThatKeepsFailingDoesNotBlockOtherFiles()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var poison = LogLines.Message(session, "poison");
        logs.Append("a.jsonl", poison);
        File.SetLastWriteTimeUtc(logs.File("a.jsonl"), DateTime.UtcNow.AddMinutes(-5));
        logs.Append("b.jsonl", LogLines.Message(session, "fine"));

        var ingester = TestDatabase.Ingester(logs.Path);
        ingester.AfterLineWritten = line =>
        {
            if (line.Bytes.SequenceEqual(Encoding.UTF8.GetBytes(poison)))
            {
                throw new InvalidOperationException("simulated persistent failure");
            }
        };

        var result = await ingester.IngestAsync(CancellationToken.None);

        Assert.Equal("a.jsonl", Assert.Single(result.FailedFiles).File);
        Assert.Equal(1, result.Messages);
        Assert.Equal(1, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task RotationResumesWithoutLossOrDuplicates()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var ingester = TestDatabase.Ingester(logs.Path);

        logs.Append("ground.jsonl", LogLines.Message(session, "m-1", second: 1), LogLines.Message(session, "m-2", second: 2));
        await ingester.IngestAsync(CancellationToken.None);

        // The writer adds a line, then rotates before the next pass: the old file is
        // renamed and a new live file starts.
        logs.Append("ground.jsonl", LogLines.Message(session, "m-3", second: 3));
        File.Move(logs.File("ground.jsonl"), logs.File("ground.jsonl.1"));
        logs.Append("ground.jsonl", LogLines.Message(session, "m-4", second: 4));

        var result = await ingester.IngestAsync(CancellationToken.None);

        Assert.Equal(4, (await CountRowsAsync(session)).Messages);
        Assert.Equal(2, result.LinesRead);
        Assert.Equal(0, result.Duplicates);
    }

    [SkippableFact]
    public async Task APartialLastLineWaitsUntilItIsComplete()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var ingester = TestDatabase.Ingester(logs.Path);
        var second = LogLines.Message(session, "m-2", second: 2);

        logs.Append("ground.jsonl", LogLines.Message(session, "m-1", second: 1));
        File.AppendAllText(logs.File("ground.jsonl"), second[..20]);
        var first = await ingester.IngestAsync(CancellationToken.None);

        File.AppendAllText(logs.File("ground.jsonl"), second[20..] + "\n");
        var next = await ingester.IngestAsync(CancellationToken.None);

        Assert.Equal(1, first.LinesRead);
        Assert.Equal(1, next.LinesRead);
        Assert.Equal(0, next.Rejected);
        Assert.Equal(2, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task HierarchyLevelIsStoredIncludingUnknown()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append(
            "ground.jsonl",
            LogLines.Message(session, "m-0", level: 0),
            LogLines.Message(session, "m-1", level: 1),
            LogLines.Message(session, "m-5", level: 5),
            LogLines.Message(session, "m-x", level: null));

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        await using var connection = TestDatabase.Connections().Create();
        var levels = (await connection.QueryAsync<(string MessageId, int? HierarchyLevel)>(
            "SELECT MessageId, HierarchyLevel FROM dbo.Messages WHERE SessionId = @session ORDER BY MessageId",
            new { session })).ToList();
        Assert.Equal([("m-0", 0), ("m-1", 1), ("m-5", 5), ("m-x", (int?)null)], levels);
    }

    [SkippableFact]
    public async Task ConcurrentIngestersNeverDuplicateRows()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var lines = Enumerable.Range(0, 300)
            .Select(i => LogLines.Message(session, $"m-{i}", second: i))
            .ToArray();
        logs.Append("ground.jsonl", lines);

        // Separate instances share nothing in memory, like two service processes.
        var results = await Task.WhenAll(Enumerable.Range(0, 4).Select(_ =>
            TestDatabase.Ingester(logs.Path, batchSize: 25).IngestAsync(CancellationToken.None)));

        Assert.Equal(300, (await CountRowsAsync(session)).Messages);
        Assert.Equal(300, results.Sum(r => r.Messages));
    }

    [SkippableFact]
    public async Task AFailedBatchRollsBackRowsAndCursor()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("ground.jsonl", Enumerable.Range(0, 10).Select(i => LogLines.Message(session, $"m-{i}", second: i)).ToArray());

        var failing = TestDatabase.Ingester(logs.Path, batchSize: 4);
        failing.AfterLineWritten = line =>
        {
            if (line.LineNumber == 6)
            {
                throw new InvalidOperationException("simulated failure");
            }
        };

        var failed = await failing.IngestAsync(CancellationToken.None);

        // The first batch (lines 1-4) committed; the failed batch (5-8) left nothing behind.
        Assert.Equal("ground.jsonl", Assert.Single(failed.FailedFiles).File);
        Assert.Equal(4, failed.Messages);
        Assert.Equal(4, (await CountRowsAsync(session)).Messages);

        var retry = await TestDatabase.Ingester(logs.Path, batchSize: 4).IngestAsync(CancellationToken.None);

        Assert.Equal(6, retry.LinesRead);
        Assert.Equal(0, retry.Duplicates);
        Assert.Equal(10, (await CountRowsAsync(session)).Messages);
    }

    [SkippableFact]
    public async Task SessionSpanCoversAllRecords()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("a.jsonl", LogLines.Message(session, "m-1", second: 10));
        logs.Append("b.jsonl", LogLines.Message(session, "m-2", second: 2), LogLines.Message(session, "m-3", second: 30));

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        await using var connection = TestDatabase.Connections().Create();
        var span = await connection.QuerySingleAsync<(DateTime StartedAt, DateTime EndedAt)>(
            "SELECT StartedAt, EndedAt FROM dbo.Sessions WHERE SessionId = @session", new { session });
        Assert.Equal(28, (span.EndedAt - span.StartedAt).TotalSeconds);
    }

    [SkippableFact]
    public async Task MissingLogDirectoryIsNotAnError()
    {
        TestDatabase.SkipIfUnavailable();

        var result = await TestDatabase.Ingester(Path.Combine(Path.GetTempPath(), "falcon-missing-" + Guid.NewGuid()))
            .IngestAsync(CancellationToken.None);

        Assert.Equal(0, result.FilesScanned);
    }
}

[CollectionDefinition("database", DisableParallelization = true)]
public class DatabaseCollection
{
}
