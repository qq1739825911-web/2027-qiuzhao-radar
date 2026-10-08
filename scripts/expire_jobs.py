import json
from pathlib import Path
from datetime import date

ROOT=Path(__file__).resolve().parents[1]
p=ROOT/"data/jobs.json"
today=date.today()
rows=json.loads(p.read_text(encoding="utf-8"))
changed=0
for x in rows:
    d=x.get("deadline")
    if d:
        try:
            if date.fromisoformat(d) < today and x.get("status") not in {"expired"}:
                x["status"]="expired"; changed+=1
        except ValueError:
            pass
p.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"expired={changed}")
