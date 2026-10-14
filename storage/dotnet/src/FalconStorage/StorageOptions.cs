namespace FalconStorage;

/// <summary>
/// Storage service settings, bound from the "Storage" configuration section.
/// The connection string comes from ConnectionStrings:FalconDb so it can be
/// supplied through the environment (ConnectionStrings__FalconDb).
/// </summary>
public sealed class StorageOptions
{
    public const string Section = "Storage";

    public string ConnectionString { get; set; } = string.Empty;

    /// <summary>Directory the agents write their JSONL exchange logs to. Empty disables ingestion.</summary>
    public string LogDirectory { get; set; } = string.Empty;

    /// <summary>File pattern for log files. The trailing wildcard also matches rotated copies.</summary>
    public string LogFilePattern { get; set; } = "*.jsonl*";

    /// <summary>Seconds between background ingestion passes. 0 disables the background loop.</summary>
    public int IngestIntervalSeconds { get; set; } = 10;

    /// <summary>Log lines written per database transaction.</summary>
    public int BatchSize { get; set; } = 500;

    /// <summary>Directory of SQL init scripts applied at startup. Empty skips schema setup.</summary>
    public string SchemaScriptsPath { get; set; } = string.Empty;
}
