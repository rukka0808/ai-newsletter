import os
import smtplib
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


# =========================================================
# 1. 🇰🇷 국내 뉴스 - 네이버 뉴스 (당일자, 다중 키워드)
# =========================================================
def fetch_naver_news(queries_dict, per_query=3):
    """
    pd=4 → 1일 이내(당일자 위주)
    sort=1 → 최신순
    """
    print("🇰🇷 [STEP 1] 네이버 뉴스 수집 중...")
    results = {}
    session = requests.Session()
    session.headers.update(HEADERS)

    for key, query in queries_dict.items():
        encoded_query = urllib.parse.quote(query)
        url = (
            f"https://search.naver.com/search.naver?where=news"
            f"&query={encoded_query}&sm=tab_opt&pd=4&sort=1"
        )
        articles = []
        try:
            res = session.get(url, timeout=10)
            res.raise_for_status()
            soup = BeautifulSoup(res.text, 'html.parser')
            news_wraps = soup.select('.news_wrap')[:per_query]

            for wrap in news_wraps:
                title_a = wrap.select_one('.news_tit')
                if not title_a:
                    continue
                title = title_a.get_text(strip=True)
                link = title_a.get('href', '#')
                press_a = wrap.select_one('.info_press')
                press = (press_a.get_text(strip=True).replace("언론사 선정", "")
                         if press_a else "국내경제지")
                dsc_div = wrap.select_one('.news_dsc')
                summary = dsc_div.get_text(strip=True) if dsc_div else ""
                articles.append({
                    "title": title, "link": link,
                    "press": press, "summary": summary
                })
        except Exception as e:
            print(f"  ❌ 네이버 [{query}] 수집 실패: {e}")

        results[key] = articles
        time.sleep(0.5)  # 과다요청 방지
    return results


# =========================================================
# 2. 🌎 해외 뉴스 - CNBC + Investing.com + Reuters
# =========================================================
def fetch_cnbc_news(translator, limit=3):
    print("🌎 [STEP 2-1] CNBC Markets RSS 수집 중...")
    url = "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=10000664"
    articles = []
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        root = ET.fromstring(r.content)
        for item in root.findall('.//item')[:limit]:
            raw_title = (item.findtext('title') or "").strip()
            link = (item.findtext('link') or "#").strip()
            raw_desc = (item.findtext('description') or "").strip()
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(strip=True)
            articles.append(_translate_article(translator, raw_title, clean_desc, link, "CNBC"))
    except Exception as e:
        print(f"  ❌ CNBC 수집 실패: {e}")
    return articles


def fetch_investing_news(translator, limit=3):
    """
    Investing.com - 채권/금리/외환 카테고리 RSS
    """
    print("🌎 [STEP 2-2] Investing.com 채권/외환 RSS 수집 중...")
    feeds = {
        "Bond": "https://www.investing.com/rss/news_25.rss",      # Bonds
        "Forex": "https://www.investing.com/rss/news_1.rss",      # Forex
        "Economy": "https://www.investing.com/rss/news_14.rss",   # Economy
    }
    articles = []
    for cat, url in feeds.items():
        try:
            r = requests.get(url, timeout=10, headers=HEADERS)
            root = ET.fromstring(r.content)
            for item in root.findall('.//item')[:limit]:
                raw_title = (item.findtext('title') or "").strip()
                link = (item.findtext('link') or "#").strip()
                raw_desc = (item.findtext('description') or "").strip()
                clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(strip=True)[:400]
                articles.append(
                    _translate_article(translator, raw_title, clean_desc, link, f"Investing-{cat}")
                )
        except Exception as e:
            print(f"  ❌ Investing [{cat}] 실패: {e}")
        time.sleep(0.3)
    return articles


def fetch_reuters_markets(translator, limit=3):
    """
    Reuters Markets - Google News RSS 우회 (Reuters 본사 RSS 폐쇄로 인한 대체)
    """
    print("🌎 [STEP 2-3] Reuters Markets (Google News 우회) 수집 중...")
    url = ("https://news.google.com/rss/search?"
           "q=site:reuters.com+(markets+OR+bonds+OR+fed+OR+treasury)+when:1d"
           "&hl=en-US&gl=US&ceid=US:en")
    articles = []
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        root = ET.fromstring(r.content)
        for item in root.findall('.//item')[:limit]:
            raw_title = (item.findtext('title') or "").strip()
            link = (item.findtext('link') or "#").strip()
            raw_desc = (item.findtext('description') or "").strip()
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(strip=True)[:400]
            articles.append(
                _translate_article(translator, raw_title, clean_desc, link, "Reuters")
            )
    except Exception as e:
        print(f"  ❌ Reuters 수집 실패: {e}")
    return articles


def _translate_article(translator, title, desc, link, press):
    """공통 번역 헬퍼"""
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
        "title": title_ko, "link": link,
        "press": press, "summary": desc_ko,
        "original_title": title,
    }


# =========================================================
# 3. 📈 시장 지표 - Yahoo Finance Quote API (무료, 키 불필요)
# =========================================================
def fetch_market_indicators():
    """
    주요 지수 / 환율 / 국채금리 / 원자재 실시간 시세
    """
    print("📈 [STEP 3] 글로벌 시장 지표 수집 중...")
    symbols = {
        "S&P 500":      "^GSPC",
        "NASDAQ":       "^IXIC",
        "KOSPI":        "^KS11",
        "KOSDAQ":       "^KQ11",
        "USD/KRW":      "KRW=X",
        "USD/JPY":      "JPY=X",
        "DXY (달러지수)": "DX-Y.NYB",
        "US 10Y 국채":  "^TNX",
        "US 2Y 국채":   "^IRX",
        "WTI 원유":     "CL=F",
        "금(Gold)":     "GC=F",
        "BTC/USD":      "BTC-USD",
    }
    base = "https://query1.finance.yahoo.com/v7/finance/quote?symbols="
    indicators = {}
    try:
        url = base + ",".join(symbols.values())
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json().get("quoteResponse", {}).get("result", [])
        sym_to_name = {v: k for k, v in symbols.items()}
        for q in data:
            name = sym_to_name.get(q.get("symbol"), q.get("symbol"))
            indicators[name] = {
                "price": q.get("regularMarketPrice"),
                "change": q.get("regularMarketChange"),
                "change_pct": q.get("regularMarketChangePercent"),
            }
    except Exception as e:
        print(f"  ❌ 시세 수집 실패: {e}")
    return indicators


# =========================================================
# 4. 🤖 Gemini AI - 핵심 시황 요약 + 카드뉴스 생성
# =========================================================
def generate_ai_briefing(global_news, domestic_news, indicators):
    print("🤖 [STEP 4] Gemini AI 핵심 시황 분석 중...")
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 없음. AI 요약 생략.")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash')

    # 프롬프트용 데이터 정제
    def fmt(arts):
        return "\n".join([f"- [{a['press']}] {a['title']} :: {a['summary'][:200]}" for a in arts])

    indicators_text = "\n".join([
        f"- {k}: {v['price']:.2f} ({v['change_pct']:+.2f}%)"
        for k, v in indicators.items() if v.get('price') is not None
    ])

    domestic_flat = []
    for arr in domestic_news.values():
        domestic_flat.extend(arr)

    prompt = f"""
당신은 25년 경력의 증권사 채권/외환 데스크 시니어 애널리스트입니다.
오늘({TODAY_STR}) 아침, 자산운용 PB들에게 배포할 **투자 상담용 시황 카드뉴스**를 작성합니다.

## 📊 오늘의 시장 지표
{indicators_text}

## 🌎 글로벌 뉴스 (밤사이 해외 시장)
{fmt(global_news)}

## 🇰🇷 국내 뉴스
{fmt(domestic_flat)}

---
아래 JSON 형식으로 **정확히** 출력하세요. (markdown 코드블럭 없이 raw JSON만):

{{
  "headline": "오늘 시장을 한 줄로 요약 (25자 이내, 강한 임팩트)",
  "executive_summary": "3~4문장의 핵심 시황 요약. 채권/금리/환율을 중심으로 어젯밤 해외시장 → 오늘 국내시장 영향까지 자연스럽게 연결.",
  "key_points": [
    {{"icon": "💰", "title": "채권시장", "content": "국내외 금리 동향과 채권시장 핵심 포인트 (2문장)"}},
    {{"icon": "💱", "title": "외환시장", "content": "원/달러, 달러지수, 주요 통화 동향 (2문장)"}},
    {{"icon": "🌐", "title": "글로벌 이슈", "content": "Fed/ECB/지정학 등 매크로 이슈 (2문장)"}},
    {{"icon": "📈", "title": "주식시장", "content": "美/韓 증시 동향 및 섹터 포인트 (2문장)"}}
  ],
  "action_points": [
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 1",
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 2",
    "투자자가 오늘 주목해야 할 구체적 액션 포인트 3"
  ],
  "watch_today": "오늘 한국시간 기준으로 발표될 주요 경제지표/이벤트 (없으면 '특이 일정 없음')"
}}
"""
    try:
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        # markdown 코드블록 제거
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        briefing = json.loads(text)
        print("  ✅ AI 브리핑 생성 완료")
        return briefing
    except Exception as e:
        print(f"  ❌ Gemini 분석 실패: {e}")
        return None


# =========================================================
# 5. 🎨 HTML 카드뉴스 렌더링
# =========================================================
def render_html(briefing, global_news, domestic_news, indicators):
    """카드뉴스 스타일 HTML 메일"""

    # ---- AI 요약 카드 ----
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

    # ---- 시장 지표 표 ----
    ind_rows = ""
    for name, v in indicators.items():
        if v.get('price') is None:
            continue
        color = "#dc2626" if (v.get('change_pct') or 0) > 0 else "#2563eb"
        arrow = "▲" if (v.get('change_pct') or 0) > 0 else "▼"
        ind_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#334155;font-weight:600;">{name}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;text-align:right;color:#0f172a;font-weight:700;">{v['price']:,.2f}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f1f5f9;font-size:13px;text-align:right;color:{color};font-weight:700;">{arrow} {abs(v['change_pct'] or 0):.2f}%</td>
        </tr>
        """

    # ---- 핵심 포인트 카드 ----
    kp_html = ""
    for kp in key_points:
        kp_html += f"""
        <div style="background:#ffffff;border-left:4px solid #2563eb;padding:14px 16px;border-radius:6px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
          <div style="font-size:14px;font-weight:800;color:#0f172a;margin-bottom:5px;">{kp.get('icon','')} {kp.get('title','')}</div>
          <div style="font-size:13.5px;color:#475569;line-height:1.6;">{kp.get('content','')}</div>
        </div>
        """

    # ---- 액션 포인트 ----
    action_html = ""
    for i, a in enumerate(actions, 1):
        action_html += f"""
        <div style="display:flex;align-items:flex-start;padding:8px 0;">
          <div style="background:#fbbf24;color:#78350f;width:22px;height:22px;border-radius:50%;text-align:center;font-weight:800;font-size:12px;line-height:22px;flex-shrink:0;margin-right:10px;">{i}</div>
          <div style="font-size:13.5px;color:#1e293b;line-height:1.6;">{a}</div>
        </div>
        """

    # ---- 뉴스 섹션 빌더 ----
    def build_news_section(title, icon, theme, articles, max_n=5):
        items = ""
        for art in articles[:max_n]:
            items += f"""
            <div style="background:#ffffff;padding:12px 14px;border-radius:6px;margin-bottom:8px;border-left:3px solid {theme};">
              <a href="{art['link']}" style="text-decoration:none;color:#0f172a;font-weight:700;font-size:13.5px;display:block;line-height:1.4;">
                [{art['press']}] {art['title']}
              </a>
              <p style="font-size:12.5px;color:#64748b;margin:6px 0 0 0;line-height:1.5;">
                {art['summary'][:180]}{'...' if len(art['summary'])>180 else ''}
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

    # 국내 뉴스 통합
    domestic_all = []
    for arr in domestic_news.values():
        domestic_all.extend(arr)

    html = f"""
    <div style="font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:680px;margin:0 auto;padding:0;background:#ffffff;color:#0f172a;">

      <!-- HEADER -->
      <div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);padding:30px 25px;text-align:center;color:white;">
        <div style="font-size:11px;letter-spacing:3px;color:#93c5fd;font-weight:700;">DAILY INVESTMENT BRIEFING</div>
        <h1 style="margin:8px 0 5px;font-size:24px;font-weight:900;letter-spacing:-0.5px;">{headline}</h1>
        <div style="font-size:13px;color:#cbd5e1;">📅 {TODAY_STR}</div>
      </div>

      <!-- AI 요약 -->
      <div style="padding:25px;background:#f0f9ff;">
        <div style="font-size:12px;color:#0369a1;font-weight:700;letter-spacing:1px;margin-bottom:8px;">🤖 AI 시황 요약</div>
        <p style="font-size:14.5px;color:#0c4a6e;line-height:1.7;margin:0;font-weight:500;">{summary}</p>
      </div>

      <!-- 시장 지표 -->
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

      <!-- 핵심 포인트 -->
      <div style="padding:5px 25px 20px;">
        <div style="font-size:15px;font-weight:800;color:#0f172a;margin-bottom:12px;">🎯 오늘의 핵심 포인트</div>
        {kp_html}
      </div>

      <!-- 액션 포인트 -->
      {f'''
      <div style="margin:0 25px;padding:18px 20px;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;">
        <div style="font-size:14px;font-weight:800;color:#92400e;margin-bottom:10px;">💡 오늘의 투자 액션 포인트</div>
        {action_html}
      </div>
      ''' if actions else ''}

      <!-- Watch Today -->
      {f'''
      <div style="margin:15px 25px 0;padding:14px 18px;background:#ecfdf5;border-left:4px solid #10b981;border-radius:6px;">
        <div style="font-size:12px;color:#065f46;font-weight:700;margin-bottom:4px;">👀 오늘 체크포인트</div>
        <div style="font-size:13.5px;color:#064e3b;line-height:1.5;">{watch}</div>
      </div>
      ''' if watch else ''}

      <!-- 뉴스 원문 -->
      <div style="padding:0 25px;">
        {build_news_section("국내 경제 / 채권 / 환율 뉴스", "🇰🇷", "#b91c1c", domestic_all, 6)}
        {build_news_section("글로벌 시장 뉴스", "🌎", "#1e40af", global_news, 6)}
      </div>

      <!-- FOOTER -->
      <div style="margin-top:30px;padding:20px;background:#0f172a;color:#94a3b8;text-align:center;font-size:11px;line-height:1.6;">
        본 리포트는 Gemini AI 분석 + DeepL 번역 + 멀티소스 크롤링으로 자동 생성됩니다.<br>
        투자 결정의 최종 책임은 투자자 본인에게 있습니다.<br>
        <span style="color:#475569;">Powered by GitHub Actions × Python × Gemini × DeepL</span>
      </div>
    </div>
    """
    return html


# =========================================================
# 6. 📧 이메일 발송
# =========================================================
def send_email(html_content):
    print("📧 [STEP 6] 이메일 발송 중...")
    sender = os.environ.get('EMAIL_SENDER')
    password = os.environ.get('EMAIL_PASSWORD')
    receiver = os.environ.get('EMAIL_RECEIVER')

    if not all([sender, password, receiver]):
        print("  ❌ 이메일 환경변수 누락")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📊 [Morning Briefing] {TODAY.strftime('%m/%d')} 채권·금리·환율 시황"
    msg['From'] = sender
    # 여러 수신자 지원 (콤마 구분)
    receivers = [r.strip() for r in receiver.split(',')]
    msg['To'] = ", ".join(receivers)
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
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

    # DeepL 번역기
    deepl_key = os.environ.get('DEEPL_API_KEY')
    translator = deepl.Translator(deepl_key) if deepl_key else None
    if not translator:
        print("⚠️ DEEPL_API_KEY 없음 - 해외 뉴스는 영문으로 발송됩니다.")

    # 1) 국내 뉴스 (네이버) - 다각화된 키워드
    domestic_queries = {
        "korea_bond_rate": "국고채 금리 채권시장",
        "korea_fx":        "원달러 환율 외환시장",
        "korea_economy":   "한국은행 기준금리 물가",
        "korea_market":    "코스피 코스닥 증시 마감",
    }
    domestic_news = fetch_naver_news(domestic_queries, per_query=3)

    # 2) 해외 뉴스 (다중 소스 + DeepL 번역)
    global_news = []
    global_news += fetch_cnbc_news(translator, limit=3)
    global_news += fetch_investing_news(translator, limit=2)
    global_news += fetch_reuters_markets(translator, limit=3)

    # 3) 시장 지표
    indicators = fetch_market_indicators()

    # 4) Gemini AI 분석 & 요약
    briefing = generate_ai_briefing(global_news, domestic_news, indicators)

    # 5) HTML 렌더링
    html = render_html(briefing, global_news, domestic_news, indicators)

    # 6) 이메일 발송
    send_email(html)

    print("🎉 파이프라인 완료!")
