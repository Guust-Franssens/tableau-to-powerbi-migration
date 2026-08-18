"""Append the AI/Copilot-readiness pass dispositions to migration-spec.json's
limitations_encountered (idempotent: skips if an ai_prep entry already exists).
Documents the description-coverage enrichment + the intentionally-skipped
Copilot artifacts (AI data schema / AI instructions / verified answers /
Approved-for-Copilot) that have no committable Git/TMDL contract today."""
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]

P = str(_REPO / "migrations" / "wind-energy-utilization" / "migration-spec.json")
spec = json.load(open(P, encoding="utf-8"))
lim = spec.setdefault("limitations_encountered", [])

if any(str(e.get("item", "")).startswith("ai_prep.") for e in lim):
    print("ai_prep entries already present - skipping (idempotent)")
    raise SystemExit

SB = "semantic_build"


def e(item, issue, severity, stage=SB):
    return {"item": item, "issue": issue, "severity": severity, "stage": stage}


new = [
    e("ai_prep.descriptions",
      "Copilot-readiness pass (offline via powerbi-modeling-mcp ConnectFolder -> *_operations Update description -> ExportToTmdlFolder to definition/ -> Disconnect). Business-meaning descriptions now cover 100% of the model: 12/12 tables, 58/58 columns, 75/75 measures. Descriptions lead with meaning + unit/grain (MWh additive, % 0-100 average, m/s, C, kV, MW, m); DAX Copilot reads the first 200 chars. Table & measure descriptions pre-existed as TMDL /// doc comments from the build; this pass added the 58 column descriptions. Verified: partial Update preserved dataType/dataCategory/summarizeBy/annotations (Latitude/Longitude/StateOrProvince dataCategory intact for azureMap). Re-validated: DeserializeDatabaseFromFolder OK, 12 tables / 2 rels / 75 unique measures, no /// on relationships.",
      "info"),
    e("ai_prep.categorical_enums",
      "Every categorical/dimension column now lists its domain (enum) values in its description so Copilot can resolve natural-language filters. Enumerated: Turbine[Onshore Offshore]={Onshore,Offshore}; [Region]={Flevoland,Friesland,Groningen,Noord-Holland,Zeeland}; [Manufacturer]={Enercon,GE Renewable,Nordex,Siemens Gamesa,Vestas}; [Model]={Cypress 5.3,E-115,N149-4.5,SG 5.0-145,V136-4.2,V150-4.2}; [Operational Status]={Operational,Maintenance}; [Owner]={Dutch Wind Consortium,Eneco,Essent,Nuon,RWE,Vattenfall}; [Grid Connection Voltage Kv]={33,66,110,150}; [Capacity Mw]={3.5,4.2,4.5,5.0,5.3}. Daily Performance[Month]={January..December}; [Weekday]={Monday..Sunday}. Slicer domains: Month Parameter[Month]={Jan..Dec,'24}; Region/Turbine Id/Upd Turbine Name/Page parameter tables enumerate their selectable values + baked default.",
      "info"),
    e("ai_prep.skipped_ai_data_schema",
      "SKIPPED (not committable in PBIP/TMDL today): AI data schema. Authored via LSDL with no stable Git/file contract, so it cannot be reliably round-tripped through the definition/ TMDL folder. Recommend setting it in the Fabric service / Desktop after deploy.",
      "low"),
    e("ai_prep.skipped_ai_instructions",
      "SKIPPED (not committable today): AI instructions (model-level Copilot guidance). LSDL-authored, no stable file contract in the TMDL folder. Set post-deploy in the service.",
      "low"),
    e("ai_prep.skipped_verified_answers",
      "SKIPPED (not Git-supported): verified answers / linguistic Q&A pairs. Not persisted in the committable TMDL definition; configure in the service after publish.",
      "low"),
    e("ai_prep.skipped_approved_for_copilot",
      "SKIPPED (service/tenant setting, not a model artifact): 'Approved for Copilot' / semantic-model indexing toggles. These are workspace/service settings applied after deployment, out of scope for the local PBIP build.",
      "low"),
]

lim.extend(new)
json.dump(spec, open(P, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print("APPENDED", len(new), "entries. Total limitations now:", len(lim))
