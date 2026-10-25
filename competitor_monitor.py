#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
competitor_monitor.py — 다이얼독 경쟁사 주간 스냅샷 · diff

매주 한 번 실행하면
  1) App Store(iTunes Lookup API + 스토어 페이지의 버전 기록), Google Play(google-play-scraper), 홈페이지 텍스트를 수집해
  2) snapshots/YYYY-MM-DD.json 으로 저장하고
  3) 직전 스냅샷과 비교한 결과를 reports/YYYY-MM-DD_diff.md 로 씁니다. (stdout에도 출력)

사용법
  python competitor_monitor.py                 # 수집 + 직전 스냅샷과 diff
  python competitor_monitor.py --init          # 첫 실행: 스냅샷만 저장하고 전체 인벤토리 출력
  python competitor_monitor.py --config apps.json --dir ./data
  python competitor_monitor.py --selftest      # 네트워크 없이 diff 로직 자체 점검

의존성: Python 3.9+. Google Play는 `pip install google-play-scraper` (없으면 iOS·홈페이지만 수집)
선택: 환경변수 SLACK_WEBHOOK (리포트 요약 전송), S3_BUCKET (스냅샷·리포트 S3 보관, boto3 필요)
"""
import argparse
import datetime as dt
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
KST = dt.timezone(dt.timedelta(hours=9))

DEFAULT_CONFIG = {
    "country": "kr",
    # 이름은 가나다순으로 정렬해 출력합니다. ios: App Store ID, gp: Google Play 패키지명(모르면 null)
    "apps": [
        {"name": "그루머노트",        "ios": 1580697862, "gp": None},
        {"name": "다이얼독 비즈",      "ios": 6445974977, "gp": None, "self": True},
        {"name": "다이얼독 회원용",    "ios": 1598230149, "gp": None, "self": True},
        {"name": "똑독",             "ios": 6754978978, "gp": "net.knockdog.petcampus.v2"},
        {"name": "멍텔모아",          "ios": 6755710326, "gp": "com.knowledgeware.modelexecutor80.dogtellmoa_2377993759"},
        {"name": "멍투데이",          "ios": 6748244494, "gp": None},
        {"name": "멍투데이 비즈니스",   "ios": 6748244482, "gp": None},
        {"name": "반비서 고객용",      "ios": 6740599562, "gp": None},
        {"name": "반짝 미용파트너",    "ios": 1605723210, "gp": "com.banjjakpet.beautypartner", "low": True},
        {"name": "우주펫",            "ios": 1597844235, "gp": "com.universepet.universepet"},
        {"name": "우주펫파트너",       "ios": 6673888931, "gp": "com.universepet.universepetPartners.universepet_partners"},
        {"name": "키즈노트",          "ios": 527574743,  "gp": "com.vaultmicro.kidsnote", "low": True},
        {"name": "티피매니저",         "ios": 1639964013, "gp": "com.fridgecrew.labrador.mobile.webapp"},
        {"name": "펫차트",            "ios": 6746713028, "gp": None},
        {"name": "포들리 파트너스",    "ios": 1609215721, "gp": None, "low": True},
        {"name": "헬로티피",          "ios": 1671905384, "gp": "com.teepee.guardian"},
    ],
    # 홈페이지: 본문 텍스트를 저장하고 바뀐 줄만 보여줍니다. JS로만 그려지는 페이지(우피·노션)는 본문이 거의 안 잡힙니다.
    "pages": [
        {"name": "그루머노트 홈",      "url": "https://groomernote.com/"},
        {"name": "그루머노트 요금제",   "url": "https://groomernote.com/pricing"},
        {"name": "똑독 홈",           "url": "https://home.knockdog.net/"},
        {"name": "멍텔모아 파트너",     "url": "https://mungtelmoa.com/partner"},
        {"name": "멍투데이 홈",        "url": "https://mungtoday.com/"},
        {"name": "반비서 홈",          "url": "https://banbiseo.com/"},
        {"name": "우주펫 파트너",       "url": "https://www.spacepet.site/partner"},
        {"name": "키즈노트 애견유치원",  "url": "https://www.with-kidsnote.com/updatenotedog"},
        {"name": "티피 홈",            "url": "https://teepee.co.kr/"},
        {"name": "티피 소개(우피)",     "url": "https://teepee.oopy.io/"},
        {"name": "펫차트 공지",         "url": "https://www.petchart.kr/page/notice.html"},
        {"name": "펫차트 요금",         "url": "https://www.petchart.kr/page/cost.html"},
    ],
}


# ----------------------------------------------------------------------------- 수집
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def itunes_lookup(ids, country):
    """iTunes Lookup API: 현재 버전·배포일·릴리즈 노트·평점·설명 등 (JSON)."""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        url = ("https://itunes.apple.com/lookup?id=%s&country=%s&_=%d"
               % (",".join(str(x) for x in chunk), country, int(time.time())))
        data = json.loads(http_get(url).decode("utf-8", "ignore"))
        for r in data.get("results", []):
            out[str(r.get("trackId"))] = r
    return out


def _extract_json_array(text, start_idx):
    """text[start_idx]가 '['인 위치부터 괄호를 맞춰 JSON 배열 문자열을 잘라낸다."""
    depth, in_str, esc = 0, False, False
    for j in range(start_idx, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start_idx:j + 1]
    return None


def appstore_version_history(app_id, country):
    """스토어 페이지에 묻힌 버전 기록(최근 25개 안팎)을 best-effort로 파싱. 실패하면 None."""
    try:
        page = http_get("https://apps.apple.com/%s/app/id%s" % (country, app_id)).decode("utf-8", "ignore")
    except Exception as e:  # noqa
        return None
    candidates = [page, page.replace('\\"', '"').replace("\\\\", "\\")]
    for text in candidates:
        for m in re.finditer(r'"versionHistory"\s*:\s*\[', text):
            arr = _extract_json_array(text, m.end() - 1)
            if not arr:
                continue
            try:
                items = json.loads(arr)
            except Exception:
                continue
            hist = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                hist.append({
                    "version": it.get("versionDisplay") or it.get("version"),
                    "date": (it.get("releaseDate") or "")[:10],
                    "notes": (it.get("releaseNotes") or "").strip(),
                })
            if hist:
                return hist
    return None


def gp_info(pkg, country):
    try:
        from google_play_scraper import app as gp_app  # type: ignore
    except ImportError:
        return {"error": "google-play-scraper 미설치 (pip install google-play-scraper)"}
    try:
        d = gp_app(pkg, lang="ko", country=country)
    except Exception as e:  # noqa
        return {"error": "%s: %s" % (type(e).__name__, e)}
    updated = d.get("updated")
    if isinstance(updated, (int, float)):
        updated = dt.datetime.fromtimestamp(updated, KST).strftime("%Y-%m-%d")
    return {
        "title": d.get("title"), "version": d.get("version"), "updated": updated,
        "installs": d.get("installs"), "realInstalls": d.get("realInstalls"),
        "score": round(d["score"], 2) if d.get("score") else None,
        "ratings": d.get("ratings"), "reviews": d.get("reviews"),
        "recentChanges": (d.get("recentChanges") or "").strip(),
        "description": d.get("description") or "",
        "released": d.get("released"), "developer": d.get("developer"),
    }


def page_text(url):
    raw = http_get(url).decode("utf-8", "ignore")
    raw = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def sha(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]


def collect(cfg, log):
    country = cfg.get("country", "kr")
    apps = cfg["apps"]
    snap = {"date": dt.datetime.now(KST).strftime("%Y-%m-%d"), "apps": {}, "pages": {}}

    try:
        lookup = itunes_lookup([a["ios"] for a in apps if a.get("ios")], country)
    except Exception as e:  # noqa
        log("iTunes Lookup 실패: %s" % e)
        lookup = {}

    for a in apps:
        entry = {"ios": None, "gp": None}
        r = lookup.get(str(a.get("ios")))
        if r:
            entry["ios"] = {
                "name": r.get("trackName"), "version": r.get("version"),
                "releaseDate": (r.get("currentVersionReleaseDate") or "")[:10],
                "notes": (r.get("releaseNotes") or "").strip(),
                "rating": round(r.get("averageUserRating") or 0, 1),
                "ratingCount": r.get("userRatingCount"),
                "sizeMB": round((r.get("fileSizeBytes") and int(r["fileSizeBytes"]) or 0) / 1048576, 1),
                "minOS": r.get("minimumOsVersion"), "age": r.get("contentAdvisoryRating"),
                "languages": r.get("languageCodesISO2A"), "seller": r.get("sellerName"),
                "firstRelease": (r.get("releaseDate") or "")[:10],
                "description": r.get("description") or "",
                "history": appstore_version_history(a["ios"], country) if a.get("ios") else None,
            }
        elif a.get("ios"):
            entry["ios"] = {"error": "Lookup 결과 없음(스토어에서 내려갔거나 ID 오류)"}
        if a.get("gp"):
            entry["gp"] = gp_info(a["gp"], country)
        snap["apps"][a["name"]] = entry
        log("  앱 수집: %s" % a["name"])

    for p in cfg.get("pages", []):
        try:
            txt = page_text(p["url"])
            snap["pages"][p["name"]] = {"url": p["url"], "hash": sha(txt), "chars": len(txt), "text": txt[:60000]}
        except Exception as e:  # noqa
            snap["pages"][p["name"]] = {"url": p["url"], "error": "%s: %s" % (type(e).__name__, e)}
        log("  페이지 수집: %s" % p["name"])
    return snap


# ----------------------------------------------------------------------------- 비교
def fmt_rating(d):
    if not d:
        return "-"
    return "%s(%s)" % (d.get("rating"), d.get("ratingCount"))


def text_diff(old, new, limit=40):
    diff = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                     lineterm="", n=0))
    body = [ln for ln in diff[2:] if not ln.startswith("@@")]
    extra = "" if len(body) <= limit else "\n… (+%d줄 생략)" % (len(body) - limit)
    return "\n".join(body[:limit]) + extra


def compare(prev, cur, cfg):
    """변동 목록을 (앱명, 등급, 한 줄 요약, 상세) 로 돌려준다. 등급: major / minor"""
    changes, unchanged = [], []
    names = sorted(cur["apps"].keys())  # 가나다순
    for name in names:
        c = cur["apps"][name]
        p = (prev or {}).get("apps", {}).get(name, {}) if prev else {}
        app_changes = []

        ci, pi = c.get("ios") or {}, p.get("ios") or {}
        if ci.get("error") and not pi.get("error"):
            app_changes.append(("major", "iOS 조회 실패: %s" % ci["error"], ""))
        elif ci and not ci.get("error"):
            if pi and ci.get("version") != pi.get("version"):
                app_changes.append(("major", "iOS %s → **%s** (%s)" % (pi.get("version"), ci["version"], ci["releaseDate"]),
                                    "릴리즈 노트:\n" + (ci.get("notes") or "(없음)")))
            if pi and ci.get("name") != pi.get("name"):
                app_changes.append(("major", "iOS 앱명 변경: %s → %s" % (pi.get("name"), ci.get("name")), ""))
            if pi and sha(ci.get("description")) != sha(pi.get("description")):
                app_changes.append(("major", "iOS 소개문 변경", text_diff(pi.get("description"), ci.get("description"))))
            if pi and (ci.get("rating"), ci.get("ratingCount")) != (pi.get("rating"), pi.get("ratingCount")):
                app_changes.append(("minor", "iOS 평점 %s → %s" % (fmt_rating(pi), fmt_rating(ci)), ""))
            for k, label in (("minOS", "최소 OS"), ("age", "연령 등급"), ("sizeMB", "크기(MB)"), ("seller", "제공자")):
                if pi and ci.get(k) != pi.get(k):
                    app_changes.append(("minor", "iOS %s %s → %s" % (label, pi.get(k), ci.get(k)), ""))
            if ci.get("history") and pi.get("history"):
                seen = {(h.get("version"), h.get("date")) for h in pi["history"]}
                new = [h for h in ci["history"] if (h.get("version"), h.get("date")) not in seen]
                if new and not any("iOS %s → " % pi.get("version") in x[1] for x in app_changes):
                    app_changes.append(("major", "iOS 버전 기록 신규 %d건" % len(new),
                                        "\n".join("- %s · %s · %s" % (h["version"], h["date"], h["notes"]) for h in new)))

        cg, pg = c.get("gp") or {}, p.get("gp") or {}
        if cg and not cg.get("error"):
            if pg and not pg.get("error"):
                if cg.get("version") != pg.get("version") or cg.get("updated") != pg.get("updated"):
                    app_changes.append(("major", "GP %s(%s) → **%s**(%s)" % (pg.get("version"), pg.get("updated"), cg.get("version"), cg.get("updated")),
                                        "새로운 기능:\n" + (cg.get("recentChanges") or "(없음)")))
                if cg.get("installs") != pg.get("installs"):
                    app_changes.append(("minor", "GP 설치 %s → %s" % (pg.get("installs"), cg.get("installs")), ""))
                if (cg.get("score"), cg.get("ratings")) != (pg.get("score"), pg.get("ratings")):
                    app_changes.append(("minor", "GP 평점 %s(%s) → %s(%s)" % (pg.get("score"), pg.get("ratings"), cg.get("score"), cg.get("ratings")), ""))
                if sha(cg.get("description")) != sha(pg.get("description")):
                    app_changes.append(("major", "GP 소개문 변경", text_diff(pg.get("description"), cg.get("description"))))
        elif cg and cg.get("error") and pg and not pg.get("error"):
            app_changes.append(("minor", "GP 조회 실패: %s" % cg["error"], ""))

        if app_changes:
            changes.append((name, app_changes))
        elif not p:
            unchanged.append(name + "(신규 추적)")
        else:
            unchanged.append(name)

    page_changes, page_same, page_err = [], [], []
    for name in sorted(cur.get("pages", {}).keys()):
        c = cur["pages"][name]
        p = ((prev or {}).get("pages", {}) or {}).get(name, {})
        if c.get("error"):
            page_err.append("%s — %s" % (name, c["error"]))
        elif p and p.get("hash") and p["hash"] != c.get("hash"):
            page_changes.append((name, c["url"], text_diff(p.get("text"), c.get("text"))))
        else:
            page_same.append(name)
    return changes, unchanged, page_changes, page_same, page_err


def inventory_line(name, c):
    ci, cg = c.get("ios") or {}, c.get("gp") or {}
    ios = ("iOS %s · %s · %s · %sMB" % (ci.get("version"), ci.get("releaseDate"), fmt_rating(ci), ci.get("sizeMB"))
           if ci and not ci.get("error") else "iOS %s" % (ci.get("error") if ci else "-"))
    gp = ("GP %s · %s · %s · %s(%s)" % (cg.get("version"), cg.get("updated"), cg.get("installs"), cg.get("score"), cg.get("ratings"))
          if cg and not cg.get("error") else "GP %s" % (cg.get("error") if cg else "-"))
    return "| %s | %s | %s |" % (name, ios, gp)


def render_report(prev, cur, cfg):
    date, pdate = cur["date"], (prev or {}).get("date")
    lines = ["# 경쟁사 주간 스냅샷 %s%s" % (date, (" (vs %s)" % pdate) if pdate else " — 첫 스냅샷(기준점)"), ""]
    if not prev:
        lines += ["## 인벤토리", "", "| 앱 | iOS | Google Play |", "|---|---|---|"]
        lines += [inventory_line(n, cur["apps"][n]) for n in sorted(cur["apps"])]
        lines += ["", "## 홈페이지", ""]
        for n in sorted(cur.get("pages", {})):
            p = cur["pages"][n]
            lines.append("- %s — %s" % (n, ("오류: " + p["error"]) if p.get("error") else "%d자 수집" % p.get("chars", 0)))
        return "\n".join(lines) + "\n"

    changes, unchanged, page_changes, page_same, page_err = compare(prev, cur, cfg)
    lines += ["## 변동 요약", ""]
    if not changes and not page_changes:
        lines.append("- 변동 없음")
    for name, items in changes:
        for level, summary, _ in items:
            lines.append("- %s**%s** — %s" % ("🔴 " if level == "major" else "", name, summary))
    for name, url, _ in page_changes:
        lines.append("- 🟠 **홈페이지 변경** — %s (%s)" % (name, url))
    lines += ["", "## 변동 없음", "", "- 앱: " + (", ".join(unchanged) or "-"), "- 홈페이지: " + (", ".join(page_same) or "-")]
    if page_err:
        lines += ["- 페이지 조회 실패: " + "; ".join(page_err)]
    if changes or page_changes:
        lines += ["", "## 상세", ""]
        for name, items in changes:
            lines.append("### %s" % name)
            for level, summary, detail in items:
                lines.append("- %s" % summary)
                if detail:
                    lines += ["", "```", detail, "```", ""]
        for name, url, detail in page_changes:
            lines += ["### 홈페이지 · %s" % name, url, "", "```", detail, "```", ""]
    lines += ["", "## 현재 인벤토리", "", "| 앱 | iOS | Google Play |", "|---|---|---|"]
    lines += [inventory_line(n, cur["apps"][n]) for n in sorted(cur["apps"])]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- 저장소 (로컬 / S3)
class Store:
    def __init__(self, base, bucket=None):
        self.base, self.bucket = base, bucket
        if not bucket:
            os.makedirs(os.path.join(base, "snapshots"), exist_ok=True)
            os.makedirs(os.path.join(base, "reports"), exist_ok=True)

    def _s3(self):
        import boto3  # type: ignore
        return boto3.client("s3")

    def list_snapshots(self):
        if self.bucket:
            resp = self._s3().list_objects_v2(Bucket=self.bucket, Prefix="snapshots/")
            keys = [o["Key"] for o in resp.get("Contents", [])]
            return sorted(k.split("/")[-1] for k in keys if k.endswith(".json"))
        return sorted(f for f in os.listdir(os.path.join(self.base, "snapshots")) if f.endswith(".json"))

    def read_snapshot(self, fname):
        if self.bucket:
            body = self._s3().get_object(Bucket=self.bucket, Key="snapshots/" + fname)["Body"].read()
            return json.loads(body.decode("utf-8"))
        with open(os.path.join(self.base, "snapshots", fname), encoding="utf-8") as f:
            return json.load(f)

    def write(self, kind, fname, content):
        data = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1)
        if self.bucket:
            self._s3().put_object(Bucket=self.bucket, Key="%s/%s" % (kind, fname), Body=data.encode("utf-8"),
                                  ContentType="text/plain; charset=utf-8")
        else:
            with open(os.path.join(self.base, kind, fname), "w", encoding="utf-8") as f:
                f.write(data)


def post_slack(webhook, text):
    body = json.dumps({"text": text[:3900]}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


# ----------------------------------------------------------------------------- 실행
def run(cfg, store, init=False, log=print):
    log("수집 시작 %s" % dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"))
    cur = collect(cfg, log)
    snaps = store.list_snapshots()
    prev = None
    if snaps and not init:
        prev_name = snaps[-1] if snaps[-1] != cur["date"] + ".json" else (snaps[-2] if len(snaps) > 1 else None)
        if prev_name:
            prev = store.read_snapshot(prev_name)
    store.write("snapshots", cur["date"] + ".json", cur)
    report = render_report(prev, cur, cfg)
    store.write("reports", cur["date"] + "_diff.md", report)
    return report


def selftest():
    """네트워크 없이 비교·리포트 로직 점검."""
    prev = {"date": "2026-10-18", "apps": {
        "티피매니저": {"ios": {"name": "티피매니저(매장용)", "version": "4.2.1", "releaseDate": "2026-10-01", "notes": "버그 수정",
                              "rating": 2.9, "ratingCount": 54, "sizeMB": 89.7, "minOS": "15.5", "age": "4+", "seller": "fridgecrew",
                              "description": "국내 최초 펫 알림장\n전자동의서", "history": [{"version": "4.2.1", "date": "2026-10-01", "notes": "버그 수정"}]},
                     "gp": {"version": "4.2.1", "updated": "2026-10-01", "installs": "5,000+", "score": 3.1, "ratings": 40, "description": "x", "recentChanges": ""}},
        "똑독": {"ios": {"name": "똑독", "version": "2.1", "releaseDate": "2026-09-14", "notes": "", "rating": 3.8, "ratingCount": 5,
                        "sizeMB": 36.2, "minOS": "15.1", "age": "4+", "seller": "eunhee gwak", "description": "유치원 탐색", "history": None}, "gp": None},
    }, "pages": {"똑독 홈": {"url": "https://home.knockdog.net/", "hash": "a", "text": "탐색\n알림장\n무료"}}}
    cur = json.loads(json.dumps(prev))
    cur["date"] = "2026-10-25"
    t = cur["apps"]["티피매니저"]["ios"]
    t.update({"version": "4.2.2", "releaseDate": "2026-10-23", "notes": "알림장 영상 안정화", "ratingCount": 55,
              "description": "국내 최초 펫 알림장\n전자동의서\n유치원 수업 관리",
              "history": [{"version": "4.2.2", "date": "2026-10-23", "notes": "알림장 영상 안정화"}] + t["history"]})
    cur["apps"]["티피매니저"]["gp"]["installs"] = "10,000+"
    cur["pages"]["똑독 홈"] = {"url": "https://home.knockdog.net/", "hash": "b", "text": "탐색\n알림장\n요금제 9,900원"}
    cur["apps"]["멍텔모아"] = {"ios": {"name": "멍텔모아", "version": "4.4.3", "releaseDate": "2026-10-22", "notes": "fixed bugs",
                                     "rating": 4.7, "ratingCount": 14, "sizeMB": 85.7, "minOS": "15.0", "age": "15+", "seller": "Mungtelmoa Inc.",
                                     "description": "", "history": None}, "gp": None}
    report = render_report(prev, cur, DEFAULT_CONFIG)
    assert "4.2.1 → **4.2.2**" in report and "GP 설치 5,000+ → 10,000+" in report
    assert "iOS 소개문 변경" in report and "+유치원 수업 관리" in report
    assert "홈페이지 변경" in report and "+요금제 9,900원" in report
    assert "똑독" in report.split("## 변동 없음")[1].split("##")[0]
    assert "멍텔모아(신규 추적)" in report
    print(report)
    print("SELFTEST OK")


def lambda_handler(event, context):  # AWS Lambda 진입점 (EventBridge 주간 스케줄)
    cfg = DEFAULT_CONFIG
    cfg_path = os.environ.get("CONFIG_PATH")
    if cfg_path and os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    store = Store("/tmp/cm", bucket=os.environ["S3_BUCKET"])
    report = run(cfg, store, init=event.get("init", False) if isinstance(event, dict) else False)
    if os.environ.get("SLACK_WEBHOOK"):
        post_slack(os.environ["SLACK_WEBHOOK"], report.split("## 변동 없음")[0])
    return {"ok": True, "report_head": report[:500]}


def main():
    ap = argparse.ArgumentParser(description="다이얼독 경쟁사 주간 스냅샷·diff")
    ap.add_argument("--config", help="apps.json 경로 (없으면 내장 기본 설정)")
    ap.add_argument("--dir", default=".", help="snapshots/·reports/ 를 둘 폴더 (기본: 현재 폴더)")
    ap.add_argument("--init", action="store_true", help="첫 실행: 기준 스냅샷 저장 + 인벤토리 출력")
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 diff 로직 점검")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    cfg = DEFAULT_CONFIG
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
    store = Store(args.dir, bucket=os.environ.get("S3_BUCKET"))
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))
    report = run(cfg, store, init=args.init, log=log)
    print(report)
    if os.environ.get("SLACK_WEBHOOK"):
        post_slack(os.environ["SLACK_WEBHOOK"], report.split("## 변동 없음")[0])


if __name__ == "__main__":
    main()
