using System.Text.Json;
using Dapper;
using FalconStorage.Data;

namespace FalconStorage.Api;

/// <summary>Read-only queries behind the REST API.</summary>
internal sealed class QueryRepository
{
    private readonly SqlConnectionFactory _connections;

    public QueryRepository(SqlConnectionFactory connections)
    {
        _connections = connections;
    }

    public async Task<Page<MessageDto>> GetMessagesAsync(
        string? sessionId, LevelFilter level, string? senderId, string? receiverId,
        string? messageType, string? direction, int limit, int offset)
    {
        var filter = new Filter()
            .Equal("SessionId", sessionId)
            .Level(level)
            .Equal("SenderId", senderId)
            .Equal("ReceiverId", receiverId)
            .Equal("MessageType", messageType)
            .Equal("Direction", direction?.ToUpperInvariant());

        var (rows, total) = await PageAsync<MessageRow>(
            "SELECT * FROM dbo.Messages", filter, "Timestamp, Id", limit, offset);

        var items = rows.Select(r => new MessageDto(
            r.Id, r.SessionId, r.MessageId, Utc(r.Timestamp), Utc(r.LoggedAt), r.LoggedBy, r.SenderId,
            r.ReceiverId, r.Direction, r.MessageType, r.SenderRole, r.RoleEpoch, r.SchemaVersion,
            r.HierarchyLevel, Json(r.Payload))).ToList();
        return new Page<MessageDto>(items, total, limit, offset);
    }

    public async Task<Page<HandoverDto>> GetHandoversAsync(
        string? sessionId, LevelFilter level, string? outcome, int limit, int offset)
    {
        var filter = new Filter()
            .Equal("SessionId", sessionId)
            .Level(level)
            .Equal("Outcome", outcome?.ToUpperInvariant());

        var (rows, total) = await PageAsync<HandoverRow>(
            "SELECT * FROM dbo.RoleHandovers", filter, "Timestamp, Id", limit, offset);

        var items = rows.Select(r => new HandoverDto(
            r.Id, r.SessionId, Utc(r.Timestamp), r.LoggedBy, r.RequesterId, r.TargetCommanderId,
            r.PreviousCommanderId, r.PreviousRole, r.NewRole, r.OldEpoch, r.NewEpoch, r.Outcome,
            r.Reason, r.HierarchyLevel)).ToList();
        return new Page<HandoverDto>(items, total, limit, offset);
    }

    public async Task<Page<StateSnapshotDto>> GetStateHistoryAsync(
        string agentId, string? sessionId, int limit, int offset)
    {
        var filter = new Filter()
            .Equal("AgentId", agentId)
            .Equal("SessionId", sessionId);

        var (rows, total) = await PageAsync<StateRow>(
            "SELECT * FROM dbo.StateSnapshots", filter, "Timestamp, Id", limit, offset);

        var items = rows.Select(r => new StateSnapshotDto(
            r.Id, r.SessionId, r.AgentId, Utc(r.Timestamp), r.LoggedBy, r.RoleEpoch, r.HierarchyLevel,
            r.TriggerMessageId, Json(r.State)!.Value)).ToList();
        return new Page<StateSnapshotDto>(items, total, limit, offset);
    }

    public async Task<IReadOnlyList<SessionDto>> GetSessionsAsync()
    {
        await using var connection = _connections.Create();
        var rows = await connection.QueryAsync<SessionRow>(
            "SELECT SessionId, StartedAt, EndedAt, Description FROM dbo.Sessions ORDER BY StartedAt DESC, SessionId");
        return rows.Select(r => new SessionDto(r.SessionId, Utc(r.StartedAt), Utc(r.EndedAt), r.Description))
            .ToList();
    }

    public async Task<SessionSummaryDto?> GetSessionSummaryAsync(string sessionId)
    {
        await using var connection = _connections.Create();
        using var results = await connection.QueryMultipleAsync(
            """
            SELECT SessionId, StartedAt, EndedAt, Description FROM dbo.Sessions WHERE SessionId = @sessionId;

            SELECT
                (SELECT COUNT(*) FROM dbo.Messages WHERE SessionId = @sessionId) AS MessageCount,
                (SELECT COUNT(*) FROM dbo.StateSnapshots WHERE SessionId = @sessionId) AS StateSnapshotCount,
                (SELECT COUNT(*) FROM dbo.RoleHandovers WHERE SessionId = @sessionId) AS HandoverCount,
                (SELECT COUNT(*) FROM dbo.Events WHERE SessionId = @sessionId) AS EventCount,
                (SELECT MAX(Epoch) FROM (
                    SELECT MAX(RoleEpoch) FROM dbo.Messages WHERE SessionId = @sessionId
                    UNION ALL
                    SELECT MAX(NewEpoch) FROM dbo.RoleHandovers WHERE SessionId = @sessionId
                    UNION ALL
                    SELECT MAX(RoleEpoch) FROM dbo.StateSnapshots WHERE SessionId = @sessionId
                ) AS Epochs (Epoch)) AS MaxRoleEpoch;

            SELECT AgentId FROM (
                SELECT LoggedBy AS AgentId FROM dbo.Messages WHERE SessionId = @sessionId
                UNION SELECT SenderId FROM dbo.Messages WHERE SessionId = @sessionId
                UNION SELECT ReceiverId FROM dbo.Messages WHERE SessionId = @sessionId
                UNION SELECT AgentId FROM dbo.StateSnapshots WHERE SessionId = @sessionId
                UNION SELECT LoggedBy FROM dbo.StateSnapshots WHERE SessionId = @sessionId
                UNION SELECT LoggedBy FROM dbo.RoleHandovers WHERE SessionId = @sessionId
                UNION SELECT RequesterId FROM dbo.RoleHandovers WHERE SessionId = @sessionId
                UNION SELECT TargetCommanderId FROM dbo.RoleHandovers WHERE SessionId = @sessionId
                UNION SELECT PreviousCommanderId FROM dbo.RoleHandovers WHERE SessionId = @sessionId
                UNION SELECT LoggedBy FROM dbo.Events WHERE SessionId = @sessionId
            ) AS Agents
            WHERE AgentId IS NOT NULL
            ORDER BY AgentId;

            SELECT MessageType, COUNT(*) AS Count FROM dbo.Messages
            WHERE SessionId = @sessionId GROUP BY MessageType ORDER BY MessageType;

            SELECT HierarchyLevel, COUNT(*) AS Count FROM dbo.Messages
            WHERE SessionId = @sessionId GROUP BY HierarchyLevel ORDER BY HierarchyLevel;
            """,
            new { sessionId });

        var session = await results.ReadSingleOrDefaultAsync<SessionRow>();
        if (session is null)
        {
            return null;
        }

        var counts = await results.ReadSingleAsync<CountsRow>();
        var agents = (await results.ReadAsync<string>()).ToList();
        var byType = (await results.ReadAsync<TypeCountRow>())
            .Select(r => new TypeCount(r.MessageType, r.Count)).ToList();
        var byLevel = (await results.ReadAsync<LevelCountRow>())
            .Select(r => new LevelCount(r.HierarchyLevel, r.Count)).ToList();

        return new SessionSummaryDto(
            session.SessionId, Utc(session.StartedAt), Utc(session.EndedAt),
            counts.MessageCount, counts.StateSnapshotCount, counts.HandoverCount, counts.EventCount,
            counts.MaxRoleEpoch, agents, byType, byLevel);
    }

    private async Task<(List<T> Rows, int Total)> PageAsync<T>(
        string select, Filter filter, string orderBy, int limit, int offset)
    {
        await using var connection = _connections.Create();
        var where = filter.WhereClause;
        filter.Parameters.Add("limit", limit);
        filter.Parameters.Add("offset", offset);

        using var results = await connection.QueryMultipleAsync(
            $"""
            {select} {where} ORDER BY {orderBy} OFFSET @offset ROWS FETCH NEXT @limit ROWS ONLY;
            SELECT COUNT(*) FROM ({select} {where}) AS Filtered;
            """,
            filter.Parameters);

        var rows = (await results.ReadAsync<T>()).ToList();
        var total = await results.ReadSingleAsync<int>();
        return (rows, total);
    }

    private static DateTime Utc(DateTime value) => DateTime.SpecifyKind(value, DateTimeKind.Utc);

    private static DateTime? Utc(DateTime? value) => value is null ? null : Utc(value.Value);

    private static JsonElement? Json(string? text)
    {
        if (text is null)
        {
            return null;
        }

        using var document = JsonDocument.Parse(text);
        return document.RootElement.Clone();
    }

    /// <summary>Builds a parameterized WHERE clause. Column names are fixed by the callers, never user input.</summary>
    private sealed class Filter
    {
        private readonly List<string> _conditions = new();

        public DynamicParameters Parameters { get; } = new();

        public string WhereClause =>
            _conditions.Count == 0 ? string.Empty : "WHERE " + string.Join(" AND ", _conditions);

        public Filter Equal(string column, string? value)
        {
            if (!string.IsNullOrWhiteSpace(value))
            {
                _conditions.Add($"{column} = @{column}");
                Parameters.Add(column, value);
            }

            return this;
        }

        public Filter Level(LevelFilter level)
        {
            if (!level.Active)
            {
                return this;
            }

            if (level.Level is null)
            {
                _conditions.Add("HierarchyLevel IS NULL");
            }
            else
            {
                _conditions.Add("HierarchyLevel = @HierarchyLevel");
                Parameters.Add("HierarchyLevel", level.Level);
            }

            return this;
        }
    }

    private sealed class MessageRow
    {
        public long Id { get; set; }
        public string SessionId { get; set; } = "";
        public string MessageId { get; set; } = "";
        public DateTime Timestamp { get; set; }
        public DateTime? LoggedAt { get; set; }
        public string LoggedBy { get; set; } = "";
        public string SenderId { get; set; } = "";
        public string ReceiverId { get; set; } = "";
        public string Direction { get; set; } = "";
        public string MessageType { get; set; } = "";
        public string? SenderRole { get; set; }
        public int RoleEpoch { get; set; }
        public string? SchemaVersion { get; set; }
        public int? HierarchyLevel { get; set; }
        public string? Payload { get; set; }
    }

    private sealed class HandoverRow
    {
        public long Id { get; set; }
        public string SessionId { get; set; } = "";
        public DateTime Timestamp { get; set; }
        public string LoggedBy { get; set; } = "";
        public string RequesterId { get; set; } = "";
        public string? TargetCommanderId { get; set; }
        public string? PreviousCommanderId { get; set; }
        public string? PreviousRole { get; set; }
        public string? NewRole { get; set; }
        public int OldEpoch { get; set; }
        public int? NewEpoch { get; set; }
        public string Outcome { get; set; } = "";
        public string? Reason { get; set; }
        public int? HierarchyLevel { get; set; }
    }

    private sealed class StateRow
    {
        public long Id { get; set; }
        public string SessionId { get; set; } = "";
        public string AgentId { get; set; } = "";
        public DateTime Timestamp { get; set; }
        public string LoggedBy { get; set; } = "";
        public int? RoleEpoch { get; set; }
        public int? HierarchyLevel { get; set; }
        public string? TriggerMessageId { get; set; }
        public string State { get; set; } = "{}";
    }

    private sealed class SessionRow
    {
        public string SessionId { get; set; } = "";
        public DateTime? StartedAt { get; set; }
        public DateTime? EndedAt { get; set; }
        public string? Description { get; set; }
    }

    private sealed class CountsRow
    {
        public int MessageCount { get; set; }
        public int StateSnapshotCount { get; set; }
        public int HandoverCount { get; set; }
        public int EventCount { get; set; }
        public int? MaxRoleEpoch { get; set; }
    }

    private sealed class TypeCountRow
    {
        public string MessageType { get; set; } = "";
        public int Count { get; set; }
    }

    private sealed class LevelCountRow
    {
        public int? HierarchyLevel { get; set; }
        public int Count { get; set; }
    }
}
