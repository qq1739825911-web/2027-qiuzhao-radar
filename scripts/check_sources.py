import json
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
# v2: health-check only enabled public sources; transient network blocks are reported, not fatal.
REGISTRY=ROOT/"data/company_sources.json"
OUT=ROOT/"data/source-health.json"
sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
def check(source):
    url=source.get("url","")
    if not url.startswith(("http://","https://")):
        return {"id":source.get("id"),"name":source.get("company",source.get("name","未知信源")),"url":url,"status":"invalid_url","detail":"缺少有效公开网址"}
    try:
        req=Request(url,headers={"User-Agent":"2027-Qiuzhao-Radar-HealthCheck/1.0"})
        with urlopen(req,timeout=8) as r:
            code=int(r.status)
        return {"id":source.get("id"),"name":source.get("company",source.get("name","未知信源")),"url":url,"status":"reachable" if 200<=code<400 else "unavailable","http_status":code}
    except HTTPError as e:
        state="restricted" if e.code in (401,403,429) else "unavailable"
        return {"id":source.get("id"),"name":source.get("company",source.get("name","未知信源")),"url":url,"status":state,"http_status":e.code}
    except Exception as e:
        return {"id":source.get("id"),"name":source.get("company",source.get("name","未知信源")),"url":url,"status":"unavailable","detail":str(e)[:120]}
results=[]
with ThreadPoolExecutor(max_workers=10) as pool:
    futures=[pool.submit(check,s) for s in enabled]
    for future in as_completed(futures):
        results.append(future.result())
results.sort(key=lambda x:x["name"])
reachable=sum(x["status"]=="reachable" for x in results)
warnings=len(results)-reachable
report={"checked_at":datetime.now(timezone.utc).replace(microsecond=0).isoformat(),"enabled_sources":len(enabled),"reachable":reachable,"warnings":warnings,"results":results,"note":"信源网络受限仅作警告，不自动判定岗位失效。"}
OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"SOURCE HEALTH: enabled={len(enabled)} reachable={reachable} warnings={warnings}")
