using FalconStorage.Ingestion;

namespace FalconStorage.Api;

internal static class ApiEndpoints
{
    private const int DefaultLimit = 100;
    private const int MaxLimit = 1000;

    public static void MapFalconApi(this WebApplication app)
    {
        var api = app.MapGroup("/api");

        api.MapGet("/messages", async (
                QueryRepository repository,
                string? sessionId, string? hierarchyLevel, string? senderId, string? receiverId,
                string? type, string? direction, int? limit, int? offset) =>
            {
                if (!LevelFilter.TryParse(hierarchyLevel, out var level))
                {
                    return InvalidLevel();
                }

                if (!TryPaging(limit, offset, out var take, out var skip, out var problem))
                {
                    return problem!;
                }

                return Results.Ok(await repository.GetMessagesAsync(
                    sessionId, level, senderId, receiverId, type, direction, take, skip));
            })
            .WithSummary("List exchange messages")
            .WithDescription(
                "Filter by session, hierarchy_level (an integer, or 'null' for messages whose sender "
                + "level was unknown), sender, receiver, message type and direction (sent/received).")
            .Produces<Page<MessageDto>>()
            .ProducesValidationProblem();

        api.MapGet("/handovers", async (
                QueryRepository repository,
                string? sessionId, string? hierarchyLevel, string? outcome, int? limit, int? offset) =>
            {
                if (!LevelFilter.TryParse(hierarchyLevel, out var level))
                {
                    return InvalidLevel();
                }

                if (!TryPaging(limit, offset, out var take, out var skip, out var problem))
                {
                    return problem!;
                }

                return Results.Ok(await repository.GetHandoversAsync(sessionId, level, outcome, take, skip));
            })
            .WithSummary("List role handovers")
            .WithDescription("Grants, denials, self-promotions, step-downs, declined promotions and vacancies.")
            .Produces<Page<HandoverDto>>()
            .ProducesValidationProblem();

        api.MapGet("/state/{agentId}/history", async (
                QueryRepository repository, string agentId, string? sessionId, int? limit, int? offset) =>
            {
                if (string.IsNullOrWhiteSpace(agentId))
                {
                    return Results.ValidationProblem(new Dictionary<string, string[]>
                    {
                        ["agentId"] = ["Must not be blank."],
                    });
                }

                if (!TryPaging(limit, offset, out var take, out var skip, out var problem))
                {
                    return problem!;
                }

                return Results.Ok(await repository.GetStateHistoryAsync(agentId, sessionId, take, skip));
            })
            .WithSummary("State snapshots for one agent, oldest first")
            .Produces<Page<StateSnapshotDto>>()
            .ProducesValidationProblem();

        api.MapGet("/sessions", async (QueryRepository repository) =>
                Results.Ok(await repository.GetSessionsAsync()))
            .WithSummary("List sessions, newest first")
            .Produces<IReadOnlyList<SessionDto>>();

        api.MapGet("/session/{id}/summary", async (QueryRepository repository, string id) =>
                await repository.GetSessionSummaryAsync(id) is { } summary
                    ? Results.Ok(summary)
                    : Results.NotFound())
            .WithSummary("Counts and participants for one session")
            .Produces<SessionSummaryDto>()
            .Produces(StatusCodes.Status404NotFound);

        api.MapPost("/ingest", async (LogIngester ingester, CancellationToken cancellationToken) =>
                Results.Ok(await ingester.IngestAsync(cancellationToken)))
            .WithSummary("Ingest the log directory now")
            .WithDescription("Runs the same pass as the background loop. Safe to call repeatedly.")
            .Produces<IngestionResult>();
    }

    private static IResult InvalidLevel() =>
        Results.ValidationProblem(new Dictionary<string, string[]>
        {
            ["hierarchyLevel"] = ["Must be a non-negative integer or 'null'."],
        });

    private static bool TryPaging(int? limit, int? offset, out int take, out int skip, out IResult? problem)
    {
        take = limit ?? DefaultLimit;
        skip = offset ?? 0;
        problem = null;

        var errors = new Dictionary<string, string[]>();
        if (take is < 1 or > MaxLimit)
        {
            errors["limit"] = [$"Must be between 1 and {MaxLimit}."];
        }

        if (skip < 0)
        {
            errors["offset"] = ["Must not be negative."];
        }

        if (errors.Count > 0)
        {
            problem = Results.ValidationProblem(errors);
            return false;
        }

        return true;
    }
}
