// FALCON storage service.
//
// Ingests the agents' JSONL exchange logs into SQL Server and exposes them
// through a read-only REST API. The Python runtime never calls this service;
// the log files are the only link between the two.

using FalconStorage;
using FalconStorage.Api;
using FalconStorage.Data;
using FalconStorage.Ingestion;

var builder = WebApplication.CreateBuilder(args);

// The connection string comes from ConnectionStrings:FalconDb. Supply it via the
// environment (ConnectionStrings__FalconDb) so no secret is committed; see
// config/.env.example.
builder.Services.Configure<StorageOptions>(builder.Configuration.GetSection(StorageOptions.Section));
builder.Services.PostConfigure<StorageOptions>(options =>
{
    if (string.IsNullOrWhiteSpace(options.ConnectionString))
    {
        options.ConnectionString = builder.Configuration.GetConnectionString("FalconDb") ?? string.Empty;
    }
});

builder.Services.AddSingleton<SqlConnectionFactory>();
builder.Services.AddSingleton<SchemaInitializer>();
builder.Services.AddSingleton<LogIngester>();
builder.Services.AddSingleton<QueryRepository>();
builder.Services.AddHostedService<IngestionBackgroundService>();

builder.Services.AddHealthChecks();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(options =>
{
    options.SwaggerDoc("v1", new() { Title = "FALCON Storage API", Version = "v1" });
    var xml = Path.Combine(AppContext.BaseDirectory, "FalconStorage.xml");
    if (File.Exists(xml))
    {
        options.IncludeXmlComments(xml);
    }
});

var app = builder.Build();

app.MapGet("/", () => "FALCON storage service").ExcludeFromDescription();

// Liveness probe used by Docker Compose and CI.
app.MapHealthChecks("/health");

app.UseSwagger();
app.UseSwaggerUI();

app.MapFalconApi();

app.Run();

/// <summary>Entry point; public so integration tests can host the app.</summary>
public partial class Program
{
}
