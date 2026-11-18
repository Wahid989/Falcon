using FalconStorage.Ingestion;
using Xunit;

namespace FalconStorage.Tests;

public class LogRecordParserTests
{
    private const string Session = "s1";

    [Fact]
    public void ParsesSentMessageWithAllEnvelopeFields()
    {
        var result = LogRecordParser.Parse(LogLines.Message(Session, "m-1", level: 2));

        var message = Assert.IsType<MessageRecord>(result.Record);
        Assert.Equal("SENT", message.Direction);
        Assert.Equal("m-1", message.MessageId);
        Assert.Equal("ground", message.SenderId);
        Assert.Equal("drone-cmd", message.ReceiverId);
        Assert.Equal("HOLD", message.MessageType);
        Assert.Equal("commander", message.SenderRole);
        Assert.Equal(1, message.RoleEpoch);
        Assert.Equal("1.0", message.SchemaVersion);
        Assert.Equal(2, message.HierarchyLevel);
        Assert.Equal(DateTimeKind.Utc, message.Timestamp.Kind);
        Assert.Contains("\"note\"", message.Payload);
    }

    [Fact]
    public void NullHierarchyLevelIsKeptAsUnknown()
    {
        var result = LogRecordParser.Parse(LogLines.Message(Session, "m-1", direction: "received", level: null));

        var message = Assert.IsType<MessageRecord>(result.Record);
        Assert.Equal("RECEIVED", message.Direction);
        Assert.Null(message.HierarchyLevel);
    }

    [Fact]
    public void RecordTypeDefaultsToMessageWhenEnvelopeIsPresent()
    {
        var line = LogLines.Message(Session, "m-1").Replace("\"record_type\":\"message\",", "");

        Assert.IsType<MessageRecord>(LogRecordParser.Parse(line).Record);
    }

    [Fact]
    public void SessionIdDefaultsWhenMissing()
    {
        var line = LogLines.Message("x", "m-1").Replace("\"session_id\":\"x\",", "");

        Assert.Equal(LogRecordParser.DefaultSessionId, LogRecordParser.Parse(line).Record!.SessionId);
    }

    [Fact]
    public void AcceptsUnixEpochSecondsAsTimestamp()
    {
        var line = """
            {"record_type":"event","logged_by":"a","timestamp":1760356800.5,"event_type":"X"}
            """;

        var record = LogRecordParser.Parse(line).Record!;
        Assert.Equal(new DateTime(2025, 10, 13, 12, 0, 0, 500, DateTimeKind.Utc), record.Timestamp);
    }

    [Fact]
    public void TimestampWithoutAnOffsetIsTreatedAsUtc()
    {
        var line = """
            {"record_type":"event","logged_by":"a","logged_at":"2026-10-13T12:00:00","event_type":"X"}
            """;

        var record = LogRecordParser.Parse(line).Record!;
        Assert.Equal(new DateTime(2026, 10, 13, 12, 0, 0, DateTimeKind.Utc), record.Timestamp);
        Assert.Equal(DateTimeKind.Utc, record.Timestamp.Kind);
    }

    [Fact]
    public void ALoneSurrogateIsRejectedNotThrown()
    {
        var line = LogLines.Message(Session, "m-1")
            .Replace("\"logged_by\":\"ground\"", "\"logged_by\":\"\\ud800\"");
        Assert.Contains("\\ud800", line);

        var result = LogRecordParser.Parse(line);

        Assert.Null(result.Record);
        Assert.Contains("unreadable", result.Error);
    }

    [Theory]
    [InlineData("1", true)]
    [InlineData("1.0", true)]
    [InlineData("12345678901234567", false)]
    [InlineData("1.00000000000000000", false)]
    public void NumericSchemaVersionIsLengthChecked(string version, bool accepted)
    {
        var line = LogLines.Message(Session, "m-1")
            .Replace("\"schema_version\":\"1.0\"", $"\"schema_version\":{version}");
        Assert.Contains($"\"schema_version\":{version}", line);

        var result = LogRecordParser.Parse(line);

        Assert.Equal(accepted, result.Record is not null);
        if (!accepted)
        {
            Assert.Contains("schema_version", result.Error);
        }
    }

    [Fact]
    public void ParsesStateSnapshotAndDefaultsAgentToLogger()
    {
        var snapshot = Assert.IsType<StateSnapshotRecord>(
            LogRecordParser.Parse(LogLines.StateSnapshot(Session, "drone-a", battery: 42)).Record);

        Assert.Equal("drone-a", snapshot.AgentId);
        Assert.Contains("42", snapshot.State);
    }

    [Theory]
    [InlineData("granted", 1, 2, null)]
    [InlineData("denied", 1, null, "BUSY")]
    [InlineData("denied", 1, 1, "TERRAIN_UNSUITABLE")]
    [InlineData("self_promoted", 2, 3, null)]
    [InlineData("stepped_down", 1, 3, "STALE_EPOCH")]
    [InlineData("promotion_declined", 3, null, "BATTERY_CRITICAL")]
    [InlineData("vacant", 3, null, null)]
    [InlineData("successor_accepted", 0, 1, null)]
    public void AcceptsEveryValidRoleOutcome(string outcome, int oldEpoch, int? newEpoch, string? reason)
    {
        var result = LogRecordParser.Parse(LogLines.RoleChange(Session, outcome, oldEpoch, newEpoch, reason));

        var change = Assert.IsType<RoleChangeRecord>(result.Record);
        Assert.Equal(outcome.ToUpperInvariant(), change.Outcome);
    }

    [Fact]
    public void ParsesEventAndNormalisesWarn()
    {
        var line = LogLines.Event(Session, "STALE_EPOCH_REJECTED", severity: "warn");

        var record = Assert.IsType<EventRecord>(LogRecordParser.Parse(line).Record);
        Assert.Equal("WARNING", record.Severity);
        Assert.Equal("STALE_EPOCH_REJECTED", record.EventType);
    }

    [Theory]
    [InlineData("   ")]
    [InlineData("")]
    public void BlankLinesAreNeitherRecordsNorErrors(string line)
    {
        Assert.True(LogRecordParser.Parse(line).IsBlank);
    }

    [Theory]
    [InlineData("{not json", "invalid JSON")]
    [InlineData("[1,2]", "not a JSON object")]
    [InlineData("""{"logged_by":"a"}""", "record_type")]
    [InlineData("""{"record_type":"gossip","logged_by":"a","logged_at":"2026-10-13T12:00:00Z"}""", "unknown record_type")]
    [InlineData("""{"record_type":"event","event_type":"X","logged_at":"2026-10-13T12:00:00Z"}""", "logged_by")]
    [InlineData("""{"record_type":"event","logged_by":"a","event_type":"X"}""", "timestamp")]
    [InlineData("""{"record_type":"event","logged_by":"a","logged_at":"yesterday","event_type":"X"}""", "logged_at")]
    [InlineData("""{"record_type":"event","logged_by":"a","logged_at":"2026-10-13T12:00:00Z","event_type":"X","severity":"LOUD"}""", "severity")]
    [InlineData("""{"record_type":"event","logged_by":"a","logged_at":"2026-10-13T12:00:00Z","event_type":"X","hierarchy_level":-1}""", "hierarchy_level")]
    [InlineData("""{"record_type":"event","logged_by":"a","logged_at":"2026-10-13T12:00:00Z","event_type":"X","hierarchy_level":"top"}""", "hierarchy_level")]
    [InlineData("""{"record_type":"event","logged_by":"a","logged_at":"2026-10-13T12:00:00Z","event_type":"X","detail":"text"}""", "detail")]
    [InlineData("""{"record_type":"state_snapshot","logged_by":"a","logged_at":"2026-10-13T12:00:00Z"}""", "state")]
    public void RejectsInvalidLinesWithTheFieldNamed(string line, string expected)
    {
        var result = LogRecordParser.Parse(line);

        Assert.Null(result.Record);
        Assert.Contains(expected, result.Error);
    }

    [Theory]
    [InlineData("\"direction\":\"sent\"", "\"direction\":\"sideways\"", "direction")]
    [InlineData("\"message_id\":\"m-1\",", "", "message.message_id")]
    [InlineData("\"recipient_id\":\"drone-cmd\",", "", "message.recipient_id")]
    [InlineData("\"role_epoch\":1", "\"role_epoch\":-1", "message.role_epoch")]
    [InlineData("\"role_epoch\":1", "\"role_epoch\":\"one\"", "message.role_epoch")]
    [InlineData("\"type\":\"HOLD\"", "\"type\":\"\"", "message.type")]
    [InlineData("\"payload\":{\"note\":\"m-1\"}", "\"payload\":\"text\"", "message.payload")]
    [InlineData("\"sender_id\":\"ground\"", "\"sender_id\":\"" + LongId + "\"", "message.sender_id")]
    public void RejectsInvalidMessages(string find, string replace, string expected)
    {
        var line = LogLines.Message(Session, "m-1").Replace(find, replace);
        Assert.NotEqual(LogLines.Message(Session, "m-1"), line);

        var result = LogRecordParser.Parse(line);

        Assert.Null(result.Record);
        Assert.Contains(expected, result.Error);
    }

    [Theory]
    [InlineData("granted", 2, 2, null, "new_epoch greater")]
    [InlineData("self_promoted", 2, null, null, "new_epoch greater")]
    [InlineData("stepped_down", 3, 1, null, "new_epoch greater")]
    [InlineData("denied", 1, 2, "BUSY", "must not change")]
    [InlineData("vacant", 1, 2, null, "must not change")]
    [InlineData("denied", 1, null, null, "requires a 'reason'")]
    [InlineData("promotion_declined", 1, null, null, "requires a 'reason'")]
    [InlineData("successor_accepted", 1, 1, null, "new_epoch greater")]
    [InlineData("voted", 1, 2, null, "unknown outcome")]
    public void RejectsRoleChangesThatBreakEpochRules(
        string outcome, int oldEpoch, int? newEpoch, string? reason, string expected)
    {
        var result = LogRecordParser.Parse(LogLines.RoleChange(Session, outcome, oldEpoch, newEpoch, reason));

        Assert.Null(result.Record);
        Assert.Contains(expected, result.Error);
    }

    private const string LongId =
        "0123456789012345678901234567890123456789012345678901234567890123456789";
}
