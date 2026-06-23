import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import arxiv
import deepl

# 1. 금융 시황 및 거시경제 데이터 수집 함수
def fetch_market_data():
    # 추후 크롤링 코드를 추가하여 고도화할 수 있는 영역입니다.
    # 지금은 워크플로우 작동 테스트를 위해 정상 데이터를 가정하고 구성합니다.
    return {
        "exchange_rate": "1,352.50원",
        "us_10y_bond": "4.23%",
        "headline": "글로벌 채권시장 금리 변동성 확대 및 주요국 통화 긴축 우려 지속"
    }

# 2. arXiv 최신 AI/LLM 논문 수집 및 번역 함수
def fetch_arxiv_papers():
    client = arxiv.Client()
    # 인공지능(cs.AI) 분야에서 최신 논문 3개 검색
    search = arxiv.Search(
        query="cat:cs.AI",
        max_results=3,
        sort_by=arxiv.SortCriterion.SubmittedDate
    )
    
    # GitHub Secrets에서 DeepL API 키 가져오기
    deepl_key = os.environ.get('DEEPL_API_KEY')
    translator = deepl.Translator(deepl_key) if deepl_key else None
    
    papers = []
    for result in client.results(search):
        # DeepL API 키가 등록되어 있으면 한국어로 번역, 없으면 영어 원문 유지
        if translator:
            try:
                translated_summary = translator.translate_text(result.summary, target_lang="KO").text
            except Exception:
                translated_summary = result.summary
        else:
            translated_summary = result.summary
            
        papers.append({
            "title": result.title,
            "url": result.entry_id,
            "summary": translated_summary
        })
    return papers

# 3. 메일로 발송할 HTML 본문 구성
def create_email_html(market_data, arxiv_data):
    html = f"""
    <div style="font-family: 'Malgun Gothic', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; line-height: 1.6;">
        <h2 style="color: #0f2c59; border-bottom: 2px solid #0f2c59; padding-bottom: 10px;">📈 오늘의 핵심 금융 시황 요약</h2>
        <ul style="list-style-type: none; padding-left: 0;">
            <li style="margin-bottom: 8px;"><b>💰 원/달러 환율:</b> {market_data['exchange_rate']}</li>
            <li style="margin-bottom: 8px;"><b>📊 미 10년물 국채 금리:</b> {market_data['us_10y_bond']}</li>
            <li style="margin-bottom: 8px; color: #555;"><b>📢 글로벌 주요 이슈:</b> {market_data['headline']}</li>
        </ul>
        <br>
        <h2 style="color: #1a5f7a; border-bottom: 2px solid #1a5f7a; padding-bottom: 10px;">🤖 최신 AI & LLM 논문 동향 (arXiv)</h2>
        <ul style="padding-left: 20px;">
    """
    
    for paper in arxiv_data:
        html += f"""
        <li style="margin-bottom: 20px;">
            <a href='{paper['url']}' style="text-decoration: none; color: #007acc; font-weight: bold; font-size: 15px;">{paper['title']}</a><br>
            <p style="font-size: 13px; color: #333; margin-top: 5px; text-align: justify;">{paper['summary'][:300]}...</p>
        </li>
        """
        
    html += """
        </ul>
        <hr style="border: 0; border-top: 1px solid #ccc; margin-top: 30px;">
        <p style="font-size: 11px; color: #888; text-align: center;">본 메일은 GitHub Actions를 통해 자동 발송된 일일 브리핑입니다.</p>
    </div>
    """
    return html

# 4. SMTP를 통한 메일 발송 함수
def send_email(html_content):
    sender = os.environ.get('EMAIL_SENDER')
    password = os.environ.get('EMAIL_PASSWORD') 
    receiver = os.environ.get('EMAIL_RECEIVER')

    if not sender or not password or not receiver:
        print("❌ 필수 이메일 환경변수가 누락되었습니다. GitHub Secrets 설정을 확인하세요.")
        return

    msg = MIMEMultipart()
    msg['Subject'] = "☀️ [일일 브리핑] 글로벌 시황 및 최신 AI 논문 정보"
    msg['From'] = sender
    msg['To'] = receiver
    msg.attach(MIMEText(html_content, 'html'))

    # 구글 SMTP 서버 연결 (Gmail 기준)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print("✅ 브리핑 메일 발송 성공!")
    except Exception as e:
        print(f"❌ 메일 발송 실패: {e}")

if __name__ == "__main__":
    print("🔄 데이터 수집 및 분석 시작...")
    m_data = fetch_market_data()
    a_data = fetch_arxiv_papers()
    
    print("🔄 이메일 본문 생성 중...")
    email_content = create_email_html(m_data, a_data)
    
    print("🔄 메일 발송 중...")
    send_email(email_content)
