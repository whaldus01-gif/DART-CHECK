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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)

API_KEY = os.getenv("DART_API_KEY", "").strip()

# ── 시작 시 API 키 유효성 검증 ─────────────────────────────
def validate_api_key() -> tuple[bool, str]:
    if not API_KEY:
        return False, ".env 파일에 DART_API_KEY 가 없습니다"
    if len(API_KEY) != 40:
        return False, f"API 키 길이가 올바르지 않습니다 (현재 {len(API_KEY)}자, 정상 40자)"
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/company.json",
            params={"crtfc_key": API_KEY, "corp_code": "00126380"},
            timeout=10,
        )
        data = res.json()
        if data.get("status") == "010":
            return False, "API 키가 유효하지 않습니다 (DART 인증 실패)"
        return True, "OK"
    except requests.Timeout:
        return False, "DART 서버 연결 시간 초과 (네트워크 확인)"
    except Exception as e:
        return False, f"API 키 검증 중 오류: {e}"

_api_key_valid, _api_key_msg = validate_api_key()
if not _api_key_valid:
    log.error("API 키 오류: %s", _api_key_msg)
    sys.exit(1)
log.info("API 키 확인 완료")

_corp_list: list[dict] = []
_corp_index: dict[str, dict] = {}

def _preload_corp_list():
    try:
        load_corp_list()
    except Exception as e:
        log.error("기업 목록 사전 로드 실패: %s", e)

threading.Thread(target=_preload_corp_list, daemon=True).start()


def load_corp_list() -> list[dict]:
    global _corp_list, _corp_index
    if _corp_list:
        return _corp_list

    log.info("기업 목록 다운로드 중...")
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": API_KEY},
            timeout=30,
        )
        res.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"기업 목록 다운로드 실패: {e}")

    try:
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            with z.open("CORPCODE.xml") as f:
                tree = ET.parse(f)
    except (zipfile.BadZipFile, KeyError) as e:
        raise RuntimeError(f"기업 목록 파일 파싱 실패: {e}")

    for item in tree.getroot().findall("list"):
        corp = {
            "corp_code":  item.findtext("corp_code") or "",
            "corp_name":  item.findtext("corp_name") or "",
            "stock_code": (item.findtext("stock_code") or "").strip(),
        }
        if corp["corp_code"] and corp["corp_name"]:
            _corp_list.append(corp)
            _corp_index[corp["corp_code"]] = corp

    log.info("기업 목록 로드 완료: %d개", len(_corp_list))
    return _corp_list


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
        return f"연도 범위 오류: {year} (2000~2030 사이여야 합니다)"
    return None


VALID_REPORT_CODES = {"11011", "11012", "11013", "11014", "Q2"}
VALID_FS_DIVS = {"CFS", "OFS"}

REPORT_LABELS = {
    "11011": "연간",
    "11012": "반기",
    "11013": "Q1",
    "11014": "Q3",
    "Q2":    "Q2",
}


# ── 핵심 로직: DART 조회 + 요약 계산 ─────────────────────────
def _fetch_summary(corp_code: str, corp_name: str, year: str,
                   report_code: str, fs_div: str) -> tuple[dict | None, tuple | None]:
    """
    DART API 호출 → 파싱 → 요약 dict 반환.
    성공: (summary, None)
    실패: (None, (error_json, http_status))
    """
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            params={
                "crtfc_key":  API_KEY,
                "corp_code":  corp_code,
                "bsns_year":  year,
                "reprt_code": report_code,
                "fs_div":     fs_div,
            },
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
    except requests.Timeout:
        return None, ({"error": "DART 서버 응답 시간 초과. 잠시 후 다시 시도해 주세요"}, 504)
    except requests.RequestException as e:
        return None, ({"error": f"DART 서버 연결 오류: {e}"}, 502)
    except ValueError:
        return None, ({"error": "DART 응답 파싱 오류 (JSON 형식 아님)"}, 502)

    if data.get("status") != "000":
        msg = data.get("message", "알 수 없는 오류")
        if "없습니다" in msg or data.get("status") == "013":
            label = REPORT_LABELS.get(report_code, report_code)
            return None, ({"error": f"{corp_name} {year}년 {label} 데이터가 없습니다. 아직 공시되지 않았거나 해당 기간 보고서가 없습니다."}, 400)
        if data.get("status") == "010":
            return None, ({"error": "API 키 인증 오류. 서버를 재시작해 주세요"}, 401)
        return None, ({"error": f"DART 오류 ({data.get('status')}): {msg}"}, 400)

    rows = data.get("list", [])
    if not rows:
        return None, ({"error": f"{corp_name} {year}년 데이터가 비어 있습니다"}, 400)

    # ── 파싱 ──────────────────────────────────────────────────
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

    # ── 지표 탐색 헬퍼 ────────────────────────────────────────
    def find_by_id(account_id, sections):
        for sj in sections:
            for r in tables.get(sj, []):
                if r["account_id"] == account_id:
                    return r["thstrm_amount"]
        return None

    def is_pure_loss(name):
        return "손실" in name and "손익" not in name and "(손실)" not in name

    def find_by_name(names, sections):
        for name in names:
            for sj in sections:
                for r in tables.get(sj, []):
                    if r["account_nm"] == name:
                        return r["thstrm_amount"]
        return None

    def find_by_name_sign(names, sections):
        for name in names:
            for sj in sections:
                for r in tables.get(sj, []):
                    if r["account_nm"] == name:
                        v = r["thstrm_amount"]
                        if v is not None and is_pure_loss(name):
                            return -v
                        return v
        return None

    def find_metric(account_id, name_variants, sections):
        val = find_by_id(account_id, sections)
        if val is None:
            val = find_by_name(name_variants, sections)
        return val

    def find_op_income():
        for sj in ("IS", "CIS"):
            for r in tables.get(sj, []):
                if r["account_id"] == "dart_OperatingIncomeLoss":
                    v = r["thstrm_amount"]
                    if v is not None and is_pure_loss(r["account_nm"]):
                        return -v
                    return v
        return find_by_name_sign(
            ["영업이익", "영업손익", "영업이익(손실)", "영업손실"], ("IS", "CIS")
        )

    def find_net_income():
        target_ids = {
            "ifrs-full_ProfitLoss",
            "ifrs-full_ProfitLossAttributableToOwnersOfParent",
        }
        for sj in ("IS", "CIS"):
            for r in tables.get(sj, []):
                if r["account_id"] in target_ids:
                    v = r["thstrm_amount"]
                    if v is not None and is_pure_loss(r["account_nm"]):
                        return -v
                    return v
        return find_by_name_sign(
            ["당기순이익", "분기순이익", "당기순손익", "당기순이익(손실)", "당기순손실", "분기순손실"],
            ("IS", "CIS")
        )

    summary = {
        "자산총계":        find_metric("ifrs-full_Assets",      ["자산총계"],                                ("BS",)),
        "부채총계":        find_metric("ifrs-full_Liabilities",  ["부채총계"],                                ("BS",)),
        "자본총계":        find_metric("ifrs-full_Equity",       ["자본총계"],                                ("BS",)),
        "매출액":          find_metric("ifrs-full_Revenue",      ["매출액", "매출", "수익(매출액)", "영업수익"], ("IS", "CIS")),
        "영업이익":        find_op_income(),
        "당기순이익":      find_net_income(),
        "영업활동현금흐름": find_by_name(["영업활동현금흐름", "영업활동으로 인한 현금흐름"], ("CF",)),
    }

    core_fields = ["매출액", "영업이익", "자산총계"]
    found = [k for k in core_fields if summary.get(k) is not None]
    if not found:
        log.warning("핵심 지표 없음: %s %s %s %s | 섹션: %s",
                    corp_name, year, report_code, fs_div,
                    {k: len(v) for k, v in tables.items()})
        return None, ({
            "error": f"{corp_name} {year}년 재무 데이터를 파싱할 수 없습니다. (계정명이 표준과 다를 수 있습니다)",
            "debug_sections": {k: len(v) for k, v in tables.items()},
        }, 400)

    return summary, None


def _sub(a, b):
    """None 안전 빼기. 둘 다 있으면 a-b, 한쪽만 있으면 그 값 반환."""
    if a is not None and b is not None:
        return round(a - b, 1)
    return a if a is not None else b


# ── 헬스체크 ──────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "api_key_valid": _api_key_valid,
        "corp_list_loaded": len(_corp_list),
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify([])
    try:
        corps = load_corp_list()
    except RuntimeError as e:
        log.error("기업 목록 로드 오류: %s", e)
        return jsonify({"error": str(e)}), 500

    results = [c for c in corps if keyword.lower() in c["corp_name"].lower()]
    results.sort(key=lambda x: x["stock_code"] == "")
    return jsonify(results[:30])


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

    # ── Q2 전용 처리: 반기(H1) - Q1 ───────────────────────────
    if report_code == "Q2":
        h1_sum, h1_err = _fetch_summary(corp_code, corp_name, year, "11012", fs_div)
        if h1_err:
            return jsonify(h1_err[0]), h1_err[1]

        q1_sum, q1_err = _fetch_summary(corp_code, corp_name, year, "11013", fs_div)
        if q1_err:
            # Q1 데이터가 없으면 H1 그대로 반환 (Q1이 없는 경우)
            log.warning("Q2 계산: Q1 데이터 없음 (%s %s), H1 그대로 반환", corp_name, year)
            summary = h1_sum
        else:
            # IS/CF 항목 = H1 - Q1 (누적 → 분기)
            # BS 항목 = H1 그대로 (시점 데이터)
            summary = {
                "매출액":          _sub(h1_sum.get("매출액"),          q1_sum.get("매출액")),
                "영업이익":        _sub(h1_sum.get("영업이익"),        q1_sum.get("영업이익")),
                "당기순이익":      _sub(h1_sum.get("당기순이익"),      q1_sum.get("당기순이익")),
                "영업활동현금흐름": _sub(h1_sum.get("영업활동현금흐름"), q1_sum.get("영업활동현금흐름")),
                "자산총계":        h1_sum.get("자산총계"),
                "부채총계":        h1_sum.get("부채총계"),
                "자본총계":        h1_sum.get("자본총계"),
            }

        log.info("Q2 조회 성공: %s %s %s", corp_name, year, fs_div)
        return jsonify({"corp_name": corp_name, "year": year, "summary": summary, "tables": {}})

    # ── 일반 조회 ──────────────────────────────────────────────
    summary, err = _fetch_summary(corp_code, corp_name, year, report_code, fs_div)
    if err:
        return jsonify(err[0]), err[1]

    log.info("조회 성공: %s %s %s %s", corp_name, year, report_code, fs_div)
    return jsonify({"corp_name": corp_name, "year": year, "summary": summary, "tables": {}})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "존재하지 않는 경로입니다"}), 404

@app.errorhandler(500)
def server_error(e):
    log.error("서버 오류: %s", e)
    return jsonify({"error": "서버 내부 오류가 발생했습니다"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
