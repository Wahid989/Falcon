namespace FalconStorage.Ingestion;

/// <param name="Bytes">Line content without the line terminator.</param>
/// <param name="LineNumber">1-based line number within the file.</param>
/// <param name="EndOffset">Byte offset just past this line's newline.</param>
internal readonly record struct LogLine(byte[] Bytes, long LineNumber, long EndOffset);

internal static class LogLineReader
{
    /// <summary>
    /// Reads newline-terminated lines starting at <paramref name="startOffset"/>.
    /// A trailing line without a newline is not returned: the writer may still be
    /// in the middle of it, so it is picked up on a later pass.
    /// </summary>
    public static IEnumerable<LogLine> ReadCompleteLines(
        Stream stream, long startOffset, long startLineNumber)
    {
        stream.Seek(startOffset, SeekOrigin.Begin);

        var buffer = new byte[64 * 1024];
        var pending = new MemoryStream();
        long position = startOffset;
        long lineNumber = startLineNumber;
        int read;

        while ((read = stream.Read(buffer, 0, buffer.Length)) > 0)
        {
            int segmentStart = 0;
            for (int i = 0; i < read; i++)
            {
                if (buffer[i] != (byte)'\n')
                {
                    continue;
                }

                pending.Write(buffer, segmentStart, i - segmentStart);
                segmentStart = i + 1;

                var bytes = pending.ToArray();
                pending.SetLength(0);
                if (bytes.Length > 0 && bytes[^1] == (byte)'\r')
                {
                    bytes = bytes[..^1];
                }

                lineNumber++;
                yield return new LogLine(bytes, lineNumber, position + i + 1);
            }

            pending.Write(buffer, segmentStart, read - segmentStart);
            position += read;
        }
    }
}
