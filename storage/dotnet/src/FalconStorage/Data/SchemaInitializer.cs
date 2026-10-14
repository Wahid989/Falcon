using System.Text.RegularExpressions;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Options;

namespace FalconStorage.Data;

/// <summary>
/// Applies the idempotent scripts in storage/sql/init at startup, so a fresh
/// SQL Server container needs no manual step before ingestion can run.
/// </summary>
internal sealed partial class SchemaInitializer
{
    /// <summary>Same sqlcmd variable scripts/init-db.sh sets with -v FalconDb=...</summary>
    internal const string DatabaseVariable = "$(FalconDb)";

    private readonly SqlConnectionFactory _connections;
    private readonly StorageOptions _options;
    private readonly ILogger<SchemaInitializer> _logger;

    public SchemaInitializer(
        SqlConnectionFactory connections,
        IOptions<StorageOptions> options,
        ILogger<SchemaInitializer> logger)
    {
        _connections = connections;
        _options = options.Value;
        _logger = logger;
    }

    public async Task ApplyAsync(CancellationToken cancellationToken)
    {
        var directory = _options.SchemaScriptsPath;
        if (string.IsNullOrWhiteSpace(directory))
        {
            return;
        }

        if (!Directory.Exists(directory))
        {
            throw new DirectoryNotFoundException($"Schema scripts directory not found: {directory}");
        }

        var database = DatabaseName(_connections.DatabaseName);

        // The scripts switch to the database themselves, so start on master; the
        // first script is the one that creates the database.
        await using var connection = _connections.CreateForServer();
        await connection.OpenAsync(cancellationToken);

        foreach (var script in Directory.GetFiles(directory, "*.sql").Order(StringComparer.Ordinal))
        {
            _logger.LogInformation("Applying schema script {Script}", Path.GetFileName(script));
            var text = await File.ReadAllTextAsync(script, cancellationToken);
            foreach (var batch in SplitBatches(text.Replace(DatabaseVariable, database)))
            {
                await using var command = new SqlCommand(batch, connection) { CommandTimeout = 120 };
                await command.ExecuteNonQueryAsync(cancellationToken);
            }
        }
    }

    /// <summary>
    /// The database name is spliced into the scripts as an identifier, so only
    /// plain names are allowed.
    /// </summary>
    internal static string DatabaseName(string name) =>
        PlainName().IsMatch(name)
            ? name
            : throw new InvalidOperationException(
                $"Database name '{name}' must contain only letters, digits and underscores");

    /// <summary>Splits a script on GO separators, which are a sqlcmd convention, not T-SQL.</summary>
    internal static IEnumerable<string> SplitBatches(string script) =>
        GoSeparator().Split(script).Where(batch => !string.IsNullOrWhiteSpace(batch));

    [GeneratedRegex(@"^\s*GO\s*$", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex GoSeparator();

    [GeneratedRegex("^[A-Za-z0-9_]{1,128}$")]
    private static partial Regex PlainName();
}
