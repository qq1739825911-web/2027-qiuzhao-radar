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
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse,urlunparse,parse_qsl,urlencode
from urllib.request import Request,urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT=Path(__file__).resolve().parents[1]
REGISTRY=ROOT/"data/company_sources.json"; INBOX=ROOT/"data/jobs.inbox.json"; MANIFEST=ROOT/"data/collection-manifest.json"
TODAY=str(date.today())
STAMP=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
HEADERS={"User-Agent":"2027-Qiuzhao-Radar-PublicCollector/3.0"}

JOB_WORDS=("工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理")
CONCRETE_WORDS=("AIGC","AI产品","AI应用","视频","影视","创意制作","制作","创作","动画","短视频","剪辑","编导","文案","游戏","直播","摄影","工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","研究岗","行业研究","分析师","分析岗","管培生","管理培训生","培训生","咨询","教师","医生","护士","质量","项目经理","策划","内容运营","新媒体","客服","行政","人事","风控","风险","信贷","会计","策略","助理","客户经理","技术支持","商业分析","投资","证券","保险","精算","编辑","翻译","业务专员","运营岗","产品运营","品牌","公关","电商","市场拓展","Sales","Marketing","Analyst","Engineer","Intern","Associate","Manager","Specialist","Consultant","Accountant","Finance","Risk","Audit","Legal","Research","Strategy","Operations","Supply Chain","Procurement","Quality","Manufacturing","Mechanical","Electrical","Clinical","Medical","Education","Teacher","Editor","Content","Copywriter","Designer","Design","Product","Business","HR","Data Science","Data Analyst")
PROGRAM_WORDS=("招聘公告","招聘启事","校园招聘","校招公告","招聘简章","招聘信息","招聘计划","招聘项目","招聘专场","秋季招聘","春季招聘","招聘通知")
NAV_BAD=("登录","注册","首页","关于我们","新闻","公告","联系我们","隐私","下载","帮助","返回","信用卡产品","借记卡产品","自营金融","产品服务","理财","基金产品")
ROLE_HREF=("job","position","career","campus","recruit","zhaopin","jobs","vacancy","detail","post")

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.text=[]; self.tables=[]; self.table_links=[]; self._row_links=[]; self._in_row=False; self.scripts=[]; self.page_title=""; self._in_title=False; self._script_buf=None; self._a=None; self._td=None; self._row=[]; self.headings=[]; self._heading_buf=None; self._heading_tag=None
    def handle_starttag(self,tag,attrs):
        d=dict(attrs); tag=tag.lower()
        if tag=="img" and d.get("alt"):
            alt=re.sub(r"\s+"," ",d.get("alt","")).strip()
            if alt: self.text.append(alt)
        if tag=="a":
            self._a={"href":d.get("href",""),"text":""}
            if self._in_row and d.get("href"): self._row_links.append(d.get("href",""))
        if tag=="title": self._in_title=True
        if tag in ("h1","h2","h3","h4"):
            self._heading_buf=[]; self._heading_tag=tag
        if tag in ("td","th"): self._td=""
        if tag=="tr": self._row=[]; self._row_links=[]; self._in_row=True
        if tag=="script": self._script_buf=[]
    def handle_data(self,data):
        s=re.sub(r"\s+"," ",data).strip()
        if s: self.text.append(s)
        if self._a is not None: self._a["text"]+=(" "+s if s else "")
        if self._script_buf is not None: self._script_buf.append(data)
        if self._in_title: self.page_title+=data
        if self._heading_buf is not None: self._heading_buf.append(data)
        if self._td is not None: self._td+=(" "+s if s else "")
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag=="a" and self._a is not None: self.links.append(self._a); self._a=None
        if tag=="title": self._in_title=False
        if tag in ("h1","h2","h3","h4") and self._heading_buf is not None and tag==self._heading_tag:
            heading=re.sub(r"\s+"," ","".join(self._heading_buf)).strip()
            if heading: self.headings.append(heading)
            self._heading_buf=None; self._heading_tag=None
        if tag=="script" and self._script_buf is not None:
            body="".join(self._script_buf).strip()
            if body and len(body)<5000000: self.scripts.append(body)
            self._script_buf=None
        if tag in ("td","th") and self._td is not None:
            self._row.append(self._td.strip()); self._td=None
        if tag=="tr" and self._row:
            self.tables.append(self._row); self.table_links.append(self._row_links); self._in_row=False
    def page_text(self): return " ".join(self.text)

def fetch(url):
    req=Request(url,headers=HEADERS)
    with urlopen(req,timeout=10) as r:
        raw=r.read(4_000_000); enc=r.headers.get_content_charset() or "utf-8"; final=r.geturl()
    return raw.decode(enc,errors="ignore"),final

def clean(s): return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()
def canonical(u):
    p=urlparse(u)
    # Preserve meaningful query parameters such as page=2 and jobId=123.
    # Strip only analytics/tracking parameters so paginated pages and distinct jobs do not collapse.
    tracking={"utm_source","utm_medium","utm_campaign","utm_term","utm_content","spm","from","referrer","trackingid","property","requestid","policytype","policyid","pagecode","pagesource","isinitiative","issuggest","jobrank","advid"}
    query=sorted((k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in tracking)
    return urlunparse((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/"),"",urlencode(query,doseq=True),""))
def same_host(a,b): return urlparse(a).netloc.lower()==urlparse(b).netloc.lower()
def year(s):
    """Return a cohort only when visible text explicitly provides one; never guess 2027."""
    text=s or ""
    m=re.search(r"(?<!\d)(20\d{2})\s*届",text)
    if m: return m.group(1)+"届"
    m=re.search(r"(?<!\d)(2[5-9])\s*届",text)
    if m: return "20"+m.group(1)+"届"
    # Recruitment titles often say "2027校园招聘/2027校招" without the 届 suffix.
    m=re.search(r"(?<!\d)(20\d{2})(?:年)?\s*(?:校园招聘|校招公告|校招职位|秋季招聘|秋招|春招)",text)
    if m: return m.group(1)+"届"
    m=re.search(r"(?<!\d)(20\d{2})\s*年(?:应届|毕业)",text)
    if m: return m.group(1)+"届"
    return ""
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

def is_direct_detail_url(url):
    p=urlparse(url or "")
    path=p.path.lower()
    if re.search(r"/jobs/detail/\d+",path): return True
    if re.search(r"/jobs/detail/(?:graduate|intern)/[0-9a-f-]{20,}",path): return True
    if re.search(r"/jobs/hr/\d+",path) and re.search(r"(?:^|&)jobid=\d+",p.query.lower()): return True
    if re.search(r"/jobdetail/\d+",path): return True
    if path.endswith("/jobdesc.html") and re.search(r"(?:^|&)postid=[^&]+",p.query.lower()): return True
    if path.endswith("/web/position/detail") and re.search(r"(?:^|&)jobunionid=\d+",p.query.lower()): return True
    if re.search(r"/campus/position/\d+/detail",path): return True
    if re.search(r"/position/\d+/detail",path): return True
    if re.search(r"/positions/\d+(?:/|$)",path): return True
    if re.search(r"/job/detail/\d+",path): return True
    if re.search(r"/job/position/\d+",path): return True
    if re.search(r"/job-\d{3}-\d{3}-\d+\.html$",path): return True
    return False

TITLE_NOISE = re.compile(r"(?:\d+\s*分钟前在线|HR\s*反馈率|反馈率\s*[:：]?|反馈时长\s*[:：]?|招聘经理|人事经理|招聘专员|今日活跃|刚刚活跃)",re.I)
ROLE_TERMS=("AIGC","AI产品","AI应用","视频","影视","创意制作","制作","创作","动画","短视频","剪辑","编导","文案","游戏","直播","摄影","工程师","开发","研发","算法","数据","产品经理","产品专员","运营","设计师","设计","视觉","交互","销售","营销","市场","财务","审计","法务","人力资源","供应链","采购","机械","电气","嵌入式","测试","运维","安全","研究员","分析师","管培生","管理培训生","咨询","教师","医生","护士","质量","项目经理","策划","内容","新媒体","客服","行政","人事","风控","信贷","会计","策略","助理","客户经理","技术支持","商业分析","投资","证券","保险","精算","编辑","翻译","品牌","公关","电商","市场拓展","芯片","硬件","软件","制造","HR","Engineer","Designer","Analyst","Product","Operations")

def clean_job_title(title, source_id=""):
    """Remove visible platform and HR-status pollution without inventing a job title."""
    t=clean(title)
    if source_id=="nowcoder":
        m=re.match(r"^(.+?)_[^_]+?(?:校招|实习|社招)_牛客网$",t)
        if m: t=m.group(1)
        t=re.sub(r"^(?:20\d{2}|\d{2})届(?:校招)?[-—_\s]*","",t)
    if source_id=="yingjiesheng" and "招聘_" in t:
        t=t.split("招聘_",1)[0].strip()
    t=re.sub(r"\s*[_|｜]\s*(?:牛客网|应届生求职网).*$","",t)
    t=re.sub(r"\s*[-—]\s*(?:牛客网|应届生求职网).*$","",t)
    t=re.sub(r"\s*\d+\s*分钟前在线.*$","",t)
    t=re.sub(r"\s*[·|｜]\s*HR\s*反馈率.*$","",t,flags=re.I)
    t=re.sub(r"\s*(?:HR\s*)?反馈率\s*[:：]?.*$","",t,flags=re.I)
    t=re.sub(r"\s*反馈时长\s*[:：]?.*$","",t,flags=re.I)
    t=re.sub(r"\s*(?:招聘经理|人事经理|招聘专员).*$","",t)
    t=re.sub(r"^(?:招聘岗位|职位|岗位名称)\s*[:：]\s*","",t)
    t=re.sub(r"\s*[（(]J\d{3,}[）)]\s*$","",t,flags=re.I)
    t=re.sub(r"\s+"," ",t).strip(" -—_|｜")
    return t

def is_concrete_role_title(title):
    t=clean_job_title(title)
    if len(t)<2 or len(t)>90 or TITLE_NOISE.search(t): return False
    if any(w in t for w in ("招聘公告","招聘简章","招聘计划","校园招聘会","招聘宣讲","专业目录","需求专业","岗位要求","任职要求","职位描述","职位亮点","公司信息","查看更多","点击查看")): return False
    return any(w.lower() in t.lower() for w in ROLE_TERMS)

def extract_section(text, starts, ends, limit=5000):
    start_re="(?:"+"|".join(starts)+r")\s*[:：]?\s*"
    m=re.search(start_re,text or "",re.I)
    if not m: return ""
    tail=(text or "")[m.end():]
    positions=[]
    for end in ends:
        hit=re.search(r"\s*(?:"+end+r")\s*[:：]?\s*",tail,re.I)
        if hit: positions.append(hit.start())
    if positions: tail=tail[:min(positions)]
    return re.sub(r"\s+"," ",tail).strip(" :-—")[:limit].strip()

def extract_yingjiesheng_fields(raw_title, text, url):
    """Extract visible fields from public Yingjiesheng job-detail pages."""
    fields={}; title=""; company=""
    path=urlparse(url).path.lower()
    if "/jobdetail/" in path:
        if "招聘_" in raw_title:
            title,rest=raw_title.split("招聘_",1)
            title=title.strip()
            company=re.split(r"招聘信息|_应届生求职网",rest,1)[0].strip(" _-")
        else:
            title=raw_title.split("_",1)[0].strip()
    else:
        m=re.search(r"职位\s*[:：]\s*(.+?)\s+发布时间",text or "")
        if m: title=m.group(1).strip()
        m=re.search(r"\[(?:全国|北京|上海|广州|深圳|杭州|南京|成都|武汉|西安)[^\]]*\]\s*(.+?)\s+(?:职位\s*[:：]|招聘岗位)",text or "")
        if m: company=m.group(1).strip()
        if not company:
            m=re.search(r"(?:^|\s)([^\s]{2,40}有限公司)\s+(?:职位\s*[:：]|招聘岗位)",text or "")
            if m: company=m.group(1).strip()
    fields["title"]=clean_job_title(title,"yingjiesheng")
    fields["company"]=company
    top=(text or "")[:3500]
    salary=re.search(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|每月|月)?",top)
    if salary: fields["salary"]=re.sub(r"\s+","",salary.group(0))
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    cities=("上海","北京","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨")
    city=next((name for name in cities if re.search(name+r"(?:[-·区市]|\s)",top[:1800])), "")
    if city: fields["city"]=city
    pub=re.search(r"发布时间\s*[:：]?\s*(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?",top)
    if pub: fields["publish_date"]=f"{pub.group(1)}-{int(pub.group(2)):02d}-{int(pub.group(3)):02d}"
    deadline=re.search(r"(?:截止时间|报名截止|投递截止|截止日期)\s*[:：]?\s*(20\d{2})[-年](\d{1,2})[-月](\d{1,2})日?",text or "")
    if deadline: fields["deadline"]=f"{deadline.group(1)}-{int(deadline.group(2)):02d}-{int(deadline.group(3)):02d}"
    fields["benefits"]=extract_section(text,("职位亮点","职位诱惑","福利待遇","薪酬福利"),("职位描述","岗位职责","工作职责","职位要求","任职要求","岗位要求","公司信息","单位简介"),1600)
    fields["responsibilities"]=extract_section(text,("岗位职责","工作职责","工作内容"),("岗位要求","任职要求","任职资格","加分项","职位亮点","职位诱惑","工作时间","公司信息","单位简介"),3500)
    fields["requirements"]=extract_section(text,("岗位要求","任职要求","任职资格"),("加分项","职位亮点","职位诱惑","工作时间","工作地址","公司地址","公司信息","单位简介","投递说明","上一条","下一条"),3500)
    fields["description"]=extract_section(text,("职位描述","招聘岗位"),("公司信息","单位简介","上一条","下一条","Top"),7000)
    fields["company_info"]=extract_section(text,("公司信息","单位简介"),("上一条","下一条","登录","Top"),1800)
    addr=re.search(r"(?:公司地址|工作地址|地址)\s*[:：]?\s*(.{4,120}?)(?=\s*(?:投递简历|职位亮点|职位描述|公司信息|单位简介|上一条|下一条|Top|$))",text or "",re.I)
    if addr: fields["address"]=addr.group(1).strip()
    for label in ("实习","全职","兼职","校招"):
        if label in top:
            fields["job_type"]=label; break
    exp=re.search(r"(?:工作经验|经验要求|经验)\s*[:：]?\s*(不限|无经验|\d+[-—至]\d+年|\d+年以上|应届毕业生)",top)
    if exp: fields["experience"]=exp.group(1)
    headcount=re.search(r"(?:招|招聘人数\s*[:：]?)\s*(\d+)\s*人",top)
    if headcount: fields["headcount"]=headcount.group(1)
    return fields

def extract_detail_fields_generic(text, raw_title=""):
    fields={}
    top=(text or "")[:4500]
    degree=next((d for d in ("博士研究生及以上","硕士研究生及以上","本科及以上","大专及以上","博士及以上","硕士及以上","本科","硕士","博士","大专") if d in top), "")
    if degree: fields["degree"]=degree
    salary=re.search(r"(?:薪资|月薪)\s*[:：]?\s*(\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|月)?|薪资面议)",top)
    if not salary: salary=re.search(r"\b(\d+(?:\.\d+)?\s*(?:K|k|千|万)(?:\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:K|k|千|万))?(?:\s*/\s*月|/月|月)?)",top)
    if salary: fields["salary"]=re.sub(r"\s+","",salary.group(1))
    cities=("北京","上海","深圳","广州","杭州","南京","成都","武汉","西安","苏州","合肥","重庆","天津","青岛","长沙","济南","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外")
    city=next((name for name in cities if re.search(r"(?:^|\s|[-·])"+name+r"(?:$|\s|[-·区市])",top[:1800])), "")
    if city: fields["city"]=city
    cohort=year(raw_title+" "+top)
    if cohort:
        fields["cohort"]=cohort; fields["cohort_evidence"]="page_text"; fields["cohort_confirmed"]=True
    dr=re.search(r"投递时间\s*[:：]?\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*[-—至到]\s*(20\d{2})年(\d{1,2})月(\d{1,2})日",text or "")
    if dr: fields["deadline"]=f"{dr.group(4)}-{int(dr.group(5)):02d}-{int(dr.group(6)):02d}"
    fields["responsibilities"]=extract_section(text,("岗位职责","工作职责","工作内容"),("岗位要求","任职要求","职位亮点","职位描述","公司信息","上一条","下一条"),3500)
    fields["requirements"]=extract_section(text,("岗位要求","任职要求","任职资格"),("加分项","职位亮点","工作时间","公司信息","上一条","下一条"),3500)
    fields["benefits"]=extract_section(text,("职位亮点","职位诱惑","福利待遇"),("岗位职责","工作职责","职位描述","公司信息","上一条","下一条"),1800)
    fields["description"]=extract_section(text,("职位描述","岗位职责"),("公司信息","上一条","下一条","Top"),6500)
    fields["company_info"]=extract_section(text,("公司信息","公司介绍"),("查看其他","上一条","下一条","Top"),1800)
    addr=re.search(r"(?:公司地址|工作地址|地址)\s*[:：]?\s*(.{4,120}?)(?=\s*(?:投递简历|职位亮点|职位描述|公司信息|上一条|下一条|Top|$))",text or "",re.I)
    if addr: fields["address"]=addr.group(1).strip()
    exp=re.search(r"(?:工作经验|经验要求|经验)\s*[:：]?\s*(不限|无经验|\d+[-—至]\d+年|\d+年以上|应届毕业生)",top)
    if exp: fields["experience"]=exp.group(1)
    if "全职" in top: fields["job_type"]="全职"
    elif "实习" in top: fields["job_type"]="实习"
    return fields

def merge_detail_fields(row, fields):
    for key,value in fields.items():
        if value not in (None,"",[],{}): row[key]=value
    return row

def record(company,title,url,source,cohort,confidence,program="",cohort_evidence=""):
    title=clean_job_title(title)
    direct=is_direct_detail_url(url)
    cohort_ok=bool(cohort and cohort.strip() not in ("待核验","未知") and cohort_evidence)
    title_ok=is_concrete_role_title(title)
    status="active" if confidence>=7 and direct and cohort_ok and title_ok else "pending_review"
    return {"id":make_id(company,title,url),"company":company,"title":title,
      "category":category(title),"city":"全国","enterprise_type":"待核验","degree":"待核验",
      "salary":"","publish_date":"","deadline":"","last_verified":"","last_collected":TODAY,"source":source,
      "source_url":url,"keywords":re.findall(r"[A-Za-z0-9+#.-]{2,}|[\u4e00-\u9fff]{2,8}",title)[:14],
      "status":status,"demo":False,"confirmed_by":[source],"cohort":cohort or "",
      "cohort_evidence":cohort_evidence or "","cohort_confirmed":bool(cohort_evidence),
      "program":program,"collector":"public-html-v4","granularity":"job" if status=="active" else "review",
      "verification_score":confidence,"description":"","responsibilities":"","requirements":"","benefits":"",
      "company_info":"","address":"","work_time":"","experience":"","job_type":"","headcount":""}

def embedded_records(company,source,parser,cohort,source_label=None,cohort_evidence=""):
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
                        item=record(company,title,url,source_label or company+"官方招聘",cohort,sc,cohort_evidence=cohort_evidence)
                        if source_label=="应届生求职网公开岗位":
                            item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=5
                        if source_label and "公开" in source_label: item["confirmed_by"]=[source_label]
                        out.append(item)
                stack.extend(v for v in node.values() if isinstance(v,(dict,list)))
            elif isinstance(node,list): stack.extend(v for v in node if isinstance(v,(dict,list)))
    return out

def table_records(company,source,parser,cohort,source_label=None,cohort_evidence=""):
    out=[]
    cities=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外","全国")
    degree_words=("博士","硕士","本科","大专")
    for row_index,row in enumerate(parser.tables):
        if len(row)<2: continue
        joined=" | ".join(row)
        if any(x in joined for x in NAV_BAD): continue
        # Keep actual role names, not category headings or copied qualification paragraphs.
        role_cells=row[1:] if source_label and "公开" in source_label else row
        role=next((x for x in role_cells if 2<len(x)<=45
                   and any(w.lower() in x.lower() for w in CONCRETE_WORDS)
                   and not x.endswith(("类","方向","相关专业","等相关专业","专业"))
                   and not x.startswith(("具有","负责","熟悉","掌握","参与","岗位","要求","本科","硕士","博士"))),None)
        if not role: continue
        role=re.sub(r"\s*更新[:：]?\s*.*$","",role).strip()
        if not role: continue
        employer=company
        if source_label and "公开" in source_label:
            role_idx=next((i for i,x in enumerate(row) if x==role),-1)
            city_tokens=("北京","上海","深圳","广州","杭州","南京","苏州","成都","西安","武汉","长沙","重庆","天津","合肥","济南","青岛","烟台","东莞","厦门","福州","郑州","宁波","无锡","南昌","哈尔滨","海外","全国","台北","新竹")
            bad_terms=("2027","2026","2025","校招","校园招聘","实习","社招","投递","截止","更新","登录","未公布","已结束","招聘对象","工作地点","届次批次","岗位","职位","薪资")
            candidates=[]
            for i,candidate in enumerate(row):
                candidate=clean(candidate)
                if i==role_idx or not (2<=len(candidate)<=60): continue
                if any(tok in candidate for tok in city_tokens+bad_terms) or any(tok in candidate for tok in degree_words): continue
                if candidate in NAV_BAD or candidate in PROGRAM_WORDS: continue
                if i>role_idx and any(w.lower() in candidate.lower() for w in CONCRETE_WORDS): continue
                if re.fullmatch(r"[\d年月日./:-]+",candidate): continue
                candidates.append(candidate)
            if candidates:
                employer=candidates[0]
                employer=re.split(r"\s+(?:金融|互联网/科技/AI|中国大陆|香港|美国|英国|新加坡|其他|金融/银行/证券|生产制造/工业|汽车/新能源|物流/供应链|快消/零售|生物医药/制药|教育/培训|房地产/建筑|咨询/四大|外企/合资|私企/民企|央国企/事业单位)\b",employer,1)[0].strip()
                employer=re.sub(r"(世界500强|知名互联网|独角兽|半导体大厂|头部外企|新势力车企|大模型公司|行业领先|上市公司|福利完善|成长空间大|团队氛围好).*$","",employer).strip(" ·-_")
            if not employer or employer==company: continue
        role_url=source
        row_links=parser.table_links[row_index] if row_index<len(getattr(parser,"table_links",[])) else []
        for href in row_links:
            full=urljoin(source,href)
            if urlparse(full).scheme in ("http","https") and any(x.lower() in full.lower() for x in ROLE_HREF):
                role_url=canonical(full); break
        score_value=5 if source_label and "公开" in source_label else 8
        r=record(employer,role,role_url,source_label or company+"官方招聘",cohort,score_value,cohort_evidence=cohort_evidence)
        if source_label and "公开" in source_label:
            r["status"]="pending_review"; r["granularity"]="review"; r["verification_score"]=5
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
    diagnostics={"pages_attempted":0,"pages_failed":0,"html_bytes":0,"links_seen":0,"tables_seen":0,"embedded_json_blocks":0,"role_like_links":0,"sample_title":"","last_error":"","parser_modes":[]}
    while queue and len(visited)<max_pages and len(rows)<max_candidates:
        rows_before_page=len(rows)
        page=queue.pop(0)
        cp=(page.split("#",1)[0] if src.get("id")=="qiuzhaowang" else canonical(page))
        if cp in visited: continue
        if src.get("id")=="qiuzhaowang" and visited: time.sleep(0.5)
        diagnostics["pages_attempted"]+=1
        try: html,final=fetch(page)
        except Exception as exc:
            diagnostics["pages_failed"]+=1
            diagnostics["last_error"]=f"{type(exc).__name__}: {str(exc)[:140]}"
            continue
        visited.add(cp)
        diagnostics["html_bytes"]+=len(html.encode("utf-8",errors="ignore"))
        p=Parser(); p.feed(html)
        page_text=p.page_text()
        raw_page_title=clean(p.page_title)
        if not diagnostics["sample_title"]: diagnostics["sample_title"]=raw_page_title[:140]
        diagnostics["links_seen"]+=len(p.links); diagnostics["tables_seen"]+=len(p.tables); diagnostics["embedded_json_blocks"]+=len(p.scripts)
        detail_heading=""
        if is_direct_detail_url(final):
            detail_heading=next((clean_job_title(head,src.get("id","")) for head in p.headings
                                 if is_concrete_role_title(clean_job_title(head,src.get("id","")))), "")
        explicit_cohort=year(raw_page_title+" "+page_text+" "+" ".join(p.headings))
        source_cohort=src.get("cohort","") if src.get("type")=="official_campus" else ""
        if src.get("id")=="qiuzhaowang" and re.search(r"(?:[?&])year=2027(?:&|$)",page):
            source_cohort="2027届"
        cohort=explicit_cohort or source_cohort
        cohort_evidence=("page_text" if explicit_cohort else ("source_registry" if source_cohort else ""))
        page_company=company
        detail_fields={}
        if src.get("id")=="yingjiesheng":
            detail_fields=extract_yingjiesheng_fields(raw_page_title,page_text,final)
            if detail_fields.get("title"): raw_page_title=detail_fields["title"]
            if detail_fields.get("company"): page_company=detail_fields["company"]
        record_source=company+"官方招聘"
        if src.get("id")=="nowcoder":
            # Job detail title pattern: role_title_company校招_牛客网
            m=re.search(r"_([^_]+?)(?:校招|实习|社招)_牛客网", raw_page_title)
            if m: page_company=m.group(1).strip()
            record_source="牛客公开岗位"
        elif src.get("id")=="yingjiesheng":
            # Company/title were extracted from visible fields above where available.
            record_source="应届生求职网公开岗位"
        elif src.get("id")=="qiuzhaowang":
            parts=re.split(r"[|｜]",raw_page_title)
            if "/jobs/" in urlparse(final).path and parts: page_company=parts[0].strip()
            record_source="秋招网公开聚合"
        elif src.get("id")=="mianlingai":
            record_source="面灵AI公开聚合"
        elif src.get("id")=="91bangtu":
            record_source="91邦途公开聚合"
        page_title=clean_job_title(detail_fields.get("title") or detail_heading or raw_page_title,src.get("id",""))
        if src.get("id")=="nowcoder":
            page_title=clean_job_title(detail_heading or p.page_title,"nowcoder")
        if is_direct_detail_url(final):
            for k,v in extract_detail_fields_generic(page_text,detail_heading or raw_page_title).items():
                detail_fields.setdefault(k,v)
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
                cohort=year(chunk) or ("2027届" if "year=2027" in page else "")
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
        if page_title and is_concrete_role_title(page_title):
            sc=score(page_title,final,page_text)
            if sc>=5 and is_direct_detail_url(final):
                item=record(page_company,page_title,final,record_source,cohort,sc,cohort_evidence=cohort_evidence)
                merge_detail_fields(item,detail_fields)
                item["cohort"]=item.get("cohort") or cohort or ""
                item["cohort_evidence"]=item.get("cohort_evidence") or cohort_evidence or ""
                item["cohort_confirmed"]=bool(item.get("cohort_confirmed") or item["cohort_evidence"])
                if item.get("deadline") and item["deadline"] < TODAY:
                    item["status"]="expired"; item["granularity"]="review"; item["verification_score"]=min(item.get("verification_score",5),4)
                if "已结束" in page_text[:1000]:
                    item["status"]="pending_review"; item["granularity"]="review"; item["verification_score"]=min(item.get("verification_score",5),4)
                item["last_collected"]=TODAY
                if is_direct_detail_url(final):
                    item["last_verified"]=TODAY; item["last_verified_at"]=STAMP
                    item["verification_status"]="link_alive"; item["verification_http_status"]=200
                    item["verification_note"]="采集器成功读取公开详情页；不代表雇主再次确认仍在招聘"
                rows.append(item)
        if src.get("id")!="qiuzhaowang":
            table_rows=table_records(page_company,final,p,cohort,record_source,cohort_evidence)
            embedded_rows=embedded_records(page_company,final,p,cohort,record_source,cohort_evidence)
            for extracted in table_rows+embedded_rows:
                if detail_fields: merge_detail_fields(extracted,detail_fields)
                extracted["last_collected"]=TODAY; extracted["last_verified"]=""
                extracted["cohort_evidence"]=extracted.get("cohort_evidence") or cohort_evidence or ""
                extracted["cohort_confirmed"]=bool(extracted.get("cohort_confirmed") or extracted["cohort_evidence"])
                if not is_direct_detail_url(extracted.get("source_url","")) or not extracted["cohort_confirmed"]:
                    extracted["status"]="pending_review"; extracted["granularity"]="review"
            rows.extend(table_rows); rows.extend(embedded_rows)
        for a in p.links:
            title=clean_job_title(a["text"],src.get("id","")); href=canonical(urljoin(final,a["href"]))
            if not title or len(title)<3 or len(title)>100 or not href or href in seen: continue
            if not is_concrete_role_title(title): continue
            if not same_host(final,href): continue
            if any(x.lower() in title.lower() for x in NAV_BAD): continue
            diagnostics["role_like_links"]+=1
            sc=score(title,href,page_text)
            if sc<5: continue
            seen.add(href)
            if src.get("id")=="qiuzhaowang": continue
            link_record=record(page_company,title,href,record_source,cohort,sc,cohort_evidence=cohort_evidence)
            link_record["last_collected"]=TODAY; link_record["last_verified"]=""
            if detail_fields: merge_detail_fields(link_record,detail_fields)
            if "公开岗位" in record_source or "公开聚合" in record_source:
                link_record["confirmed_by"]=[record_source]
            rows.append(link_record)
            if len(queue)<max_pages and len(visited)+len(queue)<max_pages and any(x.lower() in href.lower() for x in ROLE_HREF):
                queue.append(href)
        # If a public official page exposes a 2027 recruitment announcement but no job-level
        # data, retain exactly one clearly-labelled lead instead of mislabelling it as a job.
        # We do not synthesize salary, location, degree, or job descriptions for this lead.
        if len(rows)==rows_before_page:
            announcement_candidates=[]
            for link in p.links:
                ann_title=clean(link.get("text",""))
                if len(ann_title)<8 or len(ann_title)>180: continue
                if not re.search(r"(?:20\d{2}\s*届|20\d{2}年|27届)",ann_title): continue
                if not any(term in ann_title for term in PROGRAM_WORDS): continue
                if any(term.lower() in ann_title.lower() for term in NAV_BAD): continue
                ann_url=canonical(urljoin(final,link.get("href","")))
                if not ann_url or not same_host(final,ann_url): continue
                announcement_candidates.append((ann_title,ann_url))
            if not announcement_candidates and re.search(r"(?:20\d{2}\s*届|20\d{2}年|27届)",raw_page_title) and any(term in raw_page_title for term in PROGRAM_WORDS):
                announcement_candidates.append((raw_page_title,canonical(final)))
            if not announcement_candidates:
                # Some public campus portals render job cards via client-side JS but expose
                # an explicit 2027 cohort in their visible banner/alt text. Keep one landing-page
                # lead only; never interpret those banners as individual vacancies.
                visible_year=year(page_text)
                ann_terms=("校园招聘","招聘公告","招聘计划","应届生招聘","校招正式批","校招公告","秋季校园招聘")
                if visible_year and any(term in page_text for term in ann_terms):
                    announcement_candidates.append((f"{company} {visible_year}校园招聘入口线索",canonical(final)))
            portal_candidate=False
            if not announcement_candidates and src.get("type")=="official_campus" and source_cohort:
                title_year=year(raw_page_title)
                source_context=(final+" "+raw_page_title+" "+src.get("url","")).lower()
                campus_hints=("campus","校招","校园招聘","招聘项目","招聘公告","职位列表","招聘网站","search.html","/jobs","/job/")
                if (not title_year or title_year==source_cohort) and any(hint in source_context for hint in campus_hints) and not is_direct_detail_url(final):
                    announcement_candidates.append((f"{company} {source_cohort}官方招聘入口线索",canonical(final)))
                    portal_candidate=True
            if announcement_candidates:
                ann_title,ann_url=max(announcement_candidates,key=lambda item:(len(item[0]),item[0]))
                ann_year=year(ann_title) or cohort or source_cohort
                ann=record(page_company,ann_title,ann_url,record_source,ann_year,2,
                           program=("官方招聘入口线索" if portal_candidate else "招聘公告/计划线索"),
                           cohort_evidence=("source_registry" if portal_candidate else "visible_announcement_title"))
                ann["status"]="pending_review"
                ann["granularity"]="entry" if portal_candidate else "announcement"
                ann["verification_score"]=2
                ann["last_verified"]=""
                ann["last_collected"]=TODAY
                ann["announcement_only"]=not portal_candidate
                ann["entry_only"]=portal_candidate
                rows.append(ann)
    # de-dupe within source, preferring higher confidence
    best={}
    for r in rows:
        k=(r["company"],r["title"],r["source_url"])
        if k not in best or r["verification_score"]>best[k]["verification_score"]: best[k]=r
    best_rows=list(best.values())
    role_rows=[r for r in best_rows if r.get("granularity") not in {"announcement","entry"}]
    announcement_rows=[r for r in best_rows if r.get("granularity")=="announcement"]
    entry_rows=[r for r in best_rows if r.get("granularity")=="entry"]
    diagnostics["role_records"]=len(role_rows)
    diagnostics["announcement_records"]=len(announcement_rows)
    diagnostics["portal_entries"]=len(entry_rows)
    diagnostics["source_status"]="ok" if role_rows else ("announcement_only" if announcement_rows else ("entry_only" if entry_rows else ("reachable_no_records" if visited else "access_error")))
    diagnostics["parser_modes"]=[name for name,count in (("links",diagnostics["links_seen"]),("tables",diagnostics["tables_seen"]),("embedded_json",diagnostics["embedded_json_blocks"])) if count]
    return best_rows,visited,diagnostics

def main():
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
            try: rows,visited,diag=fut.result()
            except Exception as exc:
                rows,visited=[],set()
                diag={"pages_attempted":1,"pages_failed":1,"html_bytes":0,"links_seen":0,"tables_seen":0,
                      "embedded_json_blocks":0,"role_like_links":0,"sample_title":"","last_error":f"{type(exc).__name__}: {str(exc)[:140]}",
                      "parser_modes":[],"source_status":"access_error"}
            if s.get("id")=="qiuzhaowang": qiuzhaowang_visited=visited
            all_rows.extend(rows)
            a=sum(r["status"]=="active" for r in rows); rr=sum(r["status"]!="active" for r in rows)
            active+=a; review+=rr
            results.append({"id":s["id"],"company":s["company"],"pages_visited":len(visited),"records_found":len(rows),
                            "active_candidates":a,"pending_review":rr,
                            "pages_attempted":diag.get("pages_attempted",0),"pages_failed":diag.get("pages_failed",0),
                            "html_bytes":diag.get("html_bytes",0),"links_seen":diag.get("links_seen",0),
                            "tables_seen":diag.get("tables_seen",0),"embedded_json_blocks":diag.get("embedded_json_blocks",0),
                            "role_like_links":diag.get("role_like_links",0),"role_records":diag.get("role_records",0),
                            "announcement_records":diag.get("announcement_records",0),"portal_entries":diag.get("portal_entries",0),
                            "sample_title":diag.get("sample_title",""),
                            "last_error":diag.get("last_error",""),"parser_modes":diag.get("parser_modes",[]),
                            "source_status":diag.get("source_status","access_error")})
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
    

if __name__ == "__main__":
    main()
