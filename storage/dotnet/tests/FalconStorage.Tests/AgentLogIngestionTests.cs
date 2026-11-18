using Dapper;
using Xunit;

namespace FalconStorage.Tests;

/// <summary>
/// Ingests exchange logs written by the real Python Commander and Follower
/// (Fixtures/agent-logs), so the agents' log format and this service can't
/// drift apart unnoticed. The fixture is checked against the live writer on
/// the Python side.
/// </summary>
[Collection("database")]
public class AgentLogIngestionTests
{
    private const string FixtureSession = "storage-fixture";

    private sealed record Row(
        string MessageId, string LoggedBy, string Direction, string MessageType, int? HierarchyLevel, string Payload);

    private sealed record Handover(
        string LoggedBy, string RequesterId, string? PreviousCommanderId, string? NewRole,
        int OldEpoch, int? NewEpoch, string Outcome, int? HierarchyLevel);

    /// <summary>Copies a fixture into a nested folder under a fresh session id.</summary>
    private static (TempLogDirectory Logs, string Session) CopyFixture(string name = "session-a")
    {
        var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        var source = Path.Combine(AppContext.BaseDirectory, "Fixtures", "agent-logs", name);
        var target = Path.Combine(logs.Path, "mvp", session);
        Directory.CreateDirectory(target);
        foreach (var file in Directory.EnumerateFiles(source, "*.jsonl"))
        {
            var text = File.ReadAllText(file).Replace($"\"{FixtureSession}\"", $"\"{session}\"");
            File.WriteAllText(Path.Combine(target, Path.GetFileName(file)), text);
        }

        return (logs, session);
    }

    private static async Task<List<Row>> RowsAsync(string session)
    {
        await using var connection = TestDatabase.Connections().Create();
        return (await connection.QueryAsync<Row>(
            """
            SELECT MessageId, LoggedBy, Direction, MessageType, HierarchyLevel, Payload
            FROM dbo.Messages WHERE SessionId = @session
            """,
            new { session })).ToList();
    }

    [SkippableFact]
    public async Task EveryAgentExchangeReachesTheDatabase()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        var (logs, session) = CopyFixture();
        using var _ = logs;

        var result = await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);
        var rows = await RowsAsync(session);

        Assert.Equal(0, result.Rejected);
        Assert.Empty(result.FailedFiles);
        // 3 commands + 3 responses, each logged by the sender and the receiver.
        Assert.Equal(12, rows.Count);
        Assert.Equal(6, rows.Select(row => row.MessageId).Distinct().Count());
        foreach (var message in rows.GroupBy(row => row.MessageId))
        {
            Assert.Equal(["RECEIVED", "SENT"], message.Select(row => row.Direction).Order());
        }

        Assert.Equal(["command", "response"], rows.Select(row => row.MessageType).Distinct().Order());
        Assert.Contains(rows, row => row.Payload.Contains("\"BATTERY_CRITICAL\""));
    }

    [SkippableFact]
    public async Task HierarchyLevelIsStoredPerRecord()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        var (logs, session) = CopyFixture();
        using var _ = logs;

        await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);
        var rows = await RowsAsync(session);

        // The commander is the top (0) and follower-1 is one level below it.
        // A sent row carries the logger's own level, a received row the sender's.
        Assert.All(rows, row =>
        {
            var expected = (row.LoggedBy, row.Direction) switch
            {
                ("commander", "SENT") => 0,
                ("commander", "RECEIVED") => 1,
                ("follower-1", "SENT") => 1,
                ("follower-1", "RECEIVED") => 0,
                _ => throw new Xunit.Sdk.XunitException($"unexpected row {row}"),
            };
            Assert.Equal(expected, row.HierarchyLevel);
            Assert.DoesNotContain("hierarchy_level", row.Payload);
        });
    }

    [SkippableFact]
    public async Task IngestingAgentLogsTwiceAddsNothing()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        var (logs, session) = CopyFixture();
        using var _ = logs;
        var ingester = TestDatabase.Ingester(logs.Path);

        await ingester.IngestAsync(CancellationToken.None);
        var again = await ingester.IngestAsync(CancellationToken.None);

        Assert.Equal(12, (await RowsAsync(session)).Count);
        Assert.Equal(0, again.LinesRead);
    }

    [SkippableFact]
    public async Task LogsInSubfoldersAreIngestedAlongsideTopLevelLogs()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        Directory.CreateDirectory(logs.File(Path.Combine("mvp", "run-1")));
        logs.Append("ground.jsonl", LogLines.Message(session, "m-1", "sent", level: 0, second: 1));
        logs.Append(
            Path.Combine("mvp", "run-1", "drone-cmd.jsonl"),
            LogLines.Message(session, "m-1", "received", level: 0, loggedBy: "drone-cmd", second: 1));

        var result = await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);

        Assert.Equal(2, result.FilesScanned);
        Assert.Equal(2, (await RowsAsync(session)).Count);
    }

    /// <summary>
    /// A failover logged by the real agents (Fixtures/agent-logs/failover):
    /// drone-cmd dies, drone-a promotes itself, Ground accepts it as its new
    /// direct follower, then orders a HOLD through it.
    /// </summary>
    [SkippableFact]
    public async Task BothSidesOfAFailoverReachRoleHandovers()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        var (logs, session) = CopyFixture("failover");
        using var _ = logs;

        var result = await TestDatabase.Ingester(logs.Path).IngestAsync(CancellationToken.None);
        await using var connection = TestDatabase.Connections().Create();
        var handovers = (await connection.QueryAsync<Handover>(
            """
            SELECT LoggedBy, RequesterId, PreviousCommanderId, NewRole,
                   OldEpoch, NewEpoch, Outcome, HierarchyLevel
            FROM dbo.RoleHandovers WHERE SessionId = @session ORDER BY LoggedBy
            """,
            new { session })).ToList();

        Assert.Equal(0, result.Rejected);
        Assert.Equal(
            [
                new Handover("drone-a", "drone-a", "drone-cmd", "commander", 0, 1, "SELF_PROMOTED", 1),
                new Handover("ground", "drone-a", "drone-cmd", "commander", 0, 1, "SUCCESSOR_ACCEPTED", 0),
            ],
            handovers);
        // HOLD, two relays and three answers, each logged by sender and receiver.
        Assert.Equal(12, (await RowsAsync(session)).Count);
    }
}
