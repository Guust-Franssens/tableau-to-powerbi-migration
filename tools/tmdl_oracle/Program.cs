// purpose: TMDL oracle - ask the parser Power BI Desktop itself uses whether a semantic model loads.
// usage:   dotnet tmdl_oracle.dll <definitionFolder> [<definitionFolder> ...]   -> JSON on stdout
//
// Scope is deliberately ONE question: does TmdlSerializer.DeserializeDatabaseFromFolder accept this
// model? That question is answered BY the real parser, so it is exact by construction - no TMDL
// grammar is re-implemented here, and there is nothing to keep in step with a format we do not own.
//
// It deliberately does NOT try to detect SILENT ABSORPTION (a property written at the wrong indent
// and swallowed into the preceding DAX/M). That is measurably impossible from the parse: for
//
//     measure Probe =        |   measure Probe =
//         1                  |           1
//         isHidden           |           isHidden
//
// - a swallowed property on the left, ordinary expression content on the right - AMO returns the
// BYTE-IDENTICAL Expression "1\nisHidden" with IsHidden=False in both cases, because it strips the
// common indent. The distinguishing information is the source indentation, and it is gone by the
// time anything can be read back. Three attempts to recover it (a property-name allowlist, an
// indentation contract, this reflection readback) each shipped false positives on valid models.
// Issue #404 carries the analysis; this tool stays exact instead of nearly right.
//
// Exit codes: 0 = ran (per-model verdicts are in the JSON), 2 = could not run at all.

using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;

namespace TmdlOracle
{
    internal static class Program
    {
        private static readonly Regex DocumentRe = new Regex(@"Document\s*-\s*'([^']*)'", RegexOptions.Compiled);
        private static readonly Regex LineRe = new Regex(@"Line Number\s*-\s*(\d+)", RegexOptions.Compiled);

        private static int Main(string[] args)
        {
            if (args.Length < 1)
            {
                Console.Error.WriteLine("usage: tmdl_oracle <definitionFolder> [<definitionFolder> ...]");
                return 2;
            }

            var models = new List<object>();
            foreach (var folder in args)
            {
                models.Add(Inspect(folder));
            }

            var payload = new Dictionary<string, object>
            {
                // The caller checks this against the version pinned in tmdl_oracle.csproj. A verdict
                // is only as trustworthy as the parser that produced it, so an unrecognised build is
                // treated as "could not assess" rather than as a pass.
                ["amoVersion"] = typeof(TmdlSerializer).Assembly.GetName().Version?.ToString() ?? "unknown",
                ["models"] = models,
            };
            Console.Out.Write(System.Text.Json.JsonSerializer.Serialize(payload));
            return 0;
        }

        /// <summary>Hand one definition folder to TmdlSerializer and report its verdict.</summary>
        private static Dictionary<string, object> Inspect(string folder)
        {
            var result = new Dictionary<string, object> { ["definition"] = Path.GetFullPath(folder) };
            try
            {
                TmdlSerializer.DeserializeDatabaseFromFolder(folder);
            }
            catch (Exception ex)
            {
                result["ok"] = false;
                result["error"] = Describe(ex);
                return result;
            }

            result["ok"] = true;
            return result;
        }

        /// <summary>Flatten an exception into the document and line the caller can point a user at.</summary>
        private static Dictionary<string, object> Describe(Exception ex)
        {
            var text = ex.Message ?? "";
            for (var inner = ex.InnerException; inner != null; inner = inner.InnerException)
            {
                text += " | inner: " + inner.Message;
            }

            var document = DocumentRe.Match(text);
            var line = LineRe.Match(text);
            return new Dictionary<string, object>
            {
                ["type"] = ex.GetType().Name,
                ["message"] = Regex.Replace(text, @"\s+", " ").Trim(),
                ["document"] = document.Success ? document.Groups[1].Value : null,
                ["line"] = line.Success ? int.Parse(line.Groups[1].Value) : (int?)null,
            };
        }
    }
}
