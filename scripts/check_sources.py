import json
from pathlib import Path
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
rows=json.loads((ROOT/"data/sources.json").read_text(encoding="utf-8"))
failed=[]
for x in rows:
    u=x.get("url")
    if not u: continue
    try:
        req=Request(u,headers={"User-Agent":"2027-Qiuzhao-Radar/1.2"})
        with urlopen(req,timeout=12) as r:
            if r.status >= 400: failed.append((x["name"],r.status))
    except Exception as e:
        failed.append((x["name"],str(e)[:100]))
print("source_check_failed:",len(failed))
for name,err in failed: print("-",name,err)
if failed: raise SystemExit(1)
