using System;
using System.Linq;
using Microsoft.AnalysisServices.Tabular;

// Structural validation of a hand-authored TMDL semantic model, using the same
// TOM TMDL parser Power BI Desktop uses (TmdlSerializer.DeserializeDatabaseFromFolder).
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
        try
        {
            Database db = TmdlSerializer.DeserializeDatabaseFromFolder(folder);
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
                Console.WriteLine($"    - {t.Name,-24} cols={nc,-3} measures={mc,-3} partition={part}");
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine("TMDL DESERIALIZE: FAILED");
            Console.WriteLine(ex.GetType().FullName + ": " + ex.Message);
            if (ex.InnerException != null)
                Console.WriteLine("  inner: " + ex.InnerException.Message);
            return 1;
        }
    }
}
