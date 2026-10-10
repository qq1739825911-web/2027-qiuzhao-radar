"""Static checks for the job-discovery UI and in-site job-detail contract."""
from html.parser import HTMLParser
from pathlib import Path

HTML = Path("index.html").read_text(encoding="utf-8")

class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])

parser = IdParser()
parser.feed(HTML)

def check(name, ok):
    if not ok:
        raise SystemExit(f"FAILED: {name}")
    print(f"PASS: {name}")

check("job detail modal exists", {"jobDetailModal", "jobDetailContent"}.issubset(parser.ids))
check("role title opens in-site details", 'data-detail-id="' in HTML and "function openJobDetail(id" in HTML)
check("detail modal supports deep-link/back navigation", '#job/' in HTML and 'addEventListener("popstate"' in HTML)
check("close button and Escape key are wired", "data-detail-close" in HTML and 'e.key==="Escape"' in HTML)
check("priority and lead buckets are separate", "可优先核验" in HTML and "待核验线索" in HTML and 'data-bucket="priority"' in HTML and 'data-bucket="lead"' in HTML)
check("priority gate requires explicit cohort evidence", 'function has2027Evidence(x)' in HTML and '"source_registry"' not in HTML[HTML.index('function has2027Evidence(x)'):HTML.index('function syncFavoriteButton()')])
check("priority gate rejects old source observations", 'seenCutoff' in HTML and 'x.last_seen||x.last_collected' in HTML)
check("in-site detail renders salary and location", '"薪资"' in HTML and '"工作城市"' in HTML and "工作地址：</b>" in HTML)
check("in-site detail renders education and job descriptions", '"学历要求"' in HTML and "job.responsibilities" in HTML and "job.requirements" in HTML)
check("company and source CTA stay available", "job.company" in HTML and ("打开原始职位页面" in HTML or "前往官方招聘渠道核对" in HTML))
check("missing values are not invented", "系统不会用猜测补齐" in HTML)
print("FRONTEND CONTRACT TESTS PASSED")
