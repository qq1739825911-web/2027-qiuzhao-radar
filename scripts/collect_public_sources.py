"""V3 public recruitment collector.

Rules:
- public official pages only; no login/CAPTCHA/private API/anti-bot bypass.
- prefer concrete job-level listings.
- program/announcement pages are never promoted to active jobs.
- extract visible links, HTML tables/lists, JSON-LD and common embedded JSON.
- score records and only promote high-confidence concrete roles.
"""
import time
import json,re,hashlib
from pathlib import Path
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse,urlunparse
from urllib.request import Request,urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/"data/company_sources.json"; INBOX=ROOT/"data/jobs.inbox.json"; MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=str(date.today())
HEADERS={"User-Agent":"2027-Qiuzhao-Radar-PublicCollector/3.0"}

JOB_WORDS=("工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理")
CONCRETE_WORDS=("工程师","开发","研发","算法","数据","产品经理","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理")
PROGRAM_WORDS=("招聘公告","招聘启事","校园招聘","校招公告","招聘简章","招聘信息","招聘计划","招聘项目","招聘专场","秋季招聘","春季招聘","招聘通知")
NAV_BAD=("登录","注册","首页","关于我们","新闻","公告","联系我们","隐私","下载","帮助","返回","信用卡产品","借记卡产品","自营金融","产品服务","理财","基金产品")
ROLE_HREF=("job","position","career","campus","recruit","zhaopin","jobs","vacancy","detail","post")

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.text=[]; self.tables=[]; self.scripts=[]; self.page_title=""; self._in_title=False; self._script_buf=None; self._a=None; self._td=None; self._row=[]
    def handle_starttag(self,tag,attrs):
        d=dict(attrs); tag=tag.lower()
        if tag=="a": self._a={"href":d.get("href",""),"text":""}
        if tag=="title": self._in_title=True
        if tag in ("td","th"): self._td=""
        if tag=="tr": self._row=[]
        if tag=="script": self._script_buf=[]
    def handle_data(self,data):
        s=re.sub(r"\s+"," ",data).strip()
        if s: self.text.append(s)
        if self._a is not None: self._a["text"]+=(" "+s if s else "")
        if self._script_buf is not None: self._script_buf.append(data)
        if self._in_title: self.page_title+=data
        if self._td is not None: self._td+=(" "+s if s else "")
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag=="a" and self._a is not None: self.links.append(self._a); self._a=None
        if tag=="title": self._in_title=False
        if tag=="script" and self._script_buf is not None:
            body="".join(self._script_buf).strip()
            if body and len(body)<5000000: self.scripts.append(body)
            self._script_buf=None
        if tag in ("td","th") and self._td is not None:
            self._row.append(self._td.strip()); self._td=None
        if tag=="tr" and self._row: self.tables.append(self._row)
    def page_text(self): return " ".join(self.text)

def fetch(url):
    req=Request(url,headers=HEADERS)
    with urlopen(req,timeout=10) as r:
        raw=r.read(4_000_000); enc=r.headers.get_content_charset() or "utf-8"; final=r.geturl()
    return raw.decode(enc,errors="ignore"),final

def clean(s): return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()
def canonical(u):
    p=urlparse(u); return urlunparse((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/"),"","",""))
def same_host(a,b): return urlparse(a).netloc.lower()==urlparse(b).netloc.lower()
def year(s):
    m=re.search(r"(20\d{2})\s*[届年]",s or "")
    return (m.group(1)+"届") if m else "2027届"
def category(t):
    t=t.lower()
    if any(x in t for x in ("ai","算法","大模型","机器学习","软件","开发","工程师","数据","技术","研发","嵌入式")): return "AI / 算法 / 技术"
    if any(x in t for x in ("产品","运营","增长")): return "产品 / 运营"
    if any(x in t for x in ("设计","视觉","交互","ux","ui")): return "设计"
    if any(x in t for x in ("销售","商务","营销","品牌","市场")): return "销售 / 商务 / 营销"
    if any(x in t for x in ("银行","金融","投资","风险","财务","审计","fintech")): return "金融 / 银行"
    if any(x in t for x in ("人力","hr","招聘")): return "人力资源"
    if any(x in t for x in ("供应链","制造","采购","物流","机械","电气")): return "供应链 / 制造"
    if any(x in t for x in ("法律","法务","合规")): return "法务 / 合规"
    return "综合 / 校园招聘"
def make_id(company,title,url): return "auto-"+hashlib.sha1((company+"|"+title+"|"+canonical(url)).encode()).hexdigest()[:16]

def score(title,href,context):
    s=0; t=(title+" "+href).lower()
    if any(w.lower() in title.lower() for w in CONCRETE_WORDS): s+=5
    if any(w.lower() in href.lower() for w in ROLE_HREF): s+=2
    if re.search(r"20\d{2}",title): s+=1
    if any(w.lower() in title.lower() for w in PROGRAM_WORDS): s-=4
    if any(w.lower() in title.lower() for w in NAV_BAD): s-=6
    if len(title)>70: s-=2
    return s

def record(company,title,url,source,cohort,confidence,program=""):
    status="active" if confidence>=7 else "pending_review"
    return {"id":make_id(company,title,url),"company":company,"title":title,
      "category":category(title),"city":"全国","enterprise_type":"待核验","degree":"待核验",
      "salary":"","publish_date":"","deadline":"","last_verified":TODAY,"source":source,
      "source_url":url,"keywords":re.findall(r"[A-Za-z0-9+#.-]{2,}|[\u4e00-\u9fff]{2,8}",title)[:14],
      "status":status,"demo":False,"confirmed_by":[company+"官方公开页面"],"cohort":cohort,
      "program":program,"collector":"public-html-v3","granularity":"job" if status=="active" else "review",
      "verification_score":confidence}

def embedded_records(company,source,parser,cohort,source_label=None):
    out=[]; host=urlparse(source).netloc.lower()
    # Only parse JSON embedded in public HTML; never call hidden APIs.
    for blob in parser.scripts:
        if not (blob.startswith("{") or blob.startswith("[")): continue
        try: obj=json.loads(blob)
        except Exception: continue
        stack=[obj]
        while stack and len(out)<500:
            node=stack.pop()
            if isinstance(node,dict):
                title=""
                for key in ("positionName","jobName","recruitJobName","postName","title","name"):
                    val=node.get(key)
                    if isinstance(val,str) and 3<len(val.strip())<90 and any(w.lower() in val.lower() for w in CONCRETE_WORDS):
                        title=val.strip(); break
                url=""
                for key in ("positionUrl","jobUrl","detailUrl","url","href","link"):
                    val=node.get(key)
                    if isinstance(val,str) and val.startswith(("http://","https://","/")):
                        url=canonical(urljoin(source,val)); break
                if title and url and urlparse(url).netloc.lower()==host:
                    sc=score(title,url,"")
                    if sc>=7 and any(x.lower() in url.lower() for x in ROLE_HREF):
                        item=record(company,title,url,source_label or company+"官方招聘",cohort,sc)
                        if source_label=="应届生求职网公开岗位":
                            item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                        if source_label and "公开" in source_label: item["confirmed_by"]=[source_label]
                        out.append(item)
                stack.extend(v for v in node.values() if isinstance(v,(dict,list)))
            elif isinstance(node,list): stack.extend(v for v in node if isinstance(v,(dict,list)))
    return out

def table_records(company,source,parser,cohort,source_label=None):
    out=[]
    cities=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外","全国")
    degree_words=("博士","硕士","本科","大专")
    for row in parser.tables:
        if len(row)<2: continue
        joined=" | ".join(row)
        if any(x in joined for x in NAV_BAD): continue
        # Keep actual role names, not category headings or copied qualification paragraphs.
        role=next((x for x in row if 2<len(x)<=45
                   and any(w.lower() in x.lower() for w in CONCRETE_WORDS)
                   and not x.endswith(("类","方向","相关专业","等相关专业","专业"))
                   and not x.startswith(("具有","负责","熟悉","掌握","参与","岗位","要求","本科","硕士","博士"))),None)
        if not role: continue
        score_value=5 if source_label=="应届生求职网公开岗位" else 8
        r=record(company,role,source,source_label or company+"官方招聘",cohort,score_value)
        if source_label and "公开" in source_label:
            r["confirmed_by"]=[source_label]
        found_cities=[]
        for city in cities:
            if city in joined and city not in found_cities: found_cities.append(city)
        if found_cities: r["city"]="、".join(found_cities)
        degree=next((x for x in row if any(d in x for d in degree_words) and len(x)<=35),None)
        if degree: r["degree"]=degree
        out.append(r)
    return out

def collect_source(src):
    company,root=src["company"],src["url"]; max_pages=int(src.get("max_pages",6)); max_candidates=int(src.get("max_candidates",500)); queue=list(src.get("seed_urls") or [root])[:max_pages]; visited=set(); seen=set(); rows=[]
    while queue and len(visited)<max_pages and len(rows)<max_candidates:
        page=queue.pop(0)
        cp=(page.split("#",1)[0] if src.get("id")=="qiuzhaowang" else canonical(page))
        if cp in visited: continue
        visited.add(cp)
        if src.get("id")=="qiuzhaowang" and visited: time.sleep(0.5)
        try: html,final=fetch(page)
        except Exception: continue
        p=Parser(); p.feed(html); cohort=year(html[:20000])
        raw_page_title=clean(p.page_title)
        page_company=company
        record_source=company+"官方招聘"
        if src.get("id")=="nowcoder":
            # Job detail title pattern: role_title_company校招_牛客网
            m=re.search(r"_([^_]+?)(?:校招|实习|社招)_牛客网", raw_page_title)
            if m: page_company=m.group(1).strip()
            record_source="牛客公开岗位"
        elif src.get("id")=="yingjiesheng":
            # Article title pattern: employer + cohort/recruitment event + site suffix.
            m=re.search(r"^(.+?)(?:20\d{2}届|2027届|校招|校园招聘|招聘宣讲)", raw_page_title)
            if m: page_company=m.group(1).strip(" _-—")
            record_source="应届生求职网公开岗位"
        elif src.get("id")=="qiuzhaowang":
            parts=re.split(r"[|｜]",raw_page_title)
            if "/jobs/" in urlparse(final).path and parts: page_company=parts[0].strip()
            record_source="秋招网公开聚合"
        page_title=raw_page_title
        # Strip location prefixes and aggregator suffixes so the stored title remains job-level.
        page_title=re.sub(r"^(北京|上海|深圳|杭州|广州|成都|西安|武汉|南京|苏州|合肥|天津|重庆|济南)[-—_ ]+", "", page_title)
        page_title=re.sub(r"[_|｜].*(?:校招|实习|社招|招聘).*?$", "", page_title).strip()
        page_title=re.split(r"\s+[|｜_—]\s+|\s+-\s+(?:百度校园招聘|小米校园招聘|校园招聘).*$",page_title,1)[0].strip()
        if src.get("id")=="qiuzhaowang" and urlparse(final).path=="/latest":
            page_text=p.page_text()
            seen_groups=set()
            role_hints=("工程师","研发","算法","产品","经理","销售","财务","设计","运营","管理","技术","教师","审计","法律","采购","管培","分析","工艺","自动化","质量","研究","岗位","方向","PM","AI","硬件","软件","芯片","医学","金融","会计","营销","供应链","商务","专员","服务","项目","研究员","分析师","咨询","物流","教师","顾问","工程技术")
            for link_index,a in enumerate(p.links):
                group_title=clean(a["text"])
                group_href=urljoin(final,a["href"])
                if not same_host(final,group_href) or "/jobs/" not in urlparse(group_href).path: continue
                if len(group_title)<4 or len(group_title)>500: continue
                if group_title.startswith(("查看","开通会员","登录")) or group_title in ("岗位发现","我的机会","求职记录","全部岗位"): continue
                if any(x.lower() in group_title.lower() for x in NAV_BAD): continue
                if not any(x.lower() in group_title.lower() for x in role_hints): continue
                group_url=canonical(group_href)
                if group_url in seen_groups: continue
                company_name=""
                for previous in reversed(p.links[max(0,link_index-3):link_index]):
                    candidate=clean(previous["text"])
                    candidate_url=urljoin(final,previous["href"])
                    if len(candidate)<2 or len(candidate)>60 or "/jobs/" in urlparse(candidate_url).path: continue
                    if candidate in ("秋招网","岗位发现","我的机会","求职记录","校招资料","求职指南","会员权益"): continue
                    if any(x.lower() in candidate.lower() for x in NAV_BAD): continue
                    company_name=candidate
                    break
                if not company_name: continue
                seen_groups.add(group_url)
                position=page_text.find(company_name)
                chunk=page_text[position:position+1000] if position>=0 else page_text
                city_match=re.search(r"工作地点\s*(.+?)\s*届次批次",chunk)
                city=clean(city_match.group(1)) if city_match else "全国"
                cohort=year(chunk) or "2027届"
                role_text=re.sub(r"(?:\.\.\.|…)+$","",group_title).replace("//"," · ")
                if "·" in role_text or "•" in role_text:
                    role_names=re.split(r"\s*[·•]\s*",role_text)
                else:
                    role_names=re.split(r"\s+(?=(?:AI|FPGA|大数据|算法|研发|软件|应用|嵌入式|人力资源|供应链|销售|财务|项目|产品|技术|工程|质量|管理|采购|市场|运营|数据|机械|电气|材料|设计|法律|法务|审计|教师|研究|客户|生产|工艺|医学|药学|营销|品牌|战略|风险|金融|投资|管培|行政|专员|计算机|通信|网络|硬件|芯片|自动化|控制|系统|测试|运维|服务|助理|顾问|咨询|业务|商务|物流|设备|计划|渠道|会计|客服|机器人|研究员|分析师))",role_text)
                for role_name in role_names:
                    role_name=clean(role_name)
                    if len(role_name)<2 or len(role_name)>45: continue
                    if role_name.endswith(("类","方向","体系","专业")): continue
                    if role_name in ("工程热物理","热能工程","能源与动力工程","发电厂及电力系统","电气工程及其自动化","计算机科学与技术","软件工程","机械工程","材料科学与工程","化学工程与工艺","高电压与绝缘技术","电力系统及其自动化","电子与通信","工程力学","土木工程","地质工程","建筑学","计算机系统结构","计算机软件与理论","计算机应用技术","网络空间安全","控制科学与工程","信息与通信工程","电机与电器","电力电子与电力传动","安全科学与工程","环境科学与工程","数学与应用数学","冶金工程","矿业工程"): continue
                    if role_name.startswith(("招聘项目","具体岗位","岗位要求","招聘方向","需求专业","工作地点","届次批次")): continue
                    item=record(company_name,role_name,group_url,record_source,cohort,5)
                    item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                    item["city"]=city or "全国"; item["confirmed_by"]=[record_source]
                    rows.append(item)
        if src.get("id")=="qiuzhaowang" and "/jobs/" in urlparse(final).path:
            page_text=p.page_text()
            role_match=re.search(r"招聘岗位\s*(.+?)\s*工作城市",page_text)
            city_match=re.search(r"工作城市\s*(.+?)\s*面向届次",page_text)
            if role_match:
                role_text=re.sub(r"招聘项目包含.*$","",role_match.group(1)).strip()
                role_names=[clean(x) for x in re.split(r"\s*[·•]\s*",role_text) if clean(x)]
                city_text=clean(city_match.group(1)) if city_match else "全国"
                for role_name in role_names:
                    if not (2<len(role_name)<=45): continue
                    if role_name.startswith(("招聘项目","具体岗位","岗位要求","招聘方向")): continue
                    item=record(page_company,role_name,final,record_source,cohort,5)
                    item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                    item["city"]=city_text or "全国"; item["confirmed_by"]=[record_source]
                    rows.append(item)
        if page_title and any(w.lower() in page_title.lower() for w in CONCRETE_WORDS):
            sc=score(page_title,final,"")
            if sc>=7 and any(x.lower() in final.lower() for x in ROLE_HREF):
                item=record(page_company,page_title,final,record_source,cohort,sc)
                if src.get("id")=="nowcoder":
                    page_text=p.page_text()
                    # Parse visible job fields from the public detail page; never infer a field when absent.
                    city_list=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外")
                    city_match=re.search(r"[（(]([^（）()]{2,12})[）)]",page_title)
                    if city_match and city_match.group(1).strip() in city_list:
                        item["city"]=city_match.group(1).strip()
                    degree=next((d for d in ("博士及以上","硕士及以上","本科及以上","大专及以上","博士","硕士","本科","大专") if d in page_text[:1800]),None)
                    if degree: item["degree"]=degree
                    salary_match=re.search(r"(\d{1,2}(?:-\d{1,2})?K\s*(?:\*\s*\d+薪)?|薪资面议)",page_text[:1500],re.I)
                    if salary_match: item["salary"]=re.sub(r"\s+"," ",salary_match.group(1)).strip()
                    deadline_match=re.search(r"投递时间[:：]?\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*[-—至到]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日",page_text[:8000])
                    if deadline_match:
                        yy,mm,dd=deadline_match.group(4),int(deadline_match.group(5)),int(deadline_match.group(6))
                        item["deadline"]=f"{yy}-{mm:02d}-{dd:02d}"
                        if item["deadline"] < TODAY:
                            item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=4
                    if "已结束" in page_text[:600]:
                        item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=4
                if "公开岗位" in record_source or "公开聚合" in record_source:
                    item["confirmed_by"]=[record_source]
                rows.append(item)
        if src.get("id")!="qiuzhaowang":
            rows.extend(table_records(page_company,final,p,cohort,record_source))
            rows.extend(embedded_records(page_company,final,p,cohort,record_source))
        for a in p.links:
            title=clean(a["text"]); href=canonical(urljoin(final,a["href"]))
            if not title or len(title)<4 or len(title)>100 or not href or href in seen: continue
            if not same_host(final,href): continue
            if any(x.lower() in title.lower() for x in NAV_BAD): continue
            sc=score(title,href,p.page_text())
            if sc<5: continue
            seen.add(href)
            if src.get("id")=="qiuzhaowang": continue
            link_record=record(page_company,title,href,record_source,cohort,sc)
            if "公开岗位" in record_source or "公开聚合" in record_source:
                link_record["confirmed_by"]=[record_source]
            if record_source=="应届生求职网公开岗位":
                link_record["status"]="pending_review"; link_record["granularity"]="review"; link_record["verification_score"]=5
            rows.append(link_record)
            if len(queue)<max_pages and len(visited)+len(queue)<max_pages and any(x.lower() in href.lower() for x in ROLE_HREF):
                queue.append(href)
    # de-dupe within source, preferring higher confidence
    best={}
    for r in rows:
        k=(r["company"],r["title"],r["source_url"])
        if k not in best or r["verification_score"]>best[k]["verification_score"]: best[k]=r
    return list(best.values()),visited

sources=json.loads(REGISTRY.read_text(encoding="utf-8"))
STATE_PATH=ROOT/"data/collection-state.json"
try:
    state=json.loads(STATE_PATH.read_text(encoding="utf-8"))
except Exception:
    state={"qiuzhaowang_next_page":21}
rotation_start=int(state.get("qiuzhaowang_next_page",21))
rotation_pages=[]
page_num=rotation_start
while len(rotation_pages)<10:
    if page_num>192: page_num=11
    if page_num not in range(1,11) and page_num not in rotation_pages:
        rotation_pages.append(page_num)
    page_num+=1
qiuzhaowang_pages=list(range(1,11))+rotation_pages
for source in sources:
    if source.get("id")=="qiuzhaowang":
        source["seed_urls"]=[f"https://qiuzhaowang.com/latest?page={n}&year=2027" for n in qiuzhaowang_pages]
        source["max_pages"]=20
        source["max_candidates"]=3000
enabled=[s for s in sources if s.get("enabled") and s.get("access") in {"public","public_api"}]
all_rows=[]; results=[]; active=review=0; qiuzhaowang_visited=set()
with ThreadPoolExecutor(max_workers=min(12,len(enabled) or 1)) as pool:
    futures={pool.submit(collect_source,s):s for s in enabled}
    for fut in as_completed(futures):
        s=futures[fut]
        try: rows,visited=fut.result()
        except Exception: rows,visited=[],set()
        if s.get("id")=="qiuzhaowang": qiuzhaowang_visited=visited
        all_rows.extend(rows)
        a=sum(r["status"]=="active" for r in rows); rr=sum(r["status"]!="active" for r in rows)
        active+=a; review+=rr
        results.append({"id":s["id"],"company":s["company"],"pages_visited":len(visited),"records_found":len(rows),
                        "active_candidates":a,"pending_review":rr})
results.sort(key=lambda x:x["company"])
rot_seen=set()
for visited_url in qiuzhaowang_visited:
    match=re.search(r"[?&]page=(\d+)",visited_url)
    if match and int(match.group(1)) in rotation_pages: rot_seen.add(int(match.group(1)))
next_page=rotation_start
while next_page in rot_seen:
    next_page+=1
    if next_page>192: next_page=11
state["qiuzhaowang_next_page"]=next_page
state["qiuzhaowang_last_pages"]=qiuzhaowang_pages
STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
INBOX.write_text(json.dumps(all_rows,ensure_ascii=False,indent=2),encoding="utf-8")
MANIFEST.write_text(json.dumps({"run_date":TODAY,"collector_version":"4.0","enabled_sources":len(enabled),
 "pagination_state":{"qiuzhaowang_pages":qiuzhaowang_pages,"qiuzhaowang_next_page":next_page},
 "records_found":len(all_rows),"active_candidates":active,"pending_review":review,"results":results,
 "policy":"Public official pages only; no login/CAPTCHA/private API/anti-bot bypass.",
 "granularity":"Only high-confidence concrete role records are active; program/announcement/navigation pages remain pending_review."},
 ensure_ascii=False,indent=2),encoding="utf-8")
print(f"sources={len(enabled)} records={len(all_rows)} active_candidates={active} pending_review={review}")
