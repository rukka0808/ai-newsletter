import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
import deepl

# 1. 🇺🇸 해외 글로벌 매크로 뉴스 (CNBC Markets) + DeepL 번역
def fetch_cnbc_global_news(translator=None):
    """
    CNBC의 Markets(금융/시장) RSS 피드를 크롤링하여, 
    월스트리트의 밤사이 핵심 이슈를 DeepL로 한국어 번역합니다.
    """
    print("🌐 CNBC 글로벌 마켓 뉴스 수집 및 번역 중...")
    # CNBC Markets RSS Feed
    url = "https://search.cnbc.com/rs/search/combinedcms/view.xml?profile=120000000&id=10000664"
    articles = []
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # XML 파싱
        root = ET.fromstring(response.content)
        # 상위 3개의 최신 뉴스 아이템 추출
        items = root.findall('.//item')[:3]
        
        for item in items:
            raw_title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else "#"
            raw_desc = item.find('description').text if item.find('description') is not None else ""
            
            # 설명란에 포함된 불필요한 HTML 태그(이미지 등) 제거
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(strip=True)
            
            # DeepL 번역 파이프라인
            if translator:
                try:
                    # 제목과 본문을 모두 한국어로 번역
                    title_ko = translator.translate_text(raw_title, target_lang="KO").text
                    desc_ko = translator.translate_text(clean_desc, target_lang="KO").text
                except Exception as e:
                    print(f"⚠️ CNBC 번역 오류 (원문 사용): {e}")
                    title_ko, desc_ko = raw_title, clean_desc
            else:
                title_ko, desc_ko = raw_title, clean_desc
                
            articles.append({
                "title": f"[번역] {title_ko}" if translator else raw_title,
                "link": link,
                "press": "CNBC Markets",
                "summary": desc_ko
            })
            
    except Exception as e:
        print(f"❌ CNBC 데이터 수집 실패: {e}")
        
    return articles

# 2. 🇰🇷 국내 거시경제/시황 뉴스 (네이버 뉴스)
def fetch_naver_news(queries_dict):
    """
    국내 주요 경제/채권/환율 이슈를 당일자(pd=4) 기준으로 수집합니다.
    """
    print("🇰🇷 네이버 국내 시황 뉴스 수집 중...")
    results = {}
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    for key, query in queries_dict.items():
        encoded_query = urllib.parse.quote(query)
        url = f"https://search.naver.com/search.naver?where=news&query={encoded_query}&sm=tab_opt&pd=4"
        
        articles = []
        try:
            response = session.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            news_wraps = soup.select('.news_wrap')[:3]
            
            for wrap in news_wraps:
                try:
                    title_a = wrap.select_one('.news_tit')
                    if not title_a: continue
                    
                    title = title_a.get_text(strip=True)
                    link = title_a.get('href', '#')
                    
                    press_a = wrap.select_one('.info_press')
                    press = press_a.get_text(strip=True).replace("언론사 선정", "") if press_a else "국내경제지"
                    
                    dsc_div = wrap.select_one('.news_dsc')
                    summary = dsc_div.get_text(strip=True) if dsc_div else "요약 내용이 제공되지 않습니다."
                    
                    articles.append({
                        "title": title,
                        "link": link,
                        "press": press,
                        "summary": summary
                    })
                except Exception:
                    continue
        except Exception as e:
            print(f"❌ 네이버 뉴스 [{query}] 수집 실패: {e}")
            
        results[key] = articles
    return results

# 3. 프리미엄 HTML 리포트 렌더링
def create_consulting_html(global_news, domestic_data):
    today_str = datetime.today().strftime('%Y년 %m월 %d일')
    
    html = f"""
    <div style="font-family: 'Malgun Gothic', sans-serif; max-width: 650px; margin: 0 auto; padding: 25px; border: 1px solid #e0e0e0; border-radius: 8px; background-color: #f8fafc; color: #333333; line-height: 1.6;">
        <div style="text-align: center; padding-bottom: 20px; border-bottom: 3px solid #0f172a;">
            <span style="font-size: 13px; font-weight: bold; color: #2563eb; letter-spacing: 2px;">GLOBAL & DOMESTIC MACRO BRIEFING</span>
            <h1 style="margin: 10px 0 10px 0; font-size: 24px; color: #0f172a; font-weight: 900;">전략적 투자 상담용 일일 시황 리포트</h1>
            <p style="margin: 0; font-size: 14px; color: #64748b;">📅 {today_str} 발송</p>
        </div>
    """
    
    # 통합 섹션 구성 (해외 1개 + 국내 2개)
    sections = [
        {"icon": "🌎", "title": "오버나이트 글로벌 마켓 동향 (CNBC)", "data": global_news, "theme": "#1e3a8a", "bg": "#eff6ff"},
        {"icon": "🇰🇷", "title": "국내 경제 및 증시 주요 이슈", "data": domestic_data.get("korea_economy", []), "theme": "#b91c1c", "bg": "#fef2f2"},
        {"icon": "📊", "title": "채권 금리 및 외환시장 동향", "data": domestic_data.get("rates_fx", []), "theme": "#0f766e", "bg": "#f0fdf4"}
    ]
    
    for sec in sections:
        html += f"""
        <div style="margin-top: 35px;">
            <div style="background-color: {sec['theme']}; color: white; padding: 10px 15px; border-radius: 6px 6px 0 0; font-weight: bold; font-size: 16px;">
                {sec['icon']} {sec['title']}
            </div>
            <div style="background-color: {sec['bg']}; padding: 15px; border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 6px 6px;">
        """
        
        if not sec['data']:
            html += "<p style='font-size: 13px; color: #64748b; margin: 0;'>최신 업데이트된 기사가 없습니다.</p>"
        else:
            for idx, art in enumerate(sec['data']):
                html += f"""
                <div style="background-color: #ffffff; padding: 15px; border-radius: 8px; margin-bottom: { '15px' if idx < len(sec['data'])-1 else '0px' }; box-shadow: 0 1px 2px rgba(0,0,0,0.05); border-left: 4px solid {sec['theme']};">
                    <a href="{art['link']}" style="text-decoration: none; color: #0f172a; font-weight: 800; font-size: 15px; display: block; line-height: 1.4;">
                        [{art['press']}] {art['title']}
                    </a>
                    <p style="font-size: 13.5px; color: #475569; margin: 8px 0 0 0; text-align: justify; word-break: keep-all;">
                        {art['summary'][:250]}... <span style="color:{sec['theme']}; font-weight:bold; font-size: 12px;">(더보기)</span>
                    </p>
                </div>
                """
        html += "</div></div>"
        
    html += """
        <div style="margin-top: 40px; padding-top: 15px; border-top: 1px solid #cbd5e1; text-align: center;">
            <p style="font-size: 11px; color: #94a3b8; margin: 0;">
                본 정보는 DeepL AI 번역 및 자동 크롤링을 통해 수집된 데이터로, 실제 투자 판단의 최종 책임은 본인에게 있습니다.<br>
                Powered by GitHub Actions & Python Pipeline
            </p>
        </div>
    </div>
    """
    return html

# 4. 이메일 발송
def send_email(html_content):
    sender = os.environ.get('EMAIL_SENDER')
    password = os.environ.get('EMAIL_PASSWORD') 
    receiver = os.environ.get('EMAIL_RECEIVER')

    msg = MIMEMultipart()
    today_str = datetime.today().strftime('%Y/%m/%d')
    msg['Subject'] = f"🔔 [Morning Briefing] {today_str} 글로벌 & 국내 시황 리포트"
    msg['From'] = sender
    msg['To'] = receiver
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print("✅ 브리핑 리포트 발송 성공!")
    except Exception as e:
        print(f"❌ 메일 발송 실패: {e}")

if __name__ == "__main__":
    print("🚀 투자 전략 브리핑 파이프라인 가동 시작...")
    
    # 1. DeepL 번역기 로드
    deepl_key = os.environ.get('DEEPL_API_KEY')
    translator = deepl.Translator(deepl_key) if deepl_key else None
    
    if not translator:
        print("⚠️ DEEPL_API_KEY가 없습니다. 해외 뉴스는 영문으로 발송됩니다.")

    # 2. 해외 뉴스 수집 및 번역 (CNBC)
    global_news = fetch_cnbc_global_news(translator)
    
    # 3. 국내 시황 데이터 수집 (네이버)
    domestic_queries = {
        "korea_economy": "증시 동향 경제 이슈",
        "rates_fx": "채권시장 금리 환율 변동"
    }
    domestic_data = fetch_naver_news(domestic_queries)
    
    # 4. 리포트 생성 및 발송
    email_content = create_consulting_html(global_news, domestic_data)
    send_email(email_content)
