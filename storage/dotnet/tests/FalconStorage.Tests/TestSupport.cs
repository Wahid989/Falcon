using System.Text;
using System.Text.Json;
using FalconStorage.Data;
using FalconStorage.Ingestion;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using Xunit;

namespace FalconStorage.Tests;

/// <summary>
/// Access to the SQL Server used by integration tests. Set FALCON_TEST_DB to a
/// connection string; use a dedicated database (e.g. Database=FalconTest), which
/// the tests create, so test rows never mix with development data. Without it
/// those tests are skipped, unless FALCON_REQUIRE_DB=1, which turns a missing
/// database into a failure (for CI).
/// </summary>
internal static class TestDatabase
{
    private static readonly SemaphoreSlim SchemaLock = new(1, 1);
    private static bool _schemaApplied;

    public static string? ConnectionString =>
        Environment.GetEnvironmentVariable("FALCON_TEST_DB") is { Length: > 0 } value ? value : null;

    public static void SkipIfUnavailable()
    {
        if (ConnectionString is not null)
        {
            return;
        }

        if (Environment.GetEnvironmentVariable("FALCON_REQUIRE_DB") == "1")
        {
            throw new InvalidOperationException("FALCON_REQUIRE_DB=1 but FALCON_TEST_DB is not set");
        }

        Skip.If(true, "FALCON_TEST_DB is not set");
    }

    public static string SchemaScriptsPath
    {
        get
        {
            for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            {
                var candidate = Path.Combine(dir.FullName, "sql", "init");
                if (Directory.Exists(candidate))
                {
                    return candidate;
                }
            }

            throw new DirectoryNotFoundException("Could not find storage/sql/init above the test output");
        }
    }

    public static StorageOptions Options(string logDirectory = "", int batchSize = 500) => new()
    {
        ConnectionString = ConnectionString ?? string.Empty,
        LogDirectory = logDirectory,
        BatchSize = batchSize,
        IngestIntervalSeconds = 0,
        SchemaScriptsPath = SchemaScriptsPath,
    };

    public static SqlConnectionFactory Connections() => new(Microsoft.Extensions.Options.Options.Create(Options()));

    /// <summary>Applies the init scripts once per test run (they are idempotent anyway).</summary>
    public static async Task EnsureSchemaAsync()
    {
        await SchemaLock.WaitAsync();
        try
        {
            if (_schemaApplied)
            {
                return;
            }

            var options = Microsoft.Extensions.Options.Options.Create(Options());
            var initializer = new SchemaInitializer(
                new SqlConnectionFactory(options), options, NullLogger<SchemaInitializer>.Instance);
            await initializer.ApplyAsync(CancellationToken.None);
            _schemaApplied = true;
        }
        finally
        {
            SchemaLock.Release();
        }
    }

    public static LogIngester Ingester(string logDirectory, int batchSize = 500)
    {
        var options = Microsoft.Extensions.Options.Options.Create(Options(logDirectory, batchSize));
        return new LogIngester(new SqlConnectionFactory(options), options, NullLogger<LogIngester>.Instance);
    }
}

/// <summary>A temporary log directory, deleted when disposed.</summary>
public sealed class TempLogDirectory : IDisposable
{
    public TempLogDirectory()
    {
        Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "falcon-logs-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path);
    }

    public string Path { get; }

    public string File(string name) => System.IO.Path.Combine(Path, name);

    public void Append(string name, params string[] lines) =>
        System.IO.File.AppendAllText(File(name), string.Concat(lines.Select(line => line + "\n")), new UTF8Encoding(false));

    public void Dispose()
    {
        try
        {
            Directory.Delete(Path, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}

/// <summary>Builds exchange-log lines in the format the ingester reads.</summary>
public static class LogLines
{
    private static readonly DateTime Start = new(2026, 10, 13, 12, 0, 0, DateTimeKind.Utc);

    public static string NewSession() => "test-" + Guid.NewGuid().ToString("N");

    public static string Message(
        string session, string messageId, string direction = "sent", int? level = 0,
        string loggedBy = "ground", string sender = "ground", string receiver = "drone-cmd",
        string type = "HOLD", int epoch = 1, int second = 0)
    {
        var record = new Dictionary<string, object?>
        {
            ["record_type"] = "message",
            ["session_id"] = session,
            ["logged_by"] = loggedBy,
            ["logged_at"] = Start.AddSeconds(second).AddMilliseconds(5),
            ["direction"] = direction,
            ["hierarchy_level"] = level,
            ["message"] = new Dictionary<string, object?>
            {
                ["message_id"] = messageId,
                ["sender_id"] = sender,
                ["recipient_id"] = receiver,
                ["timestamp"] = Start.AddSeconds(second),
                ["schema_version"] = "1.0",
                ["sender_role"] = "commander",
                ["role_epoch"] = epoch,
                ["type"] = type,
                ["payload"] = new Dictionary<string, object?> { ["note"] = messageId },
            },
        };
        return JsonSerializer.Serialize(record);
    }

    public static string StateSnapshot(string session, string agent, int battery, int? level = 1, int second = 0) =>
        JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["record_type"] = "state_snapshot",
            ["session_id"] = session,
            ["logged_by"] = agent,
            ["logged_at"] = Start.AddSeconds(second),
            ["hierarchy_level"] = level,
            ["role_epoch"] = 1,
            ["state"] = new Dictionary<string, object?> { ["battery"] = battery },
        });

    public static string RoleChange(
        string session, string outcome, int oldEpoch, int? newEpoch, string? reason = null, int? level = 0,
        int second = 0) =>
        JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["record_type"] = "role_change",
            ["session_id"] = session,
            ["logged_by"] = "ground",
            ["logged_at"] = Start.AddSeconds(second),
            ["hierarchy_level"] = level,
            ["requester_id"] = "drone-cmd",
            ["target_commander_id"] = "ground",
            ["previous_role"] = "follower",
            ["new_role"] = "commander",
            ["old_epoch"] = oldEpoch,
            ["new_epoch"] = newEpoch,
            ["outcome"] = outcome,
            ["reason"] = reason,
        });

    public static string Event(string session, string eventType, string severity = "warning", int second = 0) =>
        JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["record_type"] = "event",
            ["session_id"] = session,
            ["logged_by"] = "drone-a",
            ["logged_at"] = Start.AddSeconds(second),
            ["severity"] = severity,
            ["event_type"] = eventType,
            ["detail"] = new Dictionary<string, object?> { ["session"] = session },
        });
}
