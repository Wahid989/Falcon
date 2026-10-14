using System.Data;
using Dapper;
using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Options;

namespace FalconStorage.Data;

internal sealed class SqlConnectionFactory
{
    private readonly string _connectionString;

    static SqlConnectionFactory()
    {
        // SqlClient sends DateTime as the legacy DATETIME type by default, which
        // rounds to 3.33 ms. The schema uses DATETIME2(6), so keep full precision.
        SqlMapper.AddTypeMap(typeof(DateTime), DbType.DateTime2);
        SqlMapper.AddTypeMap(typeof(DateTime?), DbType.DateTime2);
    }

    public SqlConnectionFactory(IOptions<StorageOptions> options)
    {
        _connectionString = options.Value.ConnectionString;
    }

    public bool IsConfigured => !string.IsNullOrWhiteSpace(_connectionString);

    /// <summary>The database named in the connection string (Falcon unless configured otherwise).</summary>
    public string DatabaseName =>
        new SqlConnectionStringBuilder(_connectionString).InitialCatalog is { Length: > 0 } name ? name : "Falcon";

    public SqlConnection Create() => new(_connectionString);

    /// <summary>A connection to master, for scripts that create the database itself.</summary>
    public SqlConnection CreateForServer() =>
        new(new SqlConnectionStringBuilder(_connectionString) { InitialCatalog = "master" }.ConnectionString);
}
