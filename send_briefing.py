import os
import re
import smtplib
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup
import deepl
import google.generativeai as genai


# =========================================================
# 0. 공통 설정
# =========================================================
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST)
TODAY_STR = TODAY.strftime('%Y년 %m월 %d일 (%a)')
NOW_UTC = datetime.now(timezone.utc)

# 🆕 "오늘 기사" 기준: 최근 24시간 이내만 허용
FRESHNESS_HOURS = 24

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": "https://finance.naver.com/",
    "Accept": "application/json, text/plain, */*",
}


# =========================================================
# 🆕 공통 유틸: pubDate 파싱 + 신선도 필터 + 중복 제거
# =========================================================
def _parse_pubdate(date_str):
    """RFC 822 형식의 pubDate → datetime (UTC). 실패 시 None"""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_fresh(pub_dt, hours=FRESHNESS_HOURS):
    """발행시각이 최근 N시간 이내인가?"""
    if pub_dt is None:
        return False  # pubDate 없으면 안전하게 제외
    return (NOW_UTC - pub_dt) <= timedelta(hours=hours)


def _normalize_title(title):
    """중복 검사용: 특수문자/공백 제거 + 소문자"""
    if not title:
        return ""
    t = re.sub(r"[\s\W_]+", "", title.lower())
    return t[:60]  # 앞 60자만으로 동일성 판단


def _dedupe_and_filter(articles, hours=FRESHNESS_HOURS):
    """① 최근 N시간 이내 + ② 제목 기준 중복 제거"""
    seen = set()
    result = []
    for a in articles:
        if not _is_fresh(a.get("_pub_dt"), hours):
            continue
        norm = _normalize_title(a.get("original_title") or a.get("title"))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        # 내부 키 삭제
        a.pop("_pub_dt", None)
        result.append(a)
    return result


# =========================================================
# 1. 🇰🇷 국내 뉴스 - Google News RSS (날짜순 + 신선도 필터)
# =========================================================
def fetch_domestic_news(queries_dict, per_query=4):
    print(f"🇰🇷 [STEP 1] 국내 뉴스 수집 중 (최근 {FRESHNESS_HOURS}시간 필터)...")
    results = {}
    site_filter = (
        "(site:hankyung.com OR site:mk.co.kr OR site:yna.co.kr "
        "OR site:edaily.co.kr OR site:newsis.com OR site:fnnews.com "
        "OR site:sedaily.com OR site:chosun.com OR site:joongang.co.kr "
        "OR site:mt.co.kr)"
    )

    for key, query in queries_dict.items():
        # ⭐ when:1d 유지 + 정렬은 RSS 기본(시간순에 가까움) + 후처리로 한 번 더 필터
        full_q = f"{query} {site_filter} when:1d"
        encoded = urllib.parse.quote(full_q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"

        raw_articles = []
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)

            # ⭐ 후보를 넉넉히 가져와서 (per_query * 4) 필터링 후 상위 N개 선택
            for item in root.findall(".//item")[:per_query * 4]:
                title_raw = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "#").strip()
                desc_raw = (item.findtext("description") or "").strip()
                pub_str = (item.findtext("pubDate") or "").strip()
                source_el = item.find("source")
                press = source_el.text.strip() if source_el is not None and source_el.text else "국내경제지"

                if " - " in title_raw:
                    title = title_raw.rsplit(" - ", 1)[0]
                else:
                    title = title_raw

                summary = BeautifulSoup(desc_raw, "html.parser").get_text(strip=True)[:250]

                raw_articles.append({
                    "title": title,
                    "original_title": title,
                    "link": link,
                    "press": press,
                    "summary": summary or title,
                    "_pub_dt": _parse_pubdate(pub_str),
                })

            # ⭐ 신선도 + 중복 제거
            filtered = _dedupe_and_filter(raw_articles)
            # 최신순 정렬 (이미 정렬되어 있지만 안전하게)
            articles = filtered[:per_query]
            results[key] = articles
            print(f"  ✅ [{key}] 후보 {len(raw_articles)}건 → 필터 후 {len(articles)}건")

        except Exception as e:
            print(f"  ❌ [{key}] 수집 실패: {e}")
            results[key] = []

        time.sleep(0.4)

    # ⭐ 키 전체에 걸쳐 중복 제거 (다른 검색어에서 같은 기사가 잡힐 수 있음)
    global_seen = set()
    for key in results:
        deduped = []
        for a in results[key]:
            norm = _normalize_title(a["title"])
            if norm in global_seen:
                continue
            global_seen.add(norm)
            deduped.append(a)
        results[key] = deduped
    return results


# =========================================================
# 2. 🌎 해외 뉴스 (DeepL + 신선도 필터)
# =========================================================
def _translate_article(translator, title, desc, link, press, pub_dt):
    if translator and title:
        try:
            title_ko = translator.translate_text(title, target_lang="KO").text
            desc_ko = translator.translate_text(desc, target_lang="KO").text if desc else ""
        except Exception as e:
            print(f"  ⚠️ DeepL 번역 오류: {e}")
            title_ko, desc_ko = title, desc
    else:
        title_ko, desc_ko = title, desc
    return {
        "title": title_ko, "link": link, "press": press,
        "summary": desc_ko, "original_title": title,
        "_pub_dt": pub_dt,
    }


def fetch_cnbc_news(translator, limit=3):
    print(f"🌎 [STEP 2-1] CNBC Markets RSS (최근 {FRESHNESS_HOURS}h 필터)...")
    url = "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=10000664"
    raw_articles = []
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        root = ET.fromstring(r.content)
        # ⭐ 후보 넉넉히
        for item in root.findall(".//item")[:limit * 4]:
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "#").strip()
            raw_desc = (item.findtext("description") or "").strip()
            pub_str = (item.findtext("pubDate") or "").strip()
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(strip=True)
            pub_dt = _parse_pubdate(pub_str)
            # ⭐ 번역 비용 절약: 신선한 것만 번역
            if not _is_fresh(pub_dt):
                continue
            raw_articles.append(
                _translate_article(translator, raw_title, clean_desc, link, "CNBC", pub_dt)
            )
        articles = _dedupe_and_filter(raw_articles)[:limit]
        print(f"  ✅ CNBC: 필터 후 {len(articles)}건")
        return articles
    except Exception as e:
        print(f"  ❌ CNBC 수집 실패: {e}")
        return []


def fetch_investing_news(translator, limit=2):
    print(f"🌎 [STEP 2-2] Investing.com 채권/외환 RSS (최근 {FRESHNESS_HOURS}h 필터)...")
    feeds = {
        "Bond": "https://www.investing.com/rss/news_25.rss",
        "Forex": "https://www.investing.com/rss/news_1.rss",
        "Economy": "https://www.investing.com/rss/news_14.rss",
    }
    all_articles = []
    for cat, url in feeds.items():
        raw_articles = []
        try:
            r = requests.get(url, timeout=10, headers=HEADERS)
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:limit * 5]:
                raw_title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "#").strip()
                raw_desc = (item.findtext("description") or "").strip()
                pub_str = (item.findtext("pubDate") or "").strip()
                clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(strip=True)[:400]
                pub_dt = _parse_pubdate(pub_str)
                if not _is_fresh(pub_dt):
                    continue
                raw_articles.append(
                    _translate_article(translator, raw_title, clean_desc, link, f"Investing-{cat}", pub_dt)
                )
            cat_articles = _dedupe_and_filter(raw_articles)[:limit]
            print(f"  ✅ Investing-{cat}: 필터 후 {len(cat_articles)}건")
            all_articles.extend(cat_articles)
        except Exception as e:
            print(f"  ❌ Investing [{cat}] 실패: {e}")
        time.sleep(0.3)
    return all_articles


def fetch_reuters_markets(translator, limit=3):
    print(f"🌎 [STEP 2-3] Reuters Markets (최근 {FRESHNESS_HOURS}h 필터)...")
    url = ("https://news.google.com/rss/search?"
           "q=site:reuters.com+(markets+OR+bonds+OR+fed+OR+treasury)+when:1d"
           "&hl=en-US&gl=US&ceid=US:en")
    raw_articles = []
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:limit * 4]:
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "#").strip()
            raw_desc = (item.findtext("description") or "").strip()
            pub_str = (item.findtext("pubDate") or "").strip()
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(strip=True)[:400]
            pub_dt = _parse_pubdate(pub_str)
            if not _is_fresh(pub_dt):
                continue
            raw_articles.append(
                _translate_article(translator, raw_title, clean_desc, link, "Reuters", pub_dt)
            )
        articles = _dedupe_and_filter(raw_articles)[:limit]
        print(f"  ✅ Reuters: 필터 후 {len(articles)}건")
        return articles
    except Exception as e:
        print(f"  ❌ Reuters 수집 실패: {e}")
        return []


# =========================================================
# 3. 📈 시장 지표 - 네이버 증권 통합 (변경 없음)
# =========================================================
def fetch_market_indicators():
    print("📈 [STEP 3] 네이버 증권 시장 지표 수집 중...")
    indicators = {}

    # ① 국내 지수
    try:
        url = ("https://polling.finance.naver.com/api/realtime"
               "?query=SERVICE_INDEX:KOSPI,KOSDAQ,KPI200")
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        name_map = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "KPI200": "KOSPI200"}
        for row in data["result"]["areas"][0]["datas"]:
            code = row["cd"]
            price = row["nv"] / 100
            change = row["cv"] / 100
            change_pct = row["cr"]
            sign = -1 if row["rf"] in ("4", "5") else 1
            indicators[name_map.get(code, code)] = {
                "price": price,
                "change": change * sign,
                "change_pct": change_pct * sign,
            }
        print(f"  ✅ 국내 지수 수집 완료")
    except Exception as e:
        print(f"  ❌ 국내 지수 수집 실패: {e}")

    # ② 환율
    fx_targets = {
        "원/달러 (USD/KRW)":  "FX_USDKRW",
        "원/엔 100 (JPY/KRW)": "FX_JPYKRW",
        "원/유로 (EUR/KRW)":  "FX_EURKRW",
        "원/위안 (CNY/KRW)":  "FX_CNYKRW",
    }
    for name, code in fx_targets.items():
        try:
            url = f"https://api.stock.naver.com/marketindex/exchange/{code}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            d = r.json().get("exchangeInfo", {})
            sign = -1 if d.get("fluctuationsType", {}).get("code") in ("4", "5") else 1
            indicators[name] = {
                "price": float(d["closePrice"].replace(",", "")),
                "change": float(d["fluctuations"].replace(",", "")) * sign,
                "change_pct": float(d["fluctuationsRatio"]) * sign,
            }
        except Exception as e:
            print(f"  ⚠️ 환율 [{name}] 실패: {e}")
        time.sleep(0.2)
    print(f"  ✅ 환율 수집 완료")

    # ③ 금
    try:
        url = "https://api.stock.naver.com/marketindex/metals/M04020000"
        r = requests.get(url, headers=HEADERS, timeout=10)
        d = r.json()
        sign = -1 if d.get("fluctuationsType", {}).get("code") in ("4", "5") else 1
        indicators["국내 금 (원/g)"] = {
            "price": float(d["closePrice"].replace(",", "")),
            "change": float(d["fluctuations"].replace(",", "")) * sign,
            "change_pct": float(d["fluctuationsRatio"]) * sign,
        }
        print(f"  ✅ 금 시세 수집")
    except Exception as e:
        print(f"  ⚠️ 금 시세 실패: {e}")

    # ④ 해외 주요 지수
    world_targets = {
        "다우존스 (DJI)":  "DJI@DJI",
        "S&P 500":        "SPI@SPX",
        "NASDAQ":         "NAS@IXIC",
        "닛케이 225":      "NII@NI225",
        "상해종합":        "SHS@000001",
        "홍콩H (HSI)":    "HSI@HSI",
    }
    for name, sym in world_targets.items():
        try:
            url = f"https://finance.naver.com/world/sise.naver?symbol={sym}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.encoding = "euc-kr"
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
            m = re.search(
                r"([0-9,]+\.\d{1,2})\s*전일대비\s*([0-9,]+\.\d{1,2})"
                r"\s*\(\s*([-+]?\d+\.\d+)\s*%\s*\)",
                text,
            )
            if m:
                price = float(m.group(1).replace(",", ""))
                change = float(m.group(2).replace(",", ""))
                pct = float(m.group(3))
                if pct < 0 and change > 0:
                    change = -change
                indicators[name] = {
                    "price": price, "change": change, "change_pct": pct,
                }
            else:
                print(f"  ⚠️ 해외 지수 [{name}] 정규식 매치 실패")
        except Exception as e:
            print(f"  ⚠️ 해외 지수 [{name}] 실패: {e}")
        time.sleep(0.3)
    print(f"  ✅ 해외 지수 수집 완료")

    print(f"📊 총 {len(indicators)}개 지표 수집됨")
    return indicators


# =========================================================
# 4. 🤖 Gemini AI 시황 분석
# =========================================================
def generate_ai_briefing(global_news, domestic_news, indicators):
    print("🤖 [STEP 4] Gemini AI 핵심 시황 분석 중...")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 없음. AI 요약 생략.")
        return None

    genai.configure(api_key=api_key)
    model_candidates = ["gemini-2.5-flash", "gemini-2.0-flash",
                        "gemini-1.5-flash", "gemini-1.5-flash-latest"]

    def fmt(arts):
        if not arts:
            return "(수집된 기사 없음)"
        return "\n".join([
            f"- [{a.get('press','')}] {a.get('title','')} :: {(a.get('summary') or '')[:200]}"
            for a in arts
        ])

    indicators_text = "\n".join([
        f"- {k}: {v['price']:.2f} ({(v.get('change_pct') or 0):+.2f}%)"
        for k, v in indicators.items() if v.get("price") is not None
    ]) or "(시세 수집 실패)"

    domestic_flat = []
    for arr in domestic_news.values():
        domestic_flat.extend(arr)

    prompt = f"""당신은 25년 경력의 증권사 채권/외환 데스크 시니어 애널리스트입니다.
오늘({TODAY_STR}) 아침, 자산운용 PB들에게 배포할 투자 상담용 시황 카드뉴스를 작성합니다.

## 📊 오늘의 시장 지표
{indicators_text}

## 🌎 글로벌 뉴스 (밤사이 해외 시장, 최근 24시간 이내)
{fmt(global_news)}

## 🇰🇷 국내 뉴스 (최근 24시간 이내)
{fmt(domestic_flat)}

---
**반드시 위 뉴스와 지표에 명시된 사실에 근거해서만 작성**하세요. 일반론·과거 사례 금지.
반드시 아래 JSON 스키마로만, 다른 텍스트 없이 출력하세요. markdown 코드블록 금지.

{{
  "headline": "오늘 시장을 한 줄로 요약 (25자 이내, 강한 임팩트)",
  "executive_summary": "3~4문장의 핵심 시황 요약. 채권/금리/환율을 중심으로 어젯밤 해외시장 → 오늘 국내시장 영향까지 자연스럽게 연결.",
  "key_points": [
    {{"icon": "💰", "title": "채권시장", "content": "국내외 금리 동향과 채권시장 핵심 포인트 (2문장)"}},
    {{"icon": "💱", "title": "외환시장", "content": "원/달러, 달러지수, 주요 통화 동향 (2문장)"}},
    {{"icon": "🌐", "title": "글로벌 이슈", "content": "Fed/ECB/지정학 등 매크로 이슈 (2문장)"}},
    {{"icon": "📈", "title": "주식시장", "content": "미국·한국 증시 동향 및 섹터 포인트 (2문장)"}}
  ],
  "action_points": [
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 1",
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 2",
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 3"
  ],
  "watch_today": "오늘 한국시간 기준 발표 예정인 주요 경제지표/이벤트 (없으면 '특이 일정 없음')"
}}
"""

    generation_config = {"temperature": 0.4, "response_mime_type": "application/json"}
    last_err = None

    for model_name in model_candidates:
        try:
            print(f"  → 모델 시도: {model_name}")
            model = genai.GenerativeModel(model_name, generation_config=generation_config)
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()

            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:]
                text = text.strip()

            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]

            briefing = json.loads(text)
            print(f"  ✅ AI 브리핑 생성 완료 (모델: {model_name})")
            return briefing
        except Exception as e:
            last_err = e
            print(f"  ⚠️ {model_name} 실패: {e}")
            continue

    print(f"  ❌ 모든 Gemini 모델 실패. 마지막 오류: {last_err}")
    return None


# =========================================================
# 5. 🎨 HTML 카드뉴스 렌더링
# =========================================================
def render_html(briefing, global_news, domestic_news, indicators):
    if briefing:
        headline = briefing.get("headline", "오늘의 시장 브리핑")
        summary = briefing.get("executive_summary", "")
        key_points = briefing.get("key_points", [])
        actions = briefing.get("action_points", [])
        watch = briefing.get("watch_today", "")
    else:
        headline = "오늘의 시장 브리핑"
        summary = "AI 요약 생성에 실패했습니다. 아래 원문 기사를 참고하세요."
        key_points, actions, watch = [], [], ""

    ind_rows = ""
    for name, v in indicators.items():
        if v.get("price") is None:
            continue
        pct = v.get("change_pct") or 0
        color = "#dc2626" if pct > 0 else ("#2563eb" if pct < 0 else "#64748b")
        arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "▬")
        ind_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#334155;font-weight:600;">{name}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;text-align:right;color:#0f172a;font-weight:700;">{v['price']:,.2f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;text-align:right;color:{color};font-weight:700;">{arrow} {abs(pct):.2f}%</td>
        </tr>
        """

    kp_html = ""
    for kp in key_points:
        kp_html += f"""
        <div style="background:#ffffff;border-left:4px solid #2563eb;padding:14px 16px;border-radius:6px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
          <div style="font-size:14px;font-weight:800;color:#0f172a;margin-bottom:5px;">{kp.get('icon','')} {kp.get('title','')}</div>
          <div style="font-size:13.5px;color:#475569;line-height:1.6;">{kp.get('content','')}</div>
        </div>
        """

    action_html = ""
    for i, a in enumerate(actions, 1):
        action_html += f"""
        <div style="display:flex;align-items:flex-start;padding:8px 0;">
          <div style="background:#fbbf24;color:#78350f;width:22px;height:22px;border-radius:50%;text-align:center;font-weight:800;font-size:12px;line-height:22px;flex-shrink:0;margin-right:10px;">{i}</div>
          <div style="font-size:13.5px;color:#1e293b;line-height:1.6;">{a}</div>
        </div>
        """

    def build_news_section(title, icon, theme, articles, max_n=6):
        items = ""
        for art in articles[:max_n]:
            items += f"""
            <div style="background:#ffffff;padding:12px 14px;border-radius:6px;margin-bottom:8px;border-left:3px solid {theme};">
              <a href="{art['link']}" style="text-decoration:none;color:#0f172a;font-weight:700;font-size:13.5px;display:block;line-height:1.4;">
                [{art['press']}] {art['title']}
              </a>
              <p style="font-size:12.5px;color:#64748b;margin:6px 0 0 0;line-height:1.5;">
                {art['summary'][:180]}{'...' if len(art['summary']) > 180 else ''}
              </p>
            </div>
            """
        return f"""
        <div style="margin-top:25px;">
          <div style="background:{theme};color:white;padding:10px 15px;border-radius:6px 6px 0 0;font-weight:700;font-size:14px;">
            {icon} {title}
          </div>
          <div style="background:#f8fafc;padding:12px;border-radius:0 0 6px 6px;">
            {items if items else '<p style="font-size:12px;color:#94a3b8;margin:0;">수집된 기사가 없습니다.</p>'}
          </div>
        </div>
        """

    domestic_all = []
    for arr in domestic_news.values():
        domestic_all.extend(arr)

    html = f"""
    <div style="font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:680px;margin:0 auto;padding:0;background:#ffffff;color:#0f172a;">
      <div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);padding:30px 25px;text-align:center;color:white;">
        <div style="font-size:11px;letter-spacing:3px;color:#93c5fd;font-weight:700;">DAILY INVESTMENT BRIEFING</div>
        <h1 style="margin:8px 0 5px;font-size:24px;font-weight:900;letter-spacing:-0.5px;">{headline}</h1>
        <div style="font-size:13px;color:#cbd5e1;">📅 {TODAY_STR}</div>
      </div>

      <div style="padding:25px;background:#f0f9ff;">
        <div style="font-size:12px;color:#0369a1;font-weight:700;letter-spacing:1px;margin-bottom:8px;">🤖 AI 시황 요약</div>
        <p style="font-size:14.5px;color:#0c4a6e;line-height:1.7;margin:0;font-weight:500;">{summary}</p>
      </div>

      <div style="padding:20px 25px;">
        <div style="font-size:15px;font-weight:800;color:#0f172a;margin-bottom:12px;">📊 글로벌 시장 지표</div>
        <table style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:6px;overflow:hidden;">
          <thead>
            <tr style="background:#0f172a;color:white;">
              <th style="padding:9px 12px;text-align:left;font-size:12px;">지표</th>
              <th style="padding:9px 12px;text-align:right;font-size:12px;">현재가</th>
              <th style="padding:9px 12px;text-align:right;font-size:12px;">등락률</th>
            </tr>
          </thead>
          <tbody>{ind_rows}</tbody>
        </table>
      </div>

      <div style="padding:5px 25px 20px;">
        <div style="font-size:15px;font-weight:800;color:#0f172a;margin-bottom:12px;">🎯 오늘의 핵심 포인트</div>
        {kp_html}
      </div>

      {f'''
      <div style="margin:0 25px;padding:18px 20px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;">
        <div style="font-size:14px;font-weight:800;color:#92400e;margin-bottom:10px;">💡 오늘의 투자 액션 포인트</div>
        {action_html}
      </div>
      ''' if actions else ''}

      {f'''
      <div style="margin:15px 25px 0;padding:14px 18px;background:#ecfdf5;border-left:4px solid #10b981;border-radius:6px;">
        <div style="font-size:12px;color:#065f46;font-weight:700;margin-bottom:4px;">👀 오늘 체크포인트</div>
        <div style="font-size:13.5px;color:#064e3b;line-height:1.5;">{watch}</div>
      </div>
      ''' if watch else ''}

      <div style="padding:0 25px;">
        {build_news_section("국내 경제 / 채권 / 환율 뉴스", "🇰🇷", "#b91c1c", domestic_all, 8)}
        {build_news_section("글로벌 시장 뉴스", "🌎", "#1e40af", global_news, 8)}
      </div>

      <div style="margin-top:30px;padding:20px;background:#0f172a;color:#94a3b8;text-align:center;font-size:11px;line-height:1.6;">
        본 리포트는 Gemini AI 분석 + DeepL 번역 + 멀티소스 크롤링으로 자동 생성됩니다.<br>
        모든 뉴스는 최근 {FRESHNESS_HOURS}시간 이내 발행분이며, 투자 결정의 최종 책임은 투자자 본인에게 있습니다.<br>
        <span style="color:#475569;">Powered by GitHub Actions × Python × Gemini × DeepL × Naver Finance</span>
      </div>
    </div>
    """
    return html


# =========================================================
# 6. 📧 이메일 발송
# =========================================================
def send_email(html_content):
    print("📧 [STEP 6] 이메일 발송 중...")
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = os.environ.get("EMAIL_RECEIVER")

    if not all([sender, password, receiver]):
        print("  ❌ 이메일 환경변수 누락")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 [Morning Briefing] {TODAY.strftime('%m/%d')} 채권·금리·환율 시황"
    msg["From"] = sender
    receivers = [r.strip() for r in receiver.split(",") if r.strip()]
    msg["To"] = ", ".join(receivers)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, receivers, msg.as_string())
        print(f"  ✅ 발송 성공 → {receivers}")
    except Exception as e:
        print(f"  ❌ 메일 발송 실패: {e}")


# =========================================================
# 7. 🚀 메인 파이프라인
# =========================================================
if __name__ == "__main__":
    print(f"🚀 일일 투자 브리핑 파이프라인 시작 ({TODAY_STR})")
    print(f"⏰ 현재 UTC: {NOW_UTC.strftime('%Y-%m-%d %H:%M:%S')} / 신선도 기준: {FRESHNESS_HOURS}h\n")

    deepl_key = os.environ.get("DEEPL_API_KEY")
    translator = deepl.Translator(deepl_key) if deepl_key else None
    if not translator:
        print("⚠️ DEEPL_API_KEY 없음 - 해외 뉴스는 영문으로 발송됩니다.")

    domestic_queries = {
        "korea_bond_rate": "국고채 금리 채권시장",
        "korea_fx":        "원달러 환율 외환시장",
        "korea_economy":   "한국은행 기준금리 물가",
        "korea_market":    "코스피 코스닥 증시 마감",
    }
    domestic_news = fetch_domestic_news(domestic_queries, per_query=4)

    global_news = []
    global_news += fetch_cnbc_news(translator, limit=3)
    global_news += fetch_investing_news(translator, limit=2)
    global_news += fetch_reuters_markets(translator, limit=3)

    indicators = fetch_market_indicators()
    briefing = generate_ai_briefing(global_news, domestic_news, indicators)

    print("\n========== 수집 결과 요약 ==========")
    print(f"국내 뉴스: {sum(len(v) for v in domestic_news.values())}건")
    for k, v in domestic_news.items():
        print(f"  - {k}: {len(v)}건")
    print(f"글로벌 뉴스: {len(global_news)}건")
    print(f"시장 지표: {len(indicators)}개")
    print(f"AI 브리핑: {'✅ 생성됨' if briefing else '❌ 실패'}")
    print("====================================\n")

    html = render_html(briefing, global_news, domestic_news, indicators)
    send_email(html)
    print("🎉 파이프라인 완료!")
