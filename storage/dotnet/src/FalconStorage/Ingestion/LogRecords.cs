namespace FalconStorage.Ingestion;

/// <summary>Fields every exchange-log record carries.</summary>
internal abstract record LogRecord(
    string SessionId,
    string LoggedBy,
    DateTime Timestamp,
    int? HierarchyLevel);

internal sealed record MessageRecord(
    string SessionId,
    string LoggedBy,
    DateTime Timestamp,
    int? HierarchyLevel,
    DateTime? LoggedAt,
    string Direction,
    string MessageId,
    string SenderId,
    string ReceiverId,
    string MessageType,
    string? SenderRole,
    int RoleEpoch,
    string? SchemaVersion,
    string? Payload)
    : LogRecord(SessionId, LoggedBy, Timestamp, HierarchyLevel);

internal sealed record StateSnapshotRecord(
    string SessionId,
    string LoggedBy,
    DateTime Timestamp,
    int? HierarchyLevel,
    string AgentId,
    int? RoleEpoch,
    string? TriggerMessageId,
    string State)
    : LogRecord(SessionId, LoggedBy, Timestamp, HierarchyLevel);

internal sealed record RoleChangeRecord(
    string SessionId,
    string LoggedBy,
    DateTime Timestamp,
    int? HierarchyLevel,
    string RequesterId,
    string? TargetCommanderId,
    string? PreviousCommanderId,
    string? PreviousRole,
    string? NewRole,
    int OldEpoch,
    int? NewEpoch,
    string Outcome,
    string? Reason)
    : LogRecord(SessionId, LoggedBy, Timestamp, HierarchyLevel);

internal sealed record EventRecord(
    string SessionId,
    string LoggedBy,
    DateTime Timestamp,
    int? HierarchyLevel,
    string Severity,
    string EventType,
    string? Detail)
    : LogRecord(SessionId, LoggedBy, Timestamp, HierarchyLevel);

/// <summary>Result of parsing one line: a record, an error, or a blank line.</summary>
internal readonly record struct ParseResult(LogRecord? Record, string? Error)
{
    public static readonly ParseResult Blank = new(null, null);

    public bool IsBlank => Record is null && Error is null;

    public static ParseResult Ok(LogRecord record) => new(record, null);

    public static ParseResult Fail(string error) => new(null, error);
}
