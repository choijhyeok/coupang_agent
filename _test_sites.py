import httpx, re

pid, iid, vid = "7209410749", "18240765929", "85387730961"

# 1. alltimeprice product page
r = httpx.get(f"https://alltimeprice.com/product/?pid={pid}-{iid}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10, follow_redirects=True)
print(f"alltimeprice product: status={r.status_code} size={len(r.text)}")
if r.status_code == 200 and len(r.text) > 5000:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", r.text)
    print(f"  title: {title_m.group(1)[:80] if title_m else '?'}")
    for kw in ["현재", "최저", "최고", "평균", "오늘"]:
        found = re.findall(rf"{kw}[^<]*?(\d{{1,3}}(?:,\d{{3}})+)", r.text)
        if found:
            print(f"  {kw}: {found[:3]}")
    prices = re.findall(r'(\d{1,3}(?:,\d{3})+)\s*원', r.text)
    print(f"  all prices: {prices[:8]}")

# 2. lowchart detail - extract price history data
r2 = httpx.get(f"https://www.lowchart.com/{pid}-{iid}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10, follow_redirects=True)
print(f"\nlowchart product: status={r2.status_code} size={len(r2.text)}")
if r2.status_code == 200:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", r2.text)
    print(f"  title: {title_m.group(1)[:80] if title_m else '?'}")
    for kw in ["현재", "최저", "최고", "평균"]:
        found = re.findall(rf"{kw}[^<]*?(\d{{1,3}}(?:,\d{{3}})+)", r2.text)
        if found:
            print(f"  {kw}: {found[:3]}")
    # Check for chart/graph data
    has_chart = "chart" in r2.text.lower() or "graph" in r2.text.lower()
    print(f"  has_chart_ref: {has_chart}")

# 3. geniealert detail - extract price info
r3 = httpx.get(f"https://geniealert.co.kr/goods/detail/{pid}?itemId={iid}&vendorItemId={vid}", headers={"User-Agent": "Mozilla/5.0"}, timeout=10, follow_redirects=True)
print(f"\ngeniealert product: status={r3.status_code} size={len(r3.text)}")
if r3.status_code == 200 and len(r3.text) > 10000:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", r3.text)
    print(f"  title: {title_m.group(1)[:80] if title_m else '?'}")
    for kw in ["현재", "최저", "최고", "평균", "할인"]:
        found = re.findall(rf"{kw}[^<]*?(\d{{1,3}}(?:,\d{{3}})+)", r3.text)
        if found:
            print(f"  {kw}: {found[:3]}")
