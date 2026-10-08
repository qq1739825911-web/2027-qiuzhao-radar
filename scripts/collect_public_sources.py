"""V1.3 public-source collector framework.
Only sources explicitly enabled in data/company_sources.json are eligible.
This first version intentionally does not bypass login, CAPTCHA, robots restrictions,
or private APIs. It records a source manifest and can be extended per-source.
"""
import json
from pathlib import Path
from datetime import date

ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/"data/company_sources.json"
MANIFEST=ROOT/"data/collection-manifest.json"

sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
manifest={
  "run_date":str(date.today()),
  "collector_version":"1.0",
  "enabled_sources":[
    {"id":s["id"],"company":s.get("company"),"url":s.get("url"),"status":"ready"}
    for s in enabled
  ],
  "policy":"Only explicitly enabled public sources. No login/CAPTCHA bypass or private API access.",
  "next_step":"Implement source-specific parsers and write raw records to data/inbox."
}
MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"enabled_sources={len(enabled)}")
