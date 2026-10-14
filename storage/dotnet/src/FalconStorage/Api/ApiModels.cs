using System.Text.Json;

namespace FalconStorage.Api;

public sealed record Page<T>(IReadOnlyList<T> Items, int Total, int Limit, int Offset);

public sealed record MessageDto(
    long Id,
    string SessionId,
    string MessageId,
    DateTime Timestamp,
    DateTime? LoggedAt,
    string LoggedBy,
    string SenderId,
    string ReceiverId,
    string Direction,
    string MessageType,
    string? SenderRole,
    int RoleEpoch,
    string? SchemaVersion,
    int? HierarchyLevel,
    JsonElement? Payload);

public sealed record HandoverDto(
    long Id,
    string SessionId,
    DateTime Timestamp,
    string LoggedBy,
    string RequesterId,
    string? TargetCommanderId,
    string? PreviousCommanderId,
    string? PreviousRole,
    string? NewRole,
    int OldEpoch,
    int? NewEpoch,
    string Outcome,
    string? Reason,
    int? HierarchyLevel);

public sealed record StateSnapshotDto(
    long Id,
    string SessionId,
    string AgentId,
    DateTime Timestamp,
    string LoggedBy,
    int? RoleEpoch,
    int? HierarchyLevel,
    string? TriggerMessageId,
    JsonElement State);

public sealed record SessionDto(
    string SessionId,
    DateTime? StartedAt,
    DateTime? EndedAt,
    string? Description);

public sealed record LevelCount(int? HierarchyLevel, int Count);

public sealed record TypeCount(string MessageType, int Count);

public sealed record SessionSummaryDto(
    string SessionId,
    DateTime? StartedAt,
    DateTime? EndedAt,
    int MessageCount,
    int StateSnapshotCount,
    int HandoverCount,
    int EventCount,
    int? MaxRoleEpoch,
    IReadOnlyList<string> Agents,
    IReadOnlyList<TypeCount> MessagesByType,
    IReadOnlyList<LevelCount> MessagesByHierarchyLevel);

/// <summary>
/// A hierarchy_level query value: absent (no filter), "null" (unknown level),
/// or a non-negative integer.
/// </summary>
public readonly record struct LevelFilter(bool Active, int? Level)
{
    public static bool TryParse(string? text, out LevelFilter filter)
    {
        filter = default;
        if (string.IsNullOrWhiteSpace(text))
        {
            return true;
        }

        if (text.Equals("null", StringComparison.OrdinalIgnoreCase))
        {
            filter = new LevelFilter(true, null);
            return true;
        }

        if (int.TryParse(text, out var level) && level >= 0)
        {
            filter = new LevelFilter(true, level);
            return true;
        }

        return false;
    }
}
