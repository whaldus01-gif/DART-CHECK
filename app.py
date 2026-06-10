from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv
from pathlib import Path
import requests
import zipfile
import io
import xml.etree.ElementTree as ET
import os
import sys
import logging
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent / ".env")
app = Flask(__name__)
API_KEY = os.getenv("DART_API_KEY", "").strip()

# ── API 키 검증 ────────────────────────────────────────────
def validate_api_key() -> tuple[bool, str]:
    if not API_KEY:
        return False, ".env 파일에 DART_API_KEY 가 없습니다"
    if len(API_KEY) != 40:
        return False, f"API 키 길이 오류 ({len(API_KEY)}자, 정상 40자)"
    try:
        res = requests.get("https://opendart.fss.or.kr/api/company.json",
                           params={"crtfc_key": API_KEY, "corp_code": "00126380"}, timeout=10)
        data = res.json()
        if data.get("status") == "010":
            return False, "API 키 인증 실패"
        return True, "OK"
    except requests.Timeout:
        return False, "DART 연결 시간 초과"
    except Exception as e:
        return False, f"API 키 검증 오류: {e}"

_api_key_valid, _api_key_msg = validate_api_key()
if not _api_key_valid:
    # 키 자체가 없거나 형식이 틀린 경우만 종료. 네트워크 일시 오류로 서버가 죽으면 안 됨.
    if "DART_API_KEY" in _api_key_msg or "길이 오류" in _api_key_msg or "인증 실패" in _api_key_msg:
        log.error("API 키 오류: %s", _api_key_msg)
        sys.exit(1)
    log.warning("API 키 검증 보류 (네트워크 오류, 서버는 계속 기동): %s", _api_key_msg)
    _api_key_valid = True  # 일시적 네트워크 문제로 간주
else:
    log.info("API 키 확인 완료")

_corp_list: list[dict] = []
_corp_index: dict[str, dict] = {}
_corp_lock = threading.Lock()


def load_corp_list() -> list[dict]:
    global _corp_list, _corp_index
    if _corp_list:
        return _corp_list
    with _corp_lock:
        if _corp_list:  # 락 대기 중 다른 스레드가 이미 로드한 경우
            return _corp_list
        log.info("기업 목록 다운로드 중...")
        try:
            res = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                               params={"crtfc_key": API_KEY}, timeout=30)
            res.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"기업 목록 다운로드 실패: {e}")
        new_list: list[dict] = []
        new_index: dict[str, dict] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                with z.open("CORPCODE.xml") as f:
                    # iterparse + clear: 전체 트리를 메모리에 들고 있지 않음 (Render 512MB 대응)
                    for _, elem in ET.iterparse(f):
                        if elem.tag == "list":
                            corp = {
                                "corp_code":  elem.findtext("corp_code") or "",
                                "corp_name":  elem.findtext("corp_name") or "",
                                "stock_code": (elem.findtext("stock_code") or "").strip(),
                            }
                            if corp["corp_code"] and corp["corp_name"]:
                                new_list.append(corp)
                                new_index[corp["corp_code"]] = corp
                            elem.clear()
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
            raise RuntimeError(f"기업 목록 파일 파싱 실패: {e}")
        _corp_index = new_index
        _corp_list = new_list
        log.info("기업 목록 로드 완료: %d개", len(_corp_list))
    return _corp_list


def _preload_corp_list():
    try:
        load_corp_list()
    except Exception as e:
        log.error("기업 목록 사전 로드 실패: %s", e)

threading.Thread(target=_preload_corp_list, daemon=True).start()


def parse_amount(val) -> float | None:
    if val is None:
        return None
    val = str(val).strip().replace(",", "")
    if not val or val in ("-", "N/A"):
        return None
    try:
        return round(int(float(val)) / 1e8, 1)
    except (ValueError, OverflowError):
        return None


def validate_year(year: str) -> str | None:
    if not year or not year.isdigit():
        return "연도는 숫자여야 합니다"
    y = int(year)
    if y < 2000 or y > 2030:
        return f"연도 범위 오류: {year}"
    return None


VALID_REPORT_CODES = {"11011", "11012", "11013", "11014", "Q2"}
VALID_FS_DIVS = {"CFS", "OFS"}
REPORT_LABELS = {"11011": "연간", "11012": "반기", "11013": "Q1", "11014": "Q3", "Q2": "Q2"}


# ── USD/KRW 환율 (1시간 캐시) ──────────────────────────────
_fx_cache = {"rate": 1350.0, "ts": 0.0}

def get_usd_krw() -> float:
    now = time.time()
    if now - _fx_cache["ts"] < 3600:
        return _fx_cache["rate"]
    try:
        import yfinance as yf
        hist = yf.Ticker("USDKRW=X").history(period="1d")
        if not hist.empty:
            rate = float(hist["Close"].iloc[-1])
            _fx_cache["rate"] = rate
            _fx_cache["ts"] = now
            log.info("환율 갱신: %.1f", rate)
            return rate
    except Exception as e:
        log.warning("환율 조회 실패 (%s), 기본값 사용", e)
    return _fx_cache["rate"]


# ── DART 요약 계산 헬퍼 ────────────────────────────────────
def _fetch_summary(corp_code, corp_name, year, report_code, fs_div):
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            params={"crtfc_key": API_KEY, "corp_code": corp_code,
                    "bsns_year": year, "reprt_code": report_code, "fs_div": fs_div},
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
    except requests.Timeout:
        return None, ({"error": "DART 서버 응답 시간 초과"}, 504)
    except requests.RequestException as e:
        return None, ({"error": f"DART 연결 오류: {e}"}, 502)
    except ValueError:
        return None, ({"error": "DART 응답 파싱 오류"}, 502)

    if data.get("status") != "000":
        msg = data.get("message", "알 수 없는 오류")
        if "없습니다" in msg or data.get("status") == "013":
            label = REPORT_LABELS.get(report_code, report_code)
            return None, ({"error": f"{corp_name} {year}년 {label} 데이터가 없습니다. 아직 공시되지 않았거나 해당 기간 보고서가 없습니다."}, 400)
        if data.get("status") == "010":
            return None, ({"error": "API 키 인증 오류"}, 401)
        return None, ({"error": f"DART 오류 ({data.get('status')}): {msg}"}, 400)

    rows = data.get("list", [])
    if not rows:
        return None, ({"error": f"{corp_name} {year}년 데이터가 비어 있습니다"}, 400)

    tables: dict[str, list] = {"BS": [], "IS": [], "CIS": [], "CF": [], "SCE": []}
    for row in rows:
        sj = row.get("sj_div", "")
        if sj not in tables:
            continue
        try:
            indent = int(row.get("indent", 0))
        except (ValueError, TypeError):
            indent = 0
        tables[sj].append({
            "account_id":       str(row.get("account_id") or ""),
            "account_nm":       str(row.get("account_nm") or ""),
            "thstrm_amount":    parse_amount(row.get("thstrm_amount")),
            "frmtrm_amount":    parse_amount(row.get("frmtrm_amount")),
            "bfefrmtrm_amount": parse_amount(row.get("bfefrmtrm_amount")),
            "indent":           indent,
        })

    def find_by_id(aid, secs):
        for sj in secs:
            for r in tables.get(sj, []):
                if r["account_id"] == aid:
                    return r["thstrm_amount"]
        return None

    def is_pure_loss(name):
        return "손실" in name and "손익" not in name and "(손실)" not in name

    def find_by_name(names, secs):
        for name in names:
            for sj in secs:
                for r in tables.get(sj, []):
                    if r["account_nm"] == name:
                        return r["thstrm_amount"]
        return None

    def find_by_name_sign(names, secs):
        for name in names:
            for sj in secs:
                for r in tables.get(sj, []):
                    if r["account_nm"] == name:
                        v = r["thstrm_amount"]
                        if v is not None and is_pure_loss(name):
                            return -v
                        return v
        return None

    def find_metric(aid, names, secs):
        val = find_by_id(aid, secs)
        if val is None:
            val = find_by_name(names, secs)
        return val

    def find_op_income():
        for sj in ("IS", "CIS"):
            for r in tables.get(sj, []):
                if r["account_id"] == "dart_OperatingIncomeLoss":
                    v = r["thstrm_amount"]
                    if v is not None and is_pure_loss(r["account_nm"]):
                        return -v
                    return v
        return find_by_name_sign(["영업이익", "영업손익", "영업이익(손실)", "영업손실"], ("IS", "CIS"))

    def find_net_income():
        for sj in ("IS", "CIS"):
            for r in tables.get(sj, []):
                if r["account_id"] in ("ifrs-full_ProfitLoss", "ifrs-full_ProfitLossAttributableToOwnersOfParent"):
                    v = r["thstrm_amount"]
                    if v is not None and is_pure_loss(r["account_nm"]):
                        return -v
                    return v
        return find_by_name_sign(
            ["당기순이익", "분기순이익", "당기순손익", "당기순이익(손실)", "당기순손실", "분기순손실"], ("IS", "CIS")
        )

    summary = {
        "자산총계":        find_metric("ifrs-full_Assets",     ["자산총계"],                                ("BS",)),
        "부채총계":        find_metric("ifrs-full_Liabilities", ["부채총계"],                                ("BS",)),
        "자본총계":        find_metric("ifrs-full_Equity",      ["자본총계"],                                ("BS",)),
        "매출액":          find_metric("ifrs-full_Revenue",     ["매출액", "매출", "수익(매출액)", "영업수익"], ("IS", "CIS")),
        "영업이익":        find_op_income(),
        "당기순이익":      find_net_income(),
        "영업활동현금흐름": find_by_name(["영업활동현금흐름", "영업활동으로 인한 현금흐름"], ("CF",)),
    }

    core_fields = ["매출액", "영업이익", "자산총계"]
    if not any(summary.get(k) is not None for k in core_fields):
        return None, ({"error": f"{corp_name} {year}년 재무 데이터를 파싱할 수 없습니다."}, 400)

    return summary, None


def _sub(a, b):
    if a is not None and b is not None:
        return round(a - b, 1)
    return a if a is not None else b


# ── 해외 재무 헬퍼 ─────────────────────────────────────────
def _get_yf_col(df, year: int, target_month: int | None):
    """DataFrame에서 연도+월에 맞는 컬럼 반환."""
    if df is None or df.empty:
        return None
    year = int(year)
    if target_month is None:
        cols = [c for c in df.columns if c.year == year]
    else:
        cols = [c for c in df.columns if c.year == year and c.month == target_month]
        if not cols:
            # 같은 연도 내 가장 가까운 월
            cols_year = [c for c in df.columns if c.year == year]
            if cols_year:
                cols = [min(cols_year, key=lambda c: abs(c.month - target_month))]
    return cols[0] if cols else None


def _yf_val(df, col, keys):
    """DataFrame에서 첫 번째 매칭 키의 값 반환."""
    if df is None or col is None:
        return None
    for k in keys:
        try:
            if k in df.index:
                v = df.loc[k, col]
                if v is not None and v == v:  # NaN 체크
                    return float(v)
        except Exception:
            continue
    return None


# ── 라우트 ─────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "api_key_valid": _api_key_valid,
                    "corp_list_loaded": len(_corp_list)})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify([])
    if not _corp_list and _corp_lock.locked():
        # 시작 직후 목록 로딩 중 — 요청을 잡고 있지 말고 바로 안내
        return jsonify({"error": "기업 목록을 불러오는 중입니다. 10초 후 다시 검색해 주세요."}), 503
    try:
        corps = load_corp_list()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    results = [c for c in corps if keyword.lower() in c["corp_name"].lower()]
    results.sort(key=lambda x: x["stock_code"] == "")
    return jsonify(results[:30])


@app.route("/api/search_overseas")
def search_overseas():
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify([])
    try:
        res = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": keyword, "quotesCount": 15, "newsCount": 0, "enableFuzzyQuery": True},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        res.raise_for_status()
        quotes = res.json().get("quotes", [])
        results = []
        for q in quotes:
            if q.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            results.append({
                "ticker":   q.get("symbol", ""),
                "name":     q.get("shortname") or q.get("longname") or q.get("symbol", ""),
                "exchange": q.get("exchange", ""),
            })
        return jsonify(results[:15])
    except Exception as e:
        log.error("해외 검색 오류: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/financial")
def financial():
    corp_code   = request.args.get("corp_code", "").strip()
    corp_name   = request.args.get("corp_name", "").strip()
    year        = request.args.get("year", "2025").strip()
    report_code = request.args.get("report_code", "11011").strip()
    fs_div      = request.args.get("fs_div", "CFS").strip()

    if not corp_code:
        return jsonify({"error": "corp_code 파라미터가 없습니다"}), 400
    year_err = validate_year(year)
    if year_err:
        return jsonify({"error": year_err}), 400
    if report_code not in VALID_REPORT_CODES:
        return jsonify({"error": f"report_code 오류: {report_code}"}), 400
    if fs_div not in VALID_FS_DIVS:
        return jsonify({"error": f"fs_div 오류: {fs_div}"}), 400

    # Q2 = 반기 - Q1
    if report_code == "Q2":
        h1, h1_err = _fetch_summary(corp_code, corp_name, year, "11012", fs_div)
        if h1_err:
            return jsonify(h1_err[0]), h1_err[1]
        q1, q1_err = _fetch_summary(corp_code, corp_name, year, "11013", fs_div)
        if q1_err:
            summary = h1
        else:
            summary = {
                "매출액":          _sub(h1.get("매출액"),          q1.get("매출액")),
                "영업이익":        _sub(h1.get("영업이익"),        q1.get("영업이익")),
                "당기순이익":      _sub(h1.get("당기순이익"),      q1.get("당기순이익")),
                "영업활동현금흐름": _sub(h1.get("영업활동현금흐름"), q1.get("영업활동현금흐름")),
                "자산총계": h1.get("자산총계"),
                "부채총계": h1.get("부채총계"),
                "자본총계": h1.get("자본총계"),
            }
        return jsonify({"corp_name": corp_name, "year": year, "summary": summary, "tables": {}})

    summary, err = _fetch_summary(corp_code, corp_name, year, report_code, fs_div)
    if err:
        return jsonify(err[0]), err[1]
    return jsonify({"corp_name": corp_name, "year": year, "summary": summary, "tables": {}})


@app.route("/api/financial_overseas")
def financial_overseas():
    ticker_sym = request.args.get("ticker", "").strip().upper()
    corp_name  = request.args.get("corp_name", ticker_sym).strip()
    year       = request.args.get("year", "2024").strip()
    period     = request.args.get("period", "annual").strip()  # annual / Q1 / Q2 / Q3

    if not ticker_sym:
        return jsonify({"error": "ticker 파라미터가 없습니다"}), 400
    year_err = validate_year(year)
    if year_err:
        return jsonify({"error": year_err}), 400

    # 분기 → 월 매핑 (회계연도 기준 근사치)
    quarter_month = {"Q1": 3, "Q2": 6, "Q3": 9}
    target_month  = quarter_month.get(period)  # annual이면 None

    try:
        import yfinance as yf
        t = yf.Ticker(ticker_sym)

        if period == "annual":
            fin = t.financials
            bs  = t.balance_sheet
            cf  = t.cashflow
        else:
            fin = t.quarterly_financials
            bs  = t.quarterly_balance_sheet
            cf  = t.quarterly_cashflow

        fin_col = _get_yf_col(fin, year, target_month)
        bs_col  = _get_yf_col(bs,  year, target_month)
        cf_col  = _get_yf_col(cf,  year, target_month)

        if fin_col is None:
            label = {"annual": "연간", "Q1": "Q1", "Q2": "Q2", "Q3": "Q3"}.get(period, period)
            return jsonify({"error": f"{ticker_sym} {year}년 {label} 데이터가 없습니다. 아직 공시되지 않았거나 해당 기간이 없습니다."}), 400

        rev   = _yf_val(fin, fin_col, ["Total Revenue", "Revenue"])
        op    = _yf_val(fin, fin_col, ["Operating Income", "EBIT"])
        net   = _yf_val(fin, fin_col, ["Net Income", "Net Income Common Stockholders"])
        assets = _yf_val(bs, bs_col,  ["Total Assets"])
        equity = _yf_val(bs, bs_col,  ["Stockholders Equity", "Total Equity Gross Minority Interest"])
        liab   = _yf_val(bs, bs_col,  ["Total Liabilities Net Minority Interest", "Total Liabilities"])
        op_cf  = _yf_val(cf, cf_col,  ["Operating Cash Flow", "Cash Flow From Continuing Operations"])

        # 회사명
        try:
            info = t.info
            corp_name = info.get("shortName") or info.get("longName") or ticker_sym
        except Exception:
            corp_name = ticker_sym

        rate = get_usd_krw()

        def to_uk_krw(v):
            return round(v * rate / 1e8, 1) if v is not None else None

        def to_uk_usd(v):
            return round(v / 1e8, 1) if v is not None else None

        summary_krw = {
            "매출액": to_uk_krw(rev), "영업이익": to_uk_krw(op),
            "당기순이익": to_uk_krw(net), "자산총계": to_uk_krw(assets),
            "자본총계": to_uk_krw(equity), "부채총계": to_uk_krw(liab),
            "영업활동현금흐름": to_uk_krw(op_cf),
        }
        summary_usd = {
            "매출액": to_uk_usd(rev), "영업이익": to_uk_usd(op),
            "당기순이익": to_uk_usd(net), "자산총계": to_uk_usd(assets),
            "자본총계": to_uk_usd(equity), "부채총계": to_uk_usd(liab),
            "영업활동현금흐름": to_uk_usd(op_cf),
        }

        log.info("해외 조회 성공: %s %s %s (환율 %.1f)", ticker_sym, year, period, rate)
        return jsonify({
            "corp_name":    corp_name,
            "ticker":       ticker_sym,
            "year":         year,
            "exchange_rate": round(rate, 1),
            "summary":      summary_krw,
            "summary_usd":  summary_usd,
            "tables":       {},
            "overseas":     True,
        })

    except Exception as e:
        log.error("해외 재무 조회 오류 (%s): %s", ticker_sym, e)
        return jsonify({"error": f"데이터 조회 실패: {e}"}), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "존재하지 않는 경로입니다"}), 404

@app.errorhandler(500)
def server_error(e):
    log.error("서버 오류: %s", e)
    return jsonify({"error": "서버 내부 오류가 발생했습니다"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
