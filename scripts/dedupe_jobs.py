import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT=Path(__file__).resolve().parents[1]
p=ROOT/"data/jobs.json"
rows=json.loads(p.read_text(encoding="utf-8"))

def norm_url(u):
    if not u: return ""
    x=urlsplit(u.strip())
    return urlunsplit((x.scheme.lower(),x.netloc.lower(),x.path.rstrip("/"),"", ""))

def key(x):
    return (norm_url(x.get("source_url","")), x.get("company","").strip().lower(), x.get("title","").strip().lower())

seen={}; out=[]
for x in rows:
    k=key(x)
    if k in seen:
        # Keep the row with the newer verification timestamp.
        old=seen[k]
        if str(x.get("last_verified","")) > str(old.get("last_verified","")):
            out[out.index(old)]=x; seen[k]=x
    else:
        seen[k]=x; out.append(x)

p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"deduped {len(rows)} -> {len(out)}")
