using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.AnalysisServices.Tabular;

// Structural + integrity validation of a hand-authored TMDL semantic model, using the
// same TOM TMDL parser Power BI Desktop uses (TmdlSerializer.DeserializeDatabaseFromFolder).
// Adds the integrity checks TmdlSerializer itself does NOT catch (per pbi-semantic-builder DoD):
//   (a) model-wide duplicate measure names
//   (b) a measure name equal to a column name in the same table
// Usage: dotnet run -- "<path to ...SemanticModel\definition>"
class Program
{
    static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: tmdl_validate <definitionFolder>");
            return 2;
        }
        string folder = args[0];
        Database db;
        try
        {
            db = TmdlSerializer.DeserializeDatabaseFromFolder(folder);
        }
        catch (Exception ex)
        {
            Console.WriteLine("TMDL DESERIALIZE: FAILED");
            Console.WriteLine(ex.GetType().FullName + ": " + ex.Message);
            if (ex.InnerException != null)
                Console.WriteLine("  inner: " + ex.InnerException.Message);
            return 1;
        }

        Model m = db.Model;
        int tables = m.Tables.Count;
        int cols = m.Tables.Sum(t => t.Columns.Count(c => c.Type != ColumnType.RowNumber));
        int calcCols = m.Tables.Sum(t => t.Columns.Count(c => c is CalculatedColumn));
        int measures = m.Tables.Sum(t => t.Measures.Count);
        int rels = m.Relationships.Count;
        Console.WriteLine("TMDL DESERIALIZE: OK");
        Console.WriteLine($"  compatibilityLevel = {db.CompatibilityLevel}");
        Console.WriteLine($"  tables       = {tables}");
        Console.WriteLine($"  columns      = {cols}  (of which calculated = {calcCols})");
        Console.WriteLine($"  measures     = {measures}");
        Console.WriteLine($"  relationships= {rels}");
        foreach (var t in m.Tables.OrderBy(t => t.Name))
        {
            int nc = t.Columns.Count(c => c.Type != ColumnType.RowNumber);
            int mc = t.Measures.Count;
            string part = t.Partitions.Count > 0 ? t.Partitions[0].SourceType.ToString() : "none";
            Console.WriteLine($"    - {t.Name,-28} cols={nc,-3} measures={mc,-3} partition={part}");
        }
        foreach (var r in m.Relationships.OfType<SingleColumnRelationship>())
        {
            Console.WriteLine($"    REL {r.FromTable.Name}[{r.FromColumn.Name}] {r.FromCardinality}->{r.ToCardinality} {r.ToTable.Name}[{r.ToColumn.Name}]  xfilter={r.CrossFilteringBehavior}");
        }

        // ---- integrity checks TmdlSerializer does NOT catch ----
        var problems = new List<string>();

        var measureNames = m.Tables.SelectMany(t => t.Measures.Select(me => me.Name)).ToList();
        var dupMeasures = measureNames.GroupBy(x => x, StringComparer.OrdinalIgnoreCase)
                                      .Where(g => g.Count() > 1).Select(g => g.Key).ToList();
        foreach (var d in dupMeasures)
            problems.Add($"DUPLICATE MEASURE NAME (model-wide): '{d}' x{measureNames.Count(x => string.Equals(x, d, StringComparison.OrdinalIgnoreCase))}");

        foreach (var t in m.Tables)
        {
            var colset = new HashSet<string>(t.Columns.Select(c => c.Name), StringComparer.OrdinalIgnoreCase);
            foreach (var me in t.Measures)
                if (colset.Contains(me.Name))
                    problems.Add($"MEASURE/COLUMN NAME COLLISION in table '{t.Name}': '{me.Name}'");
        }

        if (problems.Count == 0)
        {
            Console.WriteLine("INTEGRITY CHECKS: OK (no duplicate measure names, no measure/column collisions)");
            return 0;
        }
        Console.WriteLine("INTEGRITY CHECKS: FAILED");
        foreach (var p in problems) Console.WriteLine("  " + p);
        return 3;
    }
}
