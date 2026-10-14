using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Xunit;

namespace FalconStorage.Tests;

[Collection("database")]
public class ApiTests
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    private static WebApplicationFactory<Program> App(
        string logDirectory = "", string? connectionString = null, int intervalSeconds = 0) =>
        new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseSetting(
                "ConnectionStrings:FalconDb", connectionString ?? TestDatabase.ConnectionString ?? string.Empty);
            builder.UseSetting("Storage:LogDirectory", logDirectory);
            builder.UseSetting("Storage:IngestIntervalSeconds", intervalSeconds.ToString());
            builder.UseSetting("Storage:SchemaScriptsPath", string.Empty);
        });

    /// <summary>Ingests one session covering every record type through the API.</summary>
    private static async Task<(HttpClient Client, string Session, TempLogDirectory Logs)> SeededAsync()
    {
        await TestDatabase.EnsureSchemaAsync();
        var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append(
            "ground.jsonl",
            // Some records are written out of time order, so ordering by file
            // position instead of timestamp would fail the tests.
            LogLines.Message(session, "m-2", "sent", level: 1, loggedBy: "drone-cmd", sender: "drone-cmd",
                receiver: "drone-a", type: "PROCEED_TO_WAYPOINT", second: 2),
            LogLines.Message(session, "m-1", "sent", level: 0, second: 1),
            LogLines.Message(session, "m-3", "received", level: null, loggedBy: "drone-a", sender: "stranger",
                receiver: "drone-a", second: 3),
            LogLines.StateSnapshot(session, "drone-a", battery: 60, second: 5),
            LogLines.StateSnapshot(session, "drone-a", battery: 90, second: 4),
            LogLines.RoleChange(session, "granted", 1, 2, second: 6),
            LogLines.RoleChange(session, "denied", 2, null, "BUSY", second: 7),
            LogLines.Event(session, "STALE_EPOCH_REJECTED", second: 8));

        var client = App(logs.Path).CreateClient();
        var ingest = await client.PostAsync("/api/ingest", null);
        ingest.EnsureSuccessStatusCode();
        return (client, session, logs);
    }

    private static async Task<JsonElement> GetJsonAsync(HttpClient client, string url)
    {
        var response = await client.GetAsync(url);
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        return await response.Content.ReadFromJsonAsync<JsonElement>(Json);
    }

    private static List<string> Ids(JsonElement page, string field) =>
        page.GetProperty("items").EnumerateArray().Select(i => i.GetProperty(field).ToString()).ToList();

    [Fact]
    public async Task HealthEndpointReportsHealthyWithoutADatabase()
    {
        using var app = App(connectionString: string.Empty);
        var response = await app.CreateClient().GetAsync("/health");

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("Healthy", await response.Content.ReadAsStringAsync());
    }

    [Fact]
    public async Task OpenApiDocumentDescribesEveryEndpoint()
    {
        using var app = App();
        var document = await GetJsonAsync(app.CreateClient(), "/swagger/v1/swagger.json");

        var paths = document.GetProperty("paths").EnumerateObject().Select(p => p.Name).ToList();
        Assert.Contains("/api/messages", paths);
        Assert.Contains("/api/handovers", paths);
        Assert.Contains("/api/state/{agentId}/history", paths);
        Assert.Contains("/api/session/{id}/summary", paths);
        Assert.Contains("/api/sessions", paths);
        Assert.Contains("/api/ingest", paths);

        var parameters = document.GetProperty("paths").GetProperty("/api/messages").GetProperty("get")
            .GetProperty("parameters").EnumerateArray().Select(p => p.GetProperty("name").GetString());
        Assert.Contains("hierarchyLevel", parameters);
    }

    [SkippableFact]
    public async Task MessagesCanBeFilteredByHierarchyLevel()
    {
        TestDatabase.SkipIfUnavailable();
        var (client, session, logs) = await SeededAsync();
        using var _ = logs;

        var top = await GetJsonAsync(client, $"/api/messages?sessionId={session}&hierarchyLevel=0");
        var middle = await GetJsonAsync(client, $"/api/messages?sessionId={session}&hierarchyLevel=1");
        var unknown = await GetJsonAsync(client, $"/api/messages?sessionId={session}&hierarchyLevel=null");
        var all = await GetJsonAsync(client, $"/api/messages?sessionId={session}");

        Assert.Equal(["m-1"], Ids(top, "messageId"));
        Assert.Equal(["m-2"], Ids(middle, "messageId"));
        Assert.Equal(["m-3"], Ids(unknown, "messageId"));
        Assert.Equal(3, all.GetProperty("total").GetInt32());
        Assert.Equal(["m-1", "m-2", "m-3"], Ids(all, "messageId"));
        Assert.Equal("m-1", all.GetProperty("items")[0].GetProperty("payload").GetProperty("note").GetString());
    }

    [SkippableFact]
    public async Task MessagesCanBeFilteredAndPaged()
    {
        TestDatabase.SkipIfUnavailable();
        var (client, session, logs) = await SeededAsync();
        using var _ = logs;

        var byType = await GetJsonAsync(client, $"/api/messages?sessionId={session}&type=PROCEED_TO_WAYPOINT");
        var received = await GetJsonAsync(client, $"/api/messages?sessionId={session}&direction=received");
        var secondPage = await GetJsonAsync(client, $"/api/messages?sessionId={session}&limit=1&offset=1");

        Assert.Equal(["m-2"], Ids(byType, "messageId"));
        Assert.Equal(["m-3"], Ids(received, "messageId"));
        Assert.Equal(["m-2"], Ids(secondPage, "messageId"));
        Assert.Equal(3, secondPage.GetProperty("total").GetInt32());
    }

    [SkippableFact]
    public async Task HandoversAreListedAndFilterable()
    {
        TestDatabase.SkipIfUnavailable();
        var (client, session, logs) = await SeededAsync();
        using var _ = logs;

        var all = await GetJsonAsync(client, $"/api/handovers?sessionId={session}");
        var denied = await GetJsonAsync(client, $"/api/handovers?sessionId={session}&outcome=denied");

        Assert.Equal(["GRANTED", "DENIED"], Ids(all, "outcome"));
        Assert.Equal(["BUSY"], Ids(denied, "reason"));
    }

    [SkippableFact]
    public async Task StateHistoryIsReturnedOldestFirst()
    {
        TestDatabase.SkipIfUnavailable();
        var (client, session, logs) = await SeededAsync();
        using var _ = logs;

        var history = await GetJsonAsync(client, $"/api/state/drone-a/history?sessionId={session}");

        var batteries = history.GetProperty("items").EnumerateArray()
            .Select(i => i.GetProperty("state").GetProperty("battery").GetInt32());
        Assert.Equal([90, 60], batteries);
    }

    [SkippableFact]
    public async Task SessionSummaryCountsEverything()
    {
        TestDatabase.SkipIfUnavailable();
        var (client, session, logs) = await SeededAsync();
        using var _ = logs;

        var summary = await GetJsonAsync(client, $"/api/session/{session}/summary");

        Assert.Equal(3, summary.GetProperty("messageCount").GetInt32());
        Assert.Equal(2, summary.GetProperty("stateSnapshotCount").GetInt32());
        Assert.Equal(2, summary.GetProperty("handoverCount").GetInt32());
        Assert.Equal(1, summary.GetProperty("eventCount").GetInt32());
        Assert.Equal(2, summary.GetProperty("maxRoleEpoch").GetInt32());
        var agents = summary.GetProperty("agents").EnumerateArray().Select(a => a.GetString()).ToList();
        Assert.Equal(["drone-a", "drone-cmd", "ground", "stranger"], agents);
        var levels = summary.GetProperty("messagesByHierarchyLevel").EnumerateArray()
            .Select(l => l.GetProperty("hierarchyLevel").ValueKind == JsonValueKind.Null
                ? "null"
                : l.GetProperty("hierarchyLevel").GetInt32().ToString())
            .ToList();
        Assert.Equal(["null", "0", "1"], levels);

        var sessions = await client.GetFromJsonAsync<JsonElement>("/api/sessions", Json);
        Assert.Contains(sessions.EnumerateArray(), s => s.GetProperty("sessionId").GetString() == session);
    }

    [SkippableFact]
    public async Task BackgroundLoopIngestsWithoutBeingAsked()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var logs = new TempLogDirectory();
        var session = LogLines.NewSession();
        logs.Append("ground.jsonl", LogLines.Message(session, "m-1"));
        using var app = App(logs.Path, intervalSeconds: 1);
        var client = app.CreateClient();

        var total = 0;
        for (var attempt = 0; attempt < 50 && total == 0; attempt++)
        {
            await Task.Delay(200);
            var page = await GetJsonAsync(client, $"/api/messages?sessionId={session}");
            total = page.GetProperty("total").GetInt32();
        }

        Assert.Equal(1, total);
    }

    [SkippableFact]
    public async Task UnknownSessionSummaryIsNotFound()
    {
        TestDatabase.SkipIfUnavailable();
        await TestDatabase.EnsureSchemaAsync();
        using var app = App();

        var response = await app.CreateClient().GetAsync($"/api/session/{LogLines.NewSession()}/summary");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Theory]
    [InlineData("/api/messages?hierarchyLevel=-1")]
    [InlineData("/api/messages?hierarchyLevel=top")]
    [InlineData("/api/handovers?hierarchyLevel=1.5")]
    [InlineData("/api/messages?limit=0")]
    [InlineData("/api/messages?limit=5000")]
    [InlineData("/api/state/a/history?offset=-1")]
    public async Task InvalidQueryValuesAreRejected(string url)
    {
        using var app = App();

        var response = await app.CreateClient().GetAsync(url);

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }
}
