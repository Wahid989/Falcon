// FALCON storage service.
//
// Phase 0 scaffolding (FALCON-006): an ASP.NET Core service with dependency
// injection, configuration / connection-string management, and a health-check
// endpoint. Log ingestion and the query API land later (FALCON-042); the
// connection string is read here but a live database check is not yet wired in.

var builder = WebApplication.CreateBuilder(args);

// --- Configuration / connection-string management ---------------------------
// The SQL Server connection string comes from configuration. Supply it via the
// environment (ConnectionStrings__FalconDb) so no secret is committed; see
// config/.env.example and docs/database.md.
var connectionString =
    builder.Configuration.GetConnectionString("FalconDb") ?? string.Empty;

// --- Dependency injection ----------------------------------------------------
builder.Services.AddHealthChecks();

// Expose the resolved connection string (without secrets) to DI so later
// ingestion/query services can depend on it.
builder.Services.AddSingleton(new StorageOptions(connectionString));

var app = builder.Build();

// --- Endpoints ---------------------------------------------------------------
app.MapGet("/", () => "FALCON storage service");

// Liveness probe used by Docker Compose and CI.
app.MapHealthChecks("/health");

app.Run();

/// <summary>
/// Strongly-typed storage configuration resolved at startup.
/// Carries the SQL Server connection string to downstream services.
/// </summary>
public sealed record StorageOptions(string ConnectionString);
