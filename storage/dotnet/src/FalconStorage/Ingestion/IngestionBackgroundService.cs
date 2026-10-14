using FalconStorage.Data;
using Microsoft.Extensions.Options;

namespace FalconStorage.Ingestion;

/// <summary>
/// Applies the schema once SQL Server is reachable, then ingests the log
/// directory on a fixed interval.
/// </summary>
internal sealed class IngestionBackgroundService : BackgroundService
{
    private static readonly TimeSpan SchemaRetryDelay = TimeSpan.FromSeconds(5);

    private readonly SchemaInitializer _schema;
    private readonly LogIngester _ingester;
    private readonly SqlConnectionFactory _connections;
    private readonly StorageOptions _options;
    private readonly ILogger<IngestionBackgroundService> _logger;

    public IngestionBackgroundService(
        SchemaInitializer schema,
        LogIngester ingester,
        SqlConnectionFactory connections,
        IOptions<StorageOptions> options,
        ILogger<IngestionBackgroundService> logger)
    {
        _schema = schema;
        _ingester = ingester;
        _connections = connections;
        _options = options.Value;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!_connections.IsConfigured)
        {
            _logger.LogWarning("No database connection string configured; ingestion is disabled");
            return;
        }

        await ApplySchemaWithRetryAsync(stoppingToken);

        if (_options.IngestIntervalSeconds <= 0 || string.IsNullOrWhiteSpace(_options.LogDirectory))
        {
            _logger.LogInformation("Background ingestion is disabled");
            return;
        }

        var interval = TimeSpan.FromSeconds(_options.IngestIntervalSeconds);
        _logger.LogInformation(
            "Ingesting logs from {Directory} every {Interval}s", _options.LogDirectory, interval.TotalSeconds);

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var result = await _ingester.IngestAsync(stoppingToken);
                if (result.LinesRead > 0)
                {
                    _logger.LogInformation(
                        "Ingested {Lines} lines: {Messages} messages, {Snapshots} snapshots, "
                        + "{Handovers} handovers, {Events} events, {Duplicates} duplicates, {Rejected} rejected",
                        result.LinesRead, result.Messages, result.StateSnapshots, result.RoleHandovers,
                        result.Events, result.Duplicates, result.Rejected);
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogError(ex, "Ingestion pass failed; it will be retried");
            }

            try
            {
                await Task.Delay(interval, stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task ApplySchemaWithRetryAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await _schema.ApplyAsync(stoppingToken);
                return;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _logger.LogWarning("Database not ready ({Message}); retrying", ex.Message);
                await Task.Delay(SchemaRetryDelay, stoppingToken);
            }
        }
    }
}
