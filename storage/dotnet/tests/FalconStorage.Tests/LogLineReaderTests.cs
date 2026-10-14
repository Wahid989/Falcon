using System.Text;
using FalconStorage.Data;
using FalconStorage.Ingestion;
using Xunit;

namespace FalconStorage.Tests;

public class LogLineReaderTests
{
    private static List<LogLine> Read(string content, long offset = 0, long lineNumber = 0)
    {
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes(content));
        return LogLineReader.ReadCompleteLines(stream, offset, lineNumber).ToList();
    }

    private static string Text(LogLine line) => Encoding.UTF8.GetString(line.Bytes);

    [Fact]
    public void ReturnsEachLineWithItsEndOffset()
    {
        var lines = Read("ab\ncde\n");

        Assert.Equal(["ab", "cde"], lines.Select(Text));
        Assert.Equal([1L, 2L], lines.Select(l => l.LineNumber));
        Assert.Equal([3L, 7L], lines.Select(l => l.EndOffset));
    }

    [Fact]
    public void LeavesAnUnterminatedLastLineForLater()
    {
        var lines = Read("ab\ncd");

        Assert.Equal(["ab"], lines.Select(Text));
    }

    [Fact]
    public void StripsWindowsLineEndings()
    {
        Assert.Equal(["ab", ""], Read("ab\r\n\r\n").Select(Text));
    }

    [Fact]
    public void ResumesFromAnOffset()
    {
        var lines = Read("ab\ncde\nf\n", offset: 3, lineNumber: 1);

        Assert.Equal(["cde", "f"], lines.Select(Text));
        Assert.Equal([2L, 3L], lines.Select(l => l.LineNumber));
        Assert.Equal([7L, 9L], lines.Select(l => l.EndOffset));
    }

    [Fact]
    public void HandlesLinesLongerThanTheReadBuffer()
    {
        var longLine = new string('x', 200_000);

        var lines = Read(longLine + "\nshort\n");

        Assert.Equal(200_000, lines[0].Bytes.Length);
        Assert.Equal("short", Text(lines[1]));
        Assert.Equal(200_007, lines[1].EndOffset);
    }

    [Fact]
    public void KeepsMultiByteCharactersIntact()
    {
        Assert.Equal(["héllo ✓"], Read("héllo ✓\n").Select(Text));
    }

    [Theory]
    [InlineData("Falcon", true)]
    [InlineData("FalconTest_2", true)]
    [InlineData("Falcon]; DROP DATABASE x; --", false)]
    [InlineData("my db", false)]
    public void OnlyPlainDatabaseNamesAreAccepted(string name, bool accepted)
    {
        if (accepted)
        {
            Assert.Equal(name, SchemaInitializer.DatabaseName(name));
        }
        else
        {
            Assert.Throws<InvalidOperationException>(() => SchemaInitializer.DatabaseName(name));
        }
    }

    [Fact]
    public void SchemaScriptsSplitOnGoSeparators()
    {
        var batches = SchemaInitializer.SplitBatches("SELECT 1;\nGO\nSELECT 2;\r\n  go  \r\n\nGO\n").ToList();

        Assert.Equal(2, batches.Count);
        Assert.Contains("SELECT 1", batches[0]);
        Assert.Contains("SELECT 2", batches[1]);
    }
}
