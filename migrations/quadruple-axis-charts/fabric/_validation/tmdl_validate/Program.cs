using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;

// Structural + integrity validation of a hand-authored TMDL semantic model, using the same
// TOM TMDL parser Power BI Desktop uses (TmdlSerializer.DeserializeDatabaseFromFolder).
// Beyond deserialize, this asserts the three failure classes TmdlSerializer does NOT catch
// (both deserialize clean but fail at Desktop load / commit):
//   (1) model-wide DUPLICATE MEASURE NAMES,
//   (2) a measure whose name equals a COLUMN name in the SAME table,
//   (3) every DAX [bracket] token resolving to a real column/measure.
// Usage: dotnet run -- "<path to ...SemanticModel\definition>"
class Program
{
    static int Main(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: tmdl_validate <definitionFolder> [--require-descriptions]");
            return 2;
        }
        bool requireDesc = args.Any(a => a.Equals("--require-descriptions", StringComparison.OrdinalIgnoreCase));
        string folder = args.FirstOrDefault(a => !a.StartsWith("--"));
        if (folder == null)
        {
            Console.Error.WriteLine("usage: tmdl_validate <definitionFolder> [--require-descriptions]");
            return 2;
        }
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
            Console.WriteLine($"    - {t.Name,-42} cols={nc,-3} measures={mc,-3} partition={part}");
        }

        var errors = new List<string>();

        // (1) model-wide duplicate measure names
        var measureNames = m.Tables.SelectMany(t => t.Measures.Select(me => me.Name)).ToList();
        foreach (var g in measureNames.GroupBy(n => n, StringComparer.OrdinalIgnoreCase).Where(g => g.Count() > 1))
            errors.Add($"DUPLICATE MEASURE NAME (model-wide): '{g.Key}' x{g.Count()} -> breaks Desktop load.");

        // (2) measure name == a column name in the same table
        foreach (var t in m.Tables)
        {
            var colNames = new HashSet<string>(t.Columns.Select(c => c.Name), StringComparer.OrdinalIgnoreCase);
            foreach (var me in t.Measures)
                if (colNames.Contains(me.Name))
                    errors.Add($"MEASURE/COLUMN COLLISION in table '{t.Name}': '{me.Name}' -> fails at commit.");
        }

        // (3) every DAX [bracket] token resolves to a real column or measure (per hosting table for columns)
        var allMeasureNames = new HashSet<string>(measureNames, StringComparer.OrdinalIgnoreCase);
        var colsByTable = m.Tables.ToDictionary(
            t => t.Name,
            t => new HashSet<string>(t.Columns.Select(c => c.Name), StringComparer.OrdinalIgnoreCase));
        var allColNames = new HashSet<string>(m.Tables.SelectMany(t => t.Columns.Select(c => c.Name)), StringComparer.OrdinalIgnoreCase);
        // token regex: optional 'Table'/Table qualifier then [Column/Measure]
        var rx = new Regex(@"(?:'(?<tq>[^']+)'|(?<tb>[A-Za-z_][\w\- ]*))?\s*\[(?<name>[^\]]+)\]");

        foreach (var t in m.Tables)
        {
            var exprs = new List<(string kind, string owner, string dax)>();
            foreach (var me in t.Measures)
                if (!string.IsNullOrWhiteSpace(me.Expression)) exprs.Add(("measure", me.Name, me.Expression));
            foreach (var c in t.Columns.OfType<CalculatedColumn>())
                if (!string.IsNullOrWhiteSpace(c.Expression)) exprs.Add(("calcCol", c.Name, c.Expression));

            foreach (var (kind, owner, dax) in exprs)
            {
                foreach (Match mt in rx.Matches(dax))
                {
                    string tok = mt.Groups["name"].Value;
                    string tq = mt.Groups["tq"].Success ? mt.Groups["tq"].Value
                              : mt.Groups["tb"].Success ? mt.Groups["tb"].Value : null;
                    // DAX date literals like DATE(...) or fn args won't match [..]; only bracketed tokens do.
                    bool ok;
                    if (tq != null && colsByTable.ContainsKey(tq))
                        ok = colsByTable[tq].Contains(tok) || allMeasureNames.Contains(tok);
                    else
                        ok = allMeasureNames.Contains(tok) || allColNames.Contains(tok);
                    if (!ok)
                        errors.Add($"UNRESOLVED [{tok}] in {kind} '{t.Name}'[{owner}]"
                                 + (tq != null ? $" (qualifier '{tq}')" : ""));
                }
            }
        }

        // (4) AI/Copilot readiness: description coverage across tables, columns (excl. RowNumber), measures.
        //     TMDL '///' comments populate TOM .Description — exactly what DAX Copilot reads (first ~200 chars).
        var missing = new List<string>();
        int tabTot = 0, tabHave = 0, colTot = 0, colHave = 0, meaTot = 0, meaHave = 0;
        foreach (var t in m.Tables.OrderBy(t => t.Name))
        {
            tabTot++;
            if (!string.IsNullOrWhiteSpace(t.Description)) tabHave++; else missing.Add($"table '{t.Name}'");
            foreach (var c in t.Columns.Where(c => c.Type != ColumnType.RowNumber).OrderBy(c => c.Name))
            {
                colTot++;
                if (!string.IsNullOrWhiteSpace(c.Description)) colHave++; else missing.Add($"column '{t.Name}'[{c.Name}]");
            }
            foreach (var me in t.Measures.OrderBy(me => me.Name))
            {
                meaTot++;
                if (!string.IsNullOrWhiteSpace(me.Description)) meaHave++; else missing.Add($"measure '{t.Name}'[{me.Name}]");
            }
        }
        Console.WriteLine();
        Console.WriteLine("DESCRIPTION COVERAGE (AI/Copilot readiness):");
        Console.WriteLine($"  tables   = {tabHave}/{tabTot}");
        Console.WriteLine($"  columns  = {colHave}/{colTot}");
        Console.WriteLine($"  measures = {meaHave}/{meaTot}");
        if (missing.Count > 0)
        {
            Console.WriteLine($"  MISSING ({missing.Count}):");
            foreach (var s in missing) Console.WriteLine("    - " + s);
            if (requireDesc)
                errors.Add($"DESCRIPTION COVERAGE incomplete: {missing.Count} object(s) without a description.");
        }
        else
        {
            Console.WriteLine("  ALL objects carry a description.");
        }

        Console.WriteLine();
        if (errors.Count == 0)
        {
            Console.WriteLine("INTEGRITY CHECKS: OK (measure-name uniqueness, measure/column collision, bracket-token resolution)");
            return 0;
        }
        Console.WriteLine($"INTEGRITY CHECKS: {errors.Count} ISSUE(S)");
        foreach (var e in errors.Distinct()) Console.WriteLine("  - " + e);
        return 1;
    }
}
