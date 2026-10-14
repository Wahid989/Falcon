namespace FalconStorage.Ingestion;

/// <summary>Counts from one ingestion pass. Only committed work is counted.</summary>
public sealed class IngestionResult
{
    public int FilesScanned { get; set; }

    public long LinesRead { get; set; }

    public long Messages { get; set; }

    public long StateSnapshots { get; set; }

    public long RoleHandovers { get; set; }

    public long Events { get; set; }

    /// <summary>Lines already in the database from an earlier pass (no new row).</summary>
    public long Duplicates { get; set; }

    /// <summary>Lines that could not be parsed; stored in IngestionRejects.</summary>
    public long Rejected { get; set; }

    public long BlankLines { get; set; }

    /// <summary>Files that could not be ingested this pass; they are retried on the next one.</summary>
    public List<FailedFile> FailedFiles { get; } = new();

    internal void Add(IngestionResult other)
    {
        LinesRead += other.LinesRead;
        Messages += other.Messages;
        StateSnapshots += other.StateSnapshots;
        RoleHandovers += other.RoleHandovers;
        Events += other.Events;
        Duplicates += other.Duplicates;
        Rejected += other.Rejected;
        BlankLines += other.BlankLines;
    }
}

public sealed record FailedFile(string File, string Error);
