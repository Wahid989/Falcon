using System.Globalization;
using System.Text.Json;

namespace FalconStorage.Ingestion;

/// <summary>
/// Turns one exchange-log line into a typed record, or a reason it was rejected.
///
/// Every check the database would enforce is repeated here. A line that would
/// violate a constraint is rejected on its own; otherwise one bad line would
/// roll back the whole batch it was in.
/// </summary>
internal static class LogRecordParser
{
    public const string DefaultSessionId = "default";

    private static readonly HashSet<string> RoleOutcomes = new(StringComparer.Ordinal)
    {
        "GRANTED", "DENIED", "SELF_PROMOTED", "STEPPED_DOWN", "PROMOTION_DECLINED", "VACANT",
    };

    private static readonly HashSet<string> EpochAdvancingOutcomes = new(StringComparer.Ordinal)
    {
        "GRANTED", "SELF_PROMOTED", "STEPPED_DOWN",
    };

    private static readonly HashSet<string> ReasonRequiredOutcomes = new(StringComparer.Ordinal)
    {
        "DENIED", "PROMOTION_DECLINED",
    };

    private static readonly HashSet<string> Severities = new(StringComparer.Ordinal)
    {
        "DEBUG", "INFO", "WARNING", "ERROR",
    };

    public static ParseResult Parse(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
        {
            return ParseResult.Blank;
        }

        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(line);
        }
        catch (JsonException ex)
        {
            return ParseResult.Fail($"invalid JSON: {ex.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return ParseResult.Fail("line is not a JSON object");
            }

            try
            {
                return ParseResult.Ok(ParseRecord(root));
            }
            catch (RecordException ex)
            {
                return ParseResult.Fail(ex.Message);
            }
            catch (InvalidOperationException ex)
            {
                // A string holding an escaped lone surrogate is valid JSON syntax
                // but cannot be read as text.
                return ParseResult.Fail($"unreadable value: {ex.Message}");
            }
        }
    }

    private static LogRecord ParseRecord(JsonElement root)
    {
        var recordType = OptionalString(root, "record_type", 32)
            ?? (root.TryGetProperty("message", out _) ? "message" : null)
            ?? throw new RecordException("missing field 'record_type'");

        var common = new Common(
            SessionId: OptionalString(root, "session_id", 64) ?? DefaultSessionId,
            LoggedBy: RequiredString(root, "logged_by", 64),
            LoggedAt: OptionalTimestamp(root, "logged_at"),
            HierarchyLevel: OptionalNonNegativeInt(root, "hierarchy_level"));

        return recordType switch
        {
            "message" => ParseMessage(root, common),
            "state_snapshot" => ParseStateSnapshot(root, common),
            "role_change" => ParseRoleChange(root, common),
            "event" => ParseEvent(root, common),
            _ => throw new RecordException($"unknown record_type '{recordType}'"),
        };
    }

    private static MessageRecord ParseMessage(JsonElement root, Common common)
    {
        var direction = RequiredString(root, "direction", 8).ToUpperInvariant();
        if (direction is not ("SENT" or "RECEIVED"))
        {
            throw new RecordException($"field 'direction' must be 'sent' or 'received', got '{direction}'");
        }

        if (!root.TryGetProperty("message", out var envelope) || envelope.ValueKind != JsonValueKind.Object)
        {
            throw new RecordException("missing object field 'message'");
        }

        var timestamp = OptionalTimestamp(envelope, "message.timestamp", "timestamp")
            ?? common.LoggedAt
            ?? throw new RecordException("missing field 'message.timestamp'");

        string? payload = null;
        if (envelope.TryGetProperty("payload", out var payloadElement)
            && payloadElement.ValueKind != JsonValueKind.Null)
        {
            if (payloadElement.ValueKind != JsonValueKind.Object)
            {
                throw new RecordException("field 'message.payload' must be a JSON object");
            }

            payload = payloadElement.GetRawText();
        }

        return new MessageRecord(
            common.SessionId,
            common.LoggedBy,
            timestamp,
            common.HierarchyLevel,
            common.LoggedAt,
            direction,
            MessageId: RequiredString(envelope, "message_id", 64, "message."),
            SenderId: RequiredString(envelope, "sender_id", 64, "message."),
            ReceiverId: RequiredString(envelope, "recipient_id", 64, "message."),
            MessageType: RequiredString(envelope, "type", 32, "message."),
            SenderRole: OptionalString(envelope, "sender_role", 16, "message."),
            RoleEpoch: RequiredNonNegativeInt(envelope, "role_epoch", "message."),
            SchemaVersion: OptionalVersion(envelope, "schema_version", "message."),
            payload);
    }

    private static StateSnapshotRecord ParseStateSnapshot(JsonElement root, Common common)
    {
        if (!root.TryGetProperty("state", out var state) || state.ValueKind != JsonValueKind.Object)
        {
            throw new RecordException("missing object field 'state'");
        }

        return new StateSnapshotRecord(
            common.SessionId,
            common.LoggedBy,
            EventTime(root, common),
            common.HierarchyLevel,
            AgentId: OptionalString(root, "agent_id", 64) ?? common.LoggedBy,
            RoleEpoch: OptionalNonNegativeInt(root, "role_epoch"),
            TriggerMessageId: OptionalString(root, "trigger_message_id", 64),
            State: state.GetRawText());
    }

    private static RoleChangeRecord ParseRoleChange(JsonElement root, Common common)
    {
        var outcome = RequiredString(root, "outcome", 24).ToUpperInvariant();
        if (!RoleOutcomes.Contains(outcome))
        {
            throw new RecordException($"unknown outcome '{outcome}'");
        }

        var oldEpoch = RequiredNonNegativeInt(root, "old_epoch");
        var newEpoch = OptionalNonNegativeInt(root, "new_epoch");
        var reason = OptionalString(root, "reason", 32);

        if (EpochAdvancingOutcomes.Contains(outcome))
        {
            if (newEpoch is null || newEpoch <= oldEpoch)
            {
                throw new RecordException($"outcome {outcome} requires new_epoch greater than old_epoch");
            }
        }
        else if (newEpoch is not null && newEpoch != oldEpoch)
        {
            throw new RecordException($"outcome {outcome} must not change the epoch");
        }

        if (ReasonRequiredOutcomes.Contains(outcome) && reason is null)
        {
            throw new RecordException($"outcome {outcome} requires a 'reason'");
        }

        return new RoleChangeRecord(
            common.SessionId,
            common.LoggedBy,
            EventTime(root, common),
            common.HierarchyLevel,
            RequesterId: RequiredString(root, "requester_id", 64),
            TargetCommanderId: OptionalString(root, "target_commander_id", 64),
            PreviousCommanderId: OptionalString(root, "previous_commander_id", 64),
            PreviousRole: OptionalString(root, "previous_role", 16),
            NewRole: OptionalString(root, "new_role", 16),
            oldEpoch,
            newEpoch,
            outcome,
            reason);
    }

    private static EventRecord ParseEvent(JsonElement root, Common common)
    {
        var severity = (OptionalString(root, "severity", 8) ?? "INFO").ToUpperInvariant();
        if (severity == "WARN")
        {
            severity = "WARNING";
        }

        if (!Severities.Contains(severity))
        {
            throw new RecordException($"unknown severity '{severity}'");
        }

        string? detail = null;
        if (root.TryGetProperty("detail", out var detailElement)
            && detailElement.ValueKind != JsonValueKind.Null)
        {
            if (detailElement.ValueKind != JsonValueKind.Object)
            {
                throw new RecordException("field 'detail' must be a JSON object");
            }

            detail = detailElement.GetRawText();
        }

        return new EventRecord(
            common.SessionId,
            common.LoggedBy,
            EventTime(root, common),
            common.HierarchyLevel,
            severity,
            EventType: RequiredString(root, "event_type", 64),
            detail);
    }

    /// <summary>Records other than messages use "timestamp" if present, else "logged_at".</summary>
    private static DateTime EventTime(JsonElement root, Common common) =>
        OptionalTimestamp(root, "timestamp")
        ?? common.LoggedAt
        ?? throw new RecordException("missing field 'timestamp' or 'logged_at'");

    private static string RequiredString(JsonElement obj, string name, int maxLength, string prefix = "") =>
        OptionalString(obj, name, maxLength, prefix)
        ?? throw new RecordException($"missing field '{prefix}{name}'");

    private static string? OptionalString(JsonElement obj, string name, int maxLength, string prefix = "")
    {
        if (!obj.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        if (value.ValueKind != JsonValueKind.String)
        {
            throw new RecordException($"field '{prefix}{name}' must be a string");
        }

        var text = value.GetString()!;
        if (text.Length == 0)
        {
            throw new RecordException($"field '{prefix}{name}' must not be empty");
        }

        if (text.Length > maxLength)
        {
            throw new RecordException($"field '{prefix}{name}' is longer than {maxLength} characters");
        }

        return text;
    }

    /// <summary>schema_version may be written as "1.0" or as a number.</summary>
    private static string? OptionalVersion(JsonElement obj, string name, string prefix)
    {
        const int maxLength = 16;
        if (obj.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number)
        {
            var text = value.GetRawText();
            return text.Length <= maxLength
                ? text
                : throw new RecordException($"field '{prefix}{name}' is longer than {maxLength} characters");
        }

        return OptionalString(obj, name, maxLength, prefix);
    }

    private static int RequiredNonNegativeInt(JsonElement obj, string name, string prefix = "") =>
        OptionalNonNegativeInt(obj, name, prefix)
        ?? throw new RecordException($"missing field '{prefix}{name}'");

    private static int? OptionalNonNegativeInt(JsonElement obj, string name, string prefix = "")
    {
        if (!obj.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var number))
        {
            throw new RecordException($"field '{prefix}{name}' must be an integer");
        }

        if (number < 0)
        {
            throw new RecordException($"field '{prefix}{name}' must not be negative");
        }

        return number;
    }

    /// <summary>Accepts ISO-8601 strings or Unix epoch seconds; returns UTC. No offset means UTC.</summary>
    private static DateTime? OptionalTimestamp(JsonElement obj, string label, string? name = null)
    {
        if (!obj.TryGetProperty(name ?? label, out var value) || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        if (value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var seconds))
        {
            try
            {
                return DateTime.UnixEpoch.AddSeconds(seconds);
            }
            catch (ArgumentOutOfRangeException)
            {
                throw new RecordException($"field '{label}' is out of range");
            }
        }

        if (value.ValueKind == JsonValueKind.String
            && DateTimeOffset.TryParse(
                value.GetString(),
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out var parsed))
        {
            return parsed.UtcDateTime;
        }

        throw new RecordException($"field '{label}' is not a valid timestamp");
    }

    private sealed record Common(string SessionId, string LoggedBy, DateTime? LoggedAt, int? HierarchyLevel);

    private sealed class RecordException(string message) : Exception(message);
}
