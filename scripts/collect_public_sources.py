"""Public recruitment collector.
- Crawls only enabled public official sources.
- No login, CAPTCHA bypass, private API, robots bypass, or anti-bot evasion.
- Keeps job-level granularity: one concrete role/listing per record when the source exposes it.
- Falls back to a program/position-page record only when the source does not expose individual jobs publicly.
"""
import json,re,time,hashlib
from pathlib import Path
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse,urlunparse
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/"data/company_sources.json"
INBOX=ROOT/"data/jobs.inbox.json"
MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=str(date.today())

HEADERS={"User-Agent":"2027-Qiuzhao-Radar-PublicCollector/1.0"}

JOB_WORDS=("招聘","校招","校园","职位","岗位","工程师","产品","运营","设计","研发","算法","数据","销售","营销","金融科技","管理培训生","管培","人才")
BAD_WORDS=("登录","注册","首页","关于我们","新闻","公告","联系我们","隐私","下载","帮助","返回")

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.cur=None
    def handle_starttag(self,tag,attrs):
        if tag.lower()=="a":
            d=dict(attrs); self.cur={"href":d.get("href",""),"text":""}
    def handle_data(self,data):
        if self.cur is not None: self.cur["text"]+=data
    def handle_endtag(self,tag):
        if tag.lower()=="a" and self.cur is not None:
            self.links.append(self.cur); self.cur=None

def fetch(url):
    req=Request(url,headers=HEADERS)
    with urlopen(req,timeout=20) as r:
        raw=r.read(2_000_000)
        enc=r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc,errors="ignore"),r.geturl()

def clean(s):
    return re.sub(r"\\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()

def same_host(a,b):
    return urlparse(a).netloc.lower()==urlparse(b).netloc.lower()

def canonical(u):
    p=urlparse(u)
    return urlunparse((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/"),"","",""))

def infer_category(title):
    rules=[
      ("AI / 算法 / 技术",("AI","算法","大模型","机器学习","软件","开发","工程师","数据","技术")),
      ("产品 / 运营",("产品","运营","用户增长")),
      ("设计",("设计","视觉","交互","UX","UI")),
      ("销售 / 商务 / 营销",("销售","商务","营销","品牌","市场")),
      ("金融 / 银行",("银行","金融","投资","风险","财务","审计")),
      ("人力资源",("人力","HR","招聘")),
      ("供应链 / 制造",("供应链","制造","采购","物流")),
      ("法务 / 合规",("法律","法务","合规")),
    ]
    for cat,words in rules:
        if any(w.lower() in title.lower() for w in words): return cat
    return "综合 / 校园招聘"

def extract_year(text):
    m=re.search(r"(20\\d{2})届",text or "")
    return m.group(1)+"届" if m else "2027届"

def make_id(company,title,url):
    return "auto-"+hashlib.sha1((company+"|"+title+"|"+canonical(url)).encode()).hexdigest()[:16]

def collect_source(src):
    company=src["company"]; root=src["url"]
    records=[]; seen=set(); pages=[root]; visited=set()
    while pages and len(visited)<3 and len(records)<300:
        page=pages.pop(0)
        if canonical(page) in visited: continue
        visited.add(canonical(page))
        try: html,final=fetch(page)
        except Exception as e:
            continue
        parser=LinkParser(); parser.feed(html)
        for a in parser.links:
            title=clean(a["text"])
            href=canonical(urljoin(final,a["href"]))
            if not title or len(title)<4 or len(title)>120 or not href or href in seen: continue
            if not same_host(final,href): continue
            low=title.lower()
            if any(x.lower() in low for x in BAD_WORDS): continue
            if not any(x.lower() in low for x in JOB_WORDS): continue
            seen.add(href)
            cohort=extract_year(title+" "+html[:5000])
            records.append({
              "id":make_id(company,title,href),
              "company":company,
              "title":title,
              "category":infer_category(title),
              "city":"全国",
              "enterprise_type":"待核验",
              "degree":"待核验",
              "publish_date":"",
              "deadline":"",
              "last_verified":TODAY,
              "source":company+"官方招聘",
              "source_url":href,
              "keywords":re.findall(r"[A-Za-z0-9+#.-]{2,}|[\\u4e00-\\u9fff]{2,8}",title)[:12],
              "status":"pending_review",
              "demo":False,
              "confirmed_by":[company+"官方公开页面"],
              "cohort":cohort,
              "collector":"public-html-v2"
            })
            if len(pages)<3 and len(visited)<3 and len(records)<300:
                pages.append(href)
    return records,visited

sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
all_rows=[]; results=[]
for s in enabled:
    rows,visited=collect_source(s); all_rows.extend(rows)
    results.append({"id":s["id"],"company":s["company"],"pages_visited":len(visited),"records_found":len(rows)})
    time.sleep(.2)
INBOX.write_text(json.dumps(all_rows,ensure_ascii=False,indent=2),encoding="utf-8")
MANIFEST.write_text(json.dumps({
 "run_date":TODAY,"collector_version":"2.0","enabled_sources":len(enabled),
 "records_found":len(all_rows),"results":results,
 "policy":"Public official pages only; no login/CAPTCHA/private API/anti-bot bypass.",
 "granularity":"Prefer concrete job/listing links. Non-job program pages remain pending_review and are not silently promoted to active."
},ensure_ascii=False,indent=2),encoding="utf-8")
print("sources",len(enabled),"records",len(all_rows))
