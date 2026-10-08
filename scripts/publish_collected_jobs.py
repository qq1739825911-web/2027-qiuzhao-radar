import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
J=ROOT/"data/jobs.json"; I=ROOT/"data/jobs.inbox.json"
existing=json.loads(J.read_text(encoding="utf-8"))
incoming=json.loads(I.read_text(encoding="utf-8")) if I.exists() else []
def key(x):
    return (x.get("company","").strip().lower(),x.get("title","").strip().lower(),x.get("source_url","").strip().lower())
idx={key(x):x for x in existing}
added=0
for x in incoming:
    k=key(x)
    if not k[0] or not k[1] or not k[2]: continue
    if k in idx:
        old=idx[k]
        if x.get("last_verified"): old["last_verified"]=x["last_verified"]
        if x.get("collector"): old["collector"]=x["collector"]
    else:
        idx[k]=x; added+=1
out=list(idx.values())
J.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"incoming={len(incoming)} added={added} total={len(out)}")
