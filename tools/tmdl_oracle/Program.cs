// purpose: TMDL oracle - deserialize semantic models with the parser Power BI Desktop itself uses
//          (TmdlSerializer, AMO 19.84.1) and report what a text scanner cannot know.
// usage:   dotnet tmdl_oracle.dll <definitionFolder> [<definitionFolder> ...]   -> JSON on stdout
//
// Two questions, one process:
//
//   1. "Does this model parse at all?"  Answered BY the real parser, so it is right by
//      construction - no grammar is re-implemented here. Three hand-written rounds of that grammar
//      each shipped both false negatives and false positives (issue #254).
//
//   2. "Did a property get silently swallowed into an expression?"  The parser cannot fail on that
//      - the document is well-formed - so it is answered by READBACK: every multi-line expression
//      is emitted together with the property VOCABULARY of the object that owns it, taken by
//      reflection from the TOM type rather than from a hand-maintained list. The caller compares
//      the two. Reflection is the point: an enumerated list is exactly what kept being incomplete.
//
// Exit codes: 0 = ran (per-model verdicts are in the JSON), 2 = could not run at all.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.Json;
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

            var vocabulary = new Dictionary<string, List<object>>();
            var models = new List<object>();
            foreach (var folder in args)
            {
                models.Add(Inspect(folder, vocabulary));
            }

            var payload = new Dictionary<string, object>
            {
                ["amoVersion"] = typeof(TmdlSerializer).Assembly.GetName().Version?.ToString() ?? "unknown",
                ["vocabulary"] = vocabulary,
                ["models"] = models,
            };
            Console.Out.Write(System.Text.Json.JsonSerializer.Serialize(payload));
            return 0;
        }

        /// <summary>Deserialize one definition folder and collect its multi-line expressions.</summary>
        private static Dictionary<string, object> Inspect(string folder, Dictionary<string, List<object>> vocabulary)
        {
            var result = new Dictionary<string, object> { ["definition"] = Path.GetFullPath(folder) };
            Database database;
            try
            {
                database = TmdlSerializer.DeserializeDatabaseFromFolder(folder);
            }
            catch (Exception ex)
            {
                result["ok"] = false;
                result["error"] = Describe(ex);
                return result;
            }

            var expressions = new List<object>();
            var visited = new HashSet<object>(ReferenceEqualityComparer.Instance);
            Walk(database.Model, database, "model", expressions, vocabulary, visited);
            result["ok"] = true;
            result["expressions"] = expressions;
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
                ["message"] = Collapse(text),
                ["document"] = document.Success ? document.Groups[1].Value : null,
                ["line"] = line.Success ? int.Parse(line.Groups[1].Value) : (int?)null,
            };
        }

        private static string Collapse(string text)
        {
            return Regex.Replace(text ?? "", @"\s+", " ").Trim();
        }

        /// <summary>
        /// Walk the parsed object graph, recording every multi-line expression with the property
        /// vocabulary of its owner and of the object that contains it.
        /// </summary>
        /// <remarks>
        /// Two decisions here are load-bearing. Descent is by ASSEMBLY, not by `is MetadataObject`:
        /// `Partition.Source` is a `PartitionSource`, which is not a MetadataObject, so a
        /// MetadataObject-only walk silently reached zero partition M queries - i.e. it inspected
        /// none of the expressions this corpus actually has. And the parent is passed down the walk
        /// rather than read back off the child, because a non-MetadataObject node has no `Parent` -
        /// which is exactly where a swallowed `mode:` belongs.
        ///
        /// Containment and back-references are not distinguished on purpose: a global visited set
        /// makes a back-reference (Measure.Parent -> Table) terminate immediately, which is cheaper
        /// and far less brittle than enumerating which edges point downwards.
        /// </remarks>
        private static void Walk(
            object node,
            object parent,
            string path,
            List<object> expressions,
            Dictionary<string, List<object>> vocabulary,
            HashSet<object> visited)
        {
            if (node == null || !visited.Add(node))
            {
                return;
            }

            var type = node.GetType();
            foreach (var property in type.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (property.GetIndexParameters().Length > 0 || !property.CanRead)
                {
                    continue;
                }

                object value;
                try
                {
                    value = property.GetValue(node);
                }
                catch (Exception)
                {
                    continue;
                }

                if (value == null)
                {
                    continue;
                }

                if (value is string text)
                {
                    if (property.Name.EndsWith("Expression", StringComparison.Ordinal) && text.Contains('\n'))
                    {
                        expressions.Add(Record(node, parent, path, property.Name, text, vocabulary));
                    }
                }
                else if (value is IEnumerable items)
                {
                    // Collections BEFORE single objects: a TOM collection lives in the same
                    // assembly as the objects it holds, so an assembly test alone treats
                    // `Model.Tables` as a leaf and the walk never reaches a single table.
                    foreach (var item in items)
                    {
                        if (IsTabular(item))
                        {
                            Walk(item, node, Extend(path, item), expressions, vocabulary, visited);
                        }
                    }
                }
                else if (IsTabular(value))
                {
                    Walk(value, node, Extend(path, value), expressions, vocabulary, visited);
                }
            }
        }

        /// <summary>Whether a value is a TOM object worth descending into.</summary>
        private static bool IsTabular(object value)
        {
            return value != null && value.GetType().Assembly == typeof(TmdlSerializer).Assembly;
        }

        private static string Extend(string path, object child)
        {
            var name = (child as NamedMetadataObject)?.Name;
            var label = child.GetType().Name;
            return path + " > " + (string.IsNullOrEmpty(name) ? label : label + " '" + name + "'");
        }

        /// <summary>Record one expression plus the vocabulary a swallowed property would come from.</summary>
        private static Dictionary<string, object> Record(
            object owner,
            object parent,
            string path,
            string propertyName,
            string text,
            Dictionary<string, List<object>> vocabulary)
        {
            var types = new List<string> { owner.GetType().Name };
            if (parent != null)
            {
                types.Add(parent.GetType().Name);
            }

            foreach (var scope in new[] { owner, parent }.Where(x => x != null))
            {
                Register(scope.GetType(), vocabulary);
            }

            return new Dictionary<string, object>
            {
                ["path"] = path,
                ["property"] = propertyName,
                ["types"] = types,
                ["unsetBooleans"] = UnsetBooleans(owner).Concat(UnsetBooleans(parent)).Distinct().ToList(),
                ["text"] = text,
            };
        }

        /// <summary>Property names of a TOM type, as TMDL spells them, with their booleans marked.</summary>
        private static void Register(Type type, Dictionary<string, List<object>> vocabulary)
        {
            if (vocabulary.ContainsKey(type.Name))
            {
                return;
            }

            var names = new List<object>();
            foreach (var property in type.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (property.GetIndexParameters().Length > 0)
                {
                    continue;
                }

                names.Add(new Dictionary<string, object>
                {
                    ["name"] = Camel(property.Name),
                    ["isBoolean"] = property.PropertyType == typeof(bool) || property.PropertyType == typeof(bool?),
                });
            }

            vocabulary[type.Name] = names;
        }

        /// <summary>Boolean properties currently sitting at false - a swallowed flag never took effect.</summary>
        private static IEnumerable<string> UnsetBooleans(object node)
        {
            if (node == null)
            {
                yield break;
            }

            foreach (var property in node.GetType().GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (property.GetIndexParameters().Length > 0 || property.PropertyType != typeof(bool))
                {
                    continue;
                }

                object value;
                try
                {
                    value = property.GetValue(node);
                }
                catch (Exception)
                {
                    continue;
                }

                if (value is bool flag && !flag)
                {
                    yield return Camel(property.Name);
                }
            }
        }

        private static string Camel(string name)
        {
            return string.IsNullOrEmpty(name) ? name : char.ToLowerInvariant(name[0]) + name.Substring(1);
        }
    }
}
