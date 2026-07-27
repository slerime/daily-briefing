"""
매일 오전: 미국 경제뉴스 Top10 수집 → Claude API로 번역/단어/종목 파급력 분석
→ HTML 학습 페이지 생성(GitHub Pages) → 카카오톡으로 링크 전송

실행 모드:
  python daily_news.py generate  # 뉴스 수집 + 페이지 생성 (커밋 전 단계)
  python daily_news.py notify    # 카카오톡 전송 (push 완료 후 단계)

필요 환경변수 (GitHub Secrets):
  ANTHROPIC_API_KEY   : Claude API 키 (console.anthropic.com)
  KAKAO_REST_API_KEY  : 카카오 앱 REST API 키
  KAKAO_REFRESH_TOKEN : 카카오 리프레시 토큰
  PAGE_URL            : GitHub Pages 주소 (예: https://아이디.github.io/저장소명/)
"""
import os
import sys
import json
import html
import re
import requests
import feedparser
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST)
DATE_STR = TODAY.strftime("%Y-%m-%d")
DATE_KR = TODAY.strftime("%m월 %d일")
IS_MORNING = TODAY.hour < 12
EDITION = "새벽" if IS_MORNING else "오후"          # 페이지 제목용
EDITION_EN = "Pre-Market" if IS_MORNING else "Afternoon"
EDITION_SUFFIX = "am" if IS_MORNING else "pm"       # 아카이브 파일명용

# 미국 투자자들이 많이 보는 경제 뉴스 피드
RSS_FEEDS = [
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC Top News", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

DOCS_DIR = "docs"
CACHE_FILE = "headline_cache.json"  # notify 단계에서 사용


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def fetch_top_news(limit: int = 10) -> list[dict]:
    """여러 피드에서 최신 뉴스를 모아 상위 N건 선정 (피드별 균형 배분)"""
    collected, seen_titles = [], set()
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                title = strip_html(entry.get("title", ""))
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
                collected.append({
                    "source": source,
                    "title": title,
                    "summary": strip_html(entry.get("summary", ""))[:600],
                    "link": entry.get("link", ""),
                })
        except Exception as e:
            print(f"[warn] {source} 피드 실패: {e}")
    return collected[:limit]


# ---------------------------------------------------------------- Claude 분석

ANALYSIS_PROMPT = """당신은 한국인 개인투자자를 위한 영어 학습 겸 시장 브리핑 에디터입니다.
아래 미국 경제뉴스 목록을 분석해 JSON 배열로만 응답하세요. 마크다운 코드블록 없이 순수 JSON만 출력합니다.

각 뉴스마다 다음 필드를 생성:
- "headline_kr": 뉴스의 핵심을 한 줄로 요약한 한국어 문장 (30자 내외, 명사형 종결. 예: "연준, 9월 금리 인하 가능성 시사")
- "english": 영어 원문 요약 2~3문장. 제공된 headline과 summary를 자연스러운 영어 문단으로 정리 (초·중급 학습자가 읽을 수 있게 지나치게 어려운 구문은 피하되 원문의 표현은 살릴 것)
- "korean": 위 english의 자연스러운 한국어 번역. 핵심 문장이 무엇인지 드러나도록 정확하게 번역
- "vocab": 영어 단어 4~6개 [{"word":"...", "meaning":"한국어 뜻"}]. 규칙:
  * 기사 이해에 핵심적인 단어 위주 (초급 학습자 기준)
  * 복합 용어는 반드시 구성 단어별로 분해해 각각 등재. 예: "rate cut"이 나오면 → {"word":"rate","meaning":"금리, 비율"}, {"word":"cut","meaning":"인하, 삭감"} 두 항목으로. "labor force"면 → labor: 노동, force: 힘/인력 으로 분해
  * 대명사·전치사라도 문장 이해에 중요하면 포함
- "idioms": 숙어/표현 1~2개 [{"phrase":"...", "meaning":"한국어 뜻"}] (없으면 빈 배열)
- "impact": {
    "sectors": ["영향 업종 1~3개 (한국어)"],
    "us_tickers": ["관련 미국 종목 티커 1~3개"],
    "kr_stocks": ["관련 한국 종목명 0~3개 (직접 연관 있을 때만)"],
    "reasoning": "반드시 [원인→경로→결과] 인과 구조로 3~4문장의 한국어 설명. ①원인: 뉴스의 핵심 이벤트가 무엇인지 ②경로: 그 이벤트가 어떤 메커니즘(금리·환율·수요·공급망·원자재가·규제 등)을 통해 전달되는지 ③결과: 최종적으로 어느 업종/종목에 호재/악재/중립으로 작용하는지. '왜 그렇게 되는지'의 논리 연결고리가 끊기지 않게 작성"
  }

뉴스 목록:
{NEWS_JSON}
"""


def analyze_with_claude(news: list[dict]) -> list[dict]:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    prompt = ANALYSIS_PROMPT.replace(
        "{NEWS_JSON}",
        json.dumps(
            [{"headline": n["title"], "summary": n["summary"]} for n in news],
            ensure_ascii=False, indent=1,
        ),
    )
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5",  # 저비용. 품질 높이려면 claude-sonnet-4-6
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    res.raise_for_status()
    text = "".join(b.get("text", "") for b in res.json()["content"])
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


# ---------------------------------------------------------------- HTML 생성

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_kr} {edition} 시장 브리핑</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=IBM+Plex+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root {{
  --ink:#1B2233; --paper:#FAF9F6; --dawn:#E8A13D; --sea:#3E5C76;
  --mist:#E5E2DA; --note:#FDF6E9;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--paper); color:var(--ink);
  font-family:'IBM Plex Sans KR',sans-serif; line-height:1.65; }}
.dawn-line {{ height:4px;
  background:linear-gradient(90deg,var(--ink) 0%,var(--sea) 45%,var(--dawn) 100%); }}
header {{ background:var(--ink); color:var(--paper); padding:34px 20px 28px; }}
header .kicker {{ font-family:'IBM Plex Mono',monospace; font-size:12px;
  letter-spacing:.18em; color:var(--dawn); text-transform:uppercase; }}
header h1 {{ font-size:24px; font-weight:700; margin-top:8px; }}
header .sub {{ font-size:13px; color:#B8BECB; margin-top:6px; }}
main {{ max-width:720px; margin:0 auto; padding:28px 16px 60px; }}
.digest {{ background:#fff; border:1px solid var(--mist); border-left:4px solid var(--dawn);
  border-radius:10px; padding:18px 20px; margin-bottom:30px; }}
.digest .label {{ font-family:'IBM Plex Mono',monospace; font-size:11px;
  letter-spacing:.15em; text-transform:uppercase; color:var(--dawn); }}
.digest ol {{ margin:10px 0 0 2px; padding-left:20px; }}
.digest li {{ font-size:14.5px; margin:6px 0; }}
.digest a {{ color:var(--ink); text-decoration:none; }}
.digest a:hover {{ color:var(--sea); }}
article {{ background:#fff; border:1px solid var(--mist); border-radius:10px;
  padding:24px 20px 18px; margin-bottom:26px; position:relative; }}
.num {{ font-family:'IBM Plex Mono',monospace; font-size:12px; color:var(--dawn);
  letter-spacing:.12em; }}
.src {{ font-size:11px; color:#8A8F9C; margin-left:8px; }}
h2 {{ font-family:'Newsreader',serif; font-size:20px; font-weight:600;
  line-height:1.35; margin:8px 0 12px; }}
h2 a {{ color:var(--ink); text-decoration:none; border-bottom:1px solid var(--mist); }}
.eng {{ font-family:'Newsreader',serif; font-size:17px; line-height:1.7; }}
.listen {{ margin-top:10px; font-family:'IBM Plex Sans KR',sans-serif; font-size:12.5px;
  padding:6px 14px; border-radius:20px; border:1px solid var(--sea);
  background:#fff; color:var(--sea); cursor:pointer; }}
.listen.playing {{ background:var(--sea); color:#fff; }}
.kor {{ background:#F3F5F8; border-left:3px solid var(--sea); border-radius:0 6px 6px 0;
  padding:12px 14px; margin-top:14px; font-size:14.5px; }}
.kor .label, .impact .label, .note .label {{
  font-family:'IBM Plex Mono',monospace; font-size:10.5px; letter-spacing:.15em;
  text-transform:uppercase; display:block; margin-bottom:6px; }}
.kor .label {{ color:var(--sea); }}
.impact {{ margin-top:14px; border-top:1px dashed var(--mist); padding-top:14px; }}
.impact .label {{ color:var(--dawn); }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }}
.chip {{ font-size:12px; padding:3px 10px; border-radius:20px;
  background:var(--ink); color:var(--paper); }}
.chip.tk {{ font-family:'IBM Plex Mono',monospace; background:transparent;
  color:var(--ink); border:1px solid var(--ink); }}
.chip.kr {{ background:transparent; color:var(--sea); border:1px solid var(--sea); }}
.impact p {{ font-size:14px; }}
.note {{ margin:16px 0 0 auto; max-width:88%; width:fit-content;
  background:var(--note); border:1px dashed var(--dawn); border-radius:8px 8px 2px 8px;
  padding:10px 14px; font-size:13px; }}
.note .label {{ color:#B07A1F; }}
.note b {{ font-family:'Newsreader',serif; font-size:14px; }}
.note li {{ list-style:none; margin:2px 0; }}
footer {{ text-align:center; font-size:11.5px; color:#9AA0AC; padding:0 20px 40px; }}
</style>
</head>
<body>
<div class="dawn-line"></div>
<header>
  <span class="kicker">{edition_en} Briefing</span>
  <h1>{date_kr} {edition} 시장 브리핑</h1>
  <div class="sub">미국 경제뉴스 Top {count} · 영어 학습 + 종목 파급력 분석</div>
</header>
<main>
<div class="digest">
  <span class="label">Today's Digest — 오늘의 핵심</span>
  <ol>
{digest}
  </ol>
</div>
{articles}
</main>
<footer>본 페이지의 종목 분석은 AI가 생성한 참고 자료이며 투자 권유가 아닙니다.<br>
투자 판단과 책임은 본인에게 있습니다.</footer>
<script>
let curBtn = null;
function resetBtn() {{
  if (curBtn) {{ curBtn.classList.remove('playing'); curBtn.textContent = '🔊 영어 듣기'; curBtn = null; }}
}}
function speak(btn, id) {{
  const synth = window.speechSynthesis;
  if (synth.speaking) {{
    const same = (curBtn === btn);
    synth.cancel(); resetBtn();
    if (same) return; // 같은 버튼 다시 누르면 정지
  }}
  const u = new SpeechSynthesisUtterance(document.getElementById(id).textContent);
  u.lang = 'en-US';
  u.rate = 0.92; // 학습용으로 약간 천천히
  const voice = synth.getVoices().find(v => v.lang.startsWith('en-US'));
  if (voice) u.voice = voice;
  u.onend = resetBtn;
  u.onerror = resetBtn;
  synth.speak(u);
  curBtn = btn; btn.classList.add('playing'); btn.textContent = '⏸ 정지';
}}
// 일부 브라우저는 목소리 목록을 늦게 불러옴
window.speechSynthesis && window.speechSynthesis.getVoices();
</script>
</body>
</html>"""

ARTICLE_TEMPLATE = """<article id="a{num}">
  <span class="num">NO.{num:02d}</span><span class="src">{source}</span>
  <h2><a href="{link}" target="_blank" rel="noopener">{title}</a></h2>
  <p class="eng" id="eng-{num}">{english}</p>
  <button class="listen" onclick="speak(this,'eng-{num}')">🔊 영어 듣기</button>
  <div class="kor"><span class="label">한국어 해석</span>{korean}</div>
  <div class="impact">
    <span class="label">파급력 분석</span>
    <div class="chips">{chips}</div>
    <p>{reasoning}</p>
  </div>
  <div class="note">
    <span class="label">Vocab &amp; Idioms</span>
    <ul>{vocab_items}</ul>
  </div>
</article>"""


def esc(s: str) -> str:
    return html.escape(str(s or ""))


def build_html(news: list[dict], analysis: list[dict]) -> str:
    articles = []
    for i, (n, a) in enumerate(zip(news, analysis), 1):
        imp = a.get("impact", {})
        chips = "".join(f'<span class="chip">{esc(s)}</span>' for s in imp.get("sectors", []))
        chips += "".join(f'<span class="chip tk">{esc(t)}</span>' for t in imp.get("us_tickers", []))
        chips += "".join(f'<span class="chip kr">{esc(k)}</span>' for k in imp.get("kr_stocks", []))
        vocab = "".join(
            f"<li><b>{esc(v['word'])}</b> — {esc(v['meaning'])}</li>"
            for v in a.get("vocab", [])
        )
        vocab += "".join(
            f"<li><b>{esc(v['phrase'])}</b> — {esc(v['meaning'])}</li>"
            for v in a.get("idioms", [])
        )
        articles.append(ARTICLE_TEMPLATE.format(
            num=i, source=esc(n["source"]), link=esc(n["link"]), title=esc(n["title"]),
            english=esc(a.get("english", "")), korean=esc(a.get("korean", "")),
            chips=chips, reasoning=esc(imp.get("reasoning", "")), vocab_items=vocab,
        ))
    digest = "\n".join(
        f'    <li><a href="#a{i}">{esc(a.get("headline_kr") or n["title"])}</a></li>'
        for i, (n, a) in enumerate(zip(news, analysis), 1)
    )
    return PAGE_TEMPLATE.format(date_kr=DATE_KR, count=len(news),
                                edition=EDITION, edition_en=EDITION_EN,
                                digest=digest, articles="\n".join(articles))


# ---------------------------------------------------------------- 카카오 전송

def get_kakao_access_token() -> str:
    res = requests.post(
        "https://kauth.kakao.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["KAKAO_REST_API_KEY"],
            "refresh_token": os.environ["KAKAO_REFRESH_TOKEN"],
        }, timeout=10,
    )
    res.raise_for_status()
    return res.json()["access_token"]


def send_kakao(headlines: list[str]) -> None:
    page_url = os.environ["PAGE_URL"]
    preview = "\n".join(f"· {h[:38]}" for h in headlines[:2])
    text = (
        f"📰 {DATE_KR} {EDITION} 브리핑 도착\n"
        f"{preview}\n외 {max(len(headlines)-2,0)}건\n\n"
        f"👇 전체 브리핑 (오디오 듣기 지원)\n{page_url}"
    )
    template = {
        "object_type": "text",
        "text": text[:190],
        "link": {"web_url": page_url, "mobile_web_url": page_url},
        "button_title": "브리핑 열기",
    }
    res = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {get_kakao_access_token()}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=10,
    )
    res.raise_for_status()
    print("카카오 전송 완료")


# ---------------------------------------------------------------- 실행

def generate():
    news = fetch_top_news(10)
    if not news:
        sys.exit("뉴스 수집 실패 - 종료")
    print(f"뉴스 {len(news)}건 수집, Claude 분석 시작…")
    analysis = analyze_with_claude(news)
    page = build_html(news, analysis)

    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(f"{DOCS_DIR}/index.html", "w", encoding="utf-8") as f:
        f.write(page)
    with open(f"{DOCS_DIR}/{DATE_STR}-{EDITION_SUFFIX}.html", "w", encoding="utf-8") as f:  # 날짜별 아카이브
        f.write(page)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump([n["title"] for n in news], f, ensure_ascii=False)
    print("페이지 생성 완료:", f"{DOCS_DIR}/index.html")


def notify():
    with open(CACHE_FILE, encoding="utf-8") as f:
        headlines = json.load(f)
    send_kakao(headlines)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "generate"
    generate() if mode == "generate" else notify()
