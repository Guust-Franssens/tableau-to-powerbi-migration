using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;

// Structural + model-integrity validation of a hand-authored TMDL semantic model, using
// the same TOM TMDL parser Power BI Desktop uses (TmdlSerializer.DeserializeDatabaseFromFolder).
// On top of the structural parse it asserts the integrity classes that deserialize cleanly
// but fail at Desktop load / commit (per the pbi-semantic-builder gotchas):
//   (1) model-wide measure-name uniqueness
//   (2) no measure name equal to a column name within the same table
//   (3) every DAX [bracket] token resolves to a real column/measure
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
        Model m;
        try
        {
            Database db = TmdlSerializer.DeserializeDatabaseFromFolder(folder);
            m = db.Model;
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
                Console.WriteLine($"    - {t.Name,-16} cols={nc,-3} measures={mc,-3} partition={part}");
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine("TMDL DESERIALIZE: FAILED");
            Console.WriteLine(ex.GetType().FullName + ": " + ex.Message);
            if (ex.InnerException != null)
                Console.WriteLine("  inner: " + ex.InnerException.Message);
            return 1;
        }

        var problems = new List<string>();

        // (1) model-wide measure-name uniqueness
        var allMeasures = m.Tables.SelectMany(t => t.Measures.Select(x => x.Name)).ToList();
        foreach (var g in allMeasures.GroupBy(n => n).Where(g => g.Count() > 1))
            problems.Add($"[dup-measure] '{g.Key}' defined {g.Count()} times model-wide (Desktop refuses to load)");

        // (2) no measure name == a column name in the same table
        foreach (var t in m.Tables)
        {
            var colNames = new HashSet<string>(t.Columns.Where(c => c.Type != ColumnType.RowNumber).Select(c => c.Name));
            foreach (var meas in t.Measures)
                if (colNames.Contains(meas.Name))
                    problems.Add($"[measure==column] '{t.Name}'[{meas.Name}] collides with a same-named column (commit-time trap)");
        }

        // (3) every DAX [bracket] token resolves to a real column or measure (model-wide)
        var allCols = new HashSet<string>(m.Tables.SelectMany(t => t.Columns.Select(c => c.Name)));
        var allMeas = new HashSet<string>(allMeasures);
        var brk = new Regex(@"\[([^\[\]]+)\]");
        foreach (var t in m.Tables)
        {
            foreach (var meas in t.Measures)
                CheckRefs(t.Name, "measure " + meas.Name, meas.Expression, allCols, allMeas, brk, problems);
            foreach (var c in t.Columns.OfType<CalculatedColumn>())
                CheckRefs(t.Name, "column " + c.Name, c.Expression, allCols, allMeas, brk, problems);
            foreach (var p in t.Partitions)
                if (p.Source is CalculatedPartitionSource cps)
                    CheckRefs(t.Name, "calc-partition", cps.Expression, allCols, allMeas, brk, problems);
        }

        Console.WriteLine();
        if (problems.Count == 0)
        {
            Console.WriteLine("MODEL-INTEGRITY: OK  (measure-name uniqueness, no measure==column, all [bracket] refs resolve)");
            Console.WriteLine("\nRESULT: PASS");
            return 0;
        }
        Console.WriteLine($"MODEL-INTEGRITY: {problems.Count} problem(s):");
        foreach (var p in problems) Console.WriteLine("  - " + p);
        Console.WriteLine("\nRESULT: FAIL");
        return 1;
    }

    // Flag any [token] that is neither a known column nor a known measure. NAMEOF/UNICHAR etc.
    // are functions (no brackets); DAX keywords like TRUE()/BLANK() have no brackets. We only
    // check bracketed identifiers; unqualified [x] must match some column or measure name.
    static void CheckRefs(string table, string what, string dax, HashSet<string> cols,
                          HashSet<string> meas, Regex brk, List<string> problems)
    {
        if (string.IsNullOrEmpty(dax)) return;
        foreach (Match mt in brk.Matches(dax))
        {
            string tok = mt.Groups[1].Value;
            // skip the field-parameter Value1/2/3 physical bindings
            if (tok == "Value1" || tok == "Value2" || tok == "Value3") continue;
            if (!cols.Contains(tok) && !meas.Contains(tok))
                problems.Add($"[unresolved-ref] '{table}'.{what}: [{tok}] matches no column or measure");
        }
    }
}
