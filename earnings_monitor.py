#!/usr/bin/env python3
"""
NVDA / MU 이익추정치 모니터 v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
데이터 소스 (이중 레이어, 정합성 교차 검증):
  Layer1 - yfinance eps_trend:       내장 7d/30d/60d/90d 비교
  Layer2 - data/estimates_history.json: 자체 6개월 스냅샷 누적

수집 항목:
  - EPS 추정치 (현재분기/다음분기/현재FY/다음FY)
  - 매출 추정치 (동일 기간)
  - 애널리스트 상향/하향 수정 카운트
  - 추세꺽임 감지 (연속 방향 전환)
  - 6개월 ASCII sparkline

평일 08:00 KST 텔레그램 자동 발송
"""

import os, sys, json, subprocess, math
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    import pytz
    KST = pytz.timezone("Asia/Seoul")
except ImportError:
    KST = None

try:
    import yfinance as yf
except ImportError:
    print("[FATAL] pip install yfinance"); sys.exit(1)

# ─── 상수 ────────────────────────────────────────────────────────────────────
TICKERS       = ["NVDA", "MU"]
TICKER_NAMES  = {"NVDA": "NVIDIA", "MU": "Micron Technology"}
HISTORY_DAYS  = 185   # 6개월

DATA_DIR      = Path(__file__).parent / "data"
HISTORY_FILE  = DATA_DIR / "estimates_history.json"

PERIODS       = ["0q", "+1q", "0y", "+1y"]
PERIOD_LABELS = {
    "0q":  "현재분기",
    "+1q": "다음분기",
    "0y":  "현재FY",
    "+1y": "다음FY",
}

# 데이터 정합성 임계값
EPS_CROSS_VALIDATE_THRESHOLD = 0.10  # 10% 이상 차이 → 경고
REVERSAL_MIN_HISTORY         = 4     # 최소 4개 데이터포인트 이상일 때만 추세꺽임 판단

# ─── 설정 로드 ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN",   ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "github_token":     os.environ.get("GITHUB_TOKEN",     ""),
    }
    cp = Path(__file__).parent / "config.json"
    if cp.exists():
        with open(cp, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg

# ─── 보조 함수 ───────────────────────────────────────────────────────────────
def _safe_float(v) -> float | None:
    try:
        if v is None: return None
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    except: return None

def _safe_int(v) -> int | None:
    try:
        return None if v is None else int(v)
    except: return None

def now_kst() -> datetime:
    if KST:
        return datetime.now(KST)
    import time
    # KST fallback
    utc_ts = datetime.utcnow()
    return utc_ts + timedelta(hours=9)

def sparkline(values: list, length: int = 14) -> str:
    """ASCII 추이 시각화 (▁▂▃▄▅▆▇█)"""
    blocks = "▁▂▃▄▅▆▇█"
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return "─" * max(length, len(vals))
    # 균등 샘플링
    step = max(1, len(vals) // length)
    sampled = vals[::step][-length:]
    mn, mx = min(sampled), max(sampled)
    if abs(mx - mn) < 1e-9:
        return "─" * len(sampled)
    return "".join(blocks[int((v - mn) / (mx - mn) * 7)] for v in sampled)

# ─── yfinance 데이터 수집 ─────────────────────────────────────────────────────
def parse_eps_trend_df(df) -> dict:
    """
    eps_trend DataFrame 파싱 (두 가지 방향 대응)
    Returns: {period: {"current", "7dAgo", "30dAgo", "60dAgo", "90dAgo"}}
    """
    if df is None or df.empty:
        return {}

    result = {}
    idx_str = [str(i) for i in df.index]
    col_str = [str(c) for c in df.columns]

    # Orientation A: index=time_labels, columns=periods
    time_labels = ["current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"]
    key_map = {
        "current":    "current",
        "7daysAgo":   "7dAgo",
        "30daysAgo":  "30dAgo",
        "60daysAgo":  "60dAgo",
        "90daysAgo":  "90dAgo",
    }

    if any(t in idx_str for t in ["current", "7daysAgo"]):
        for period in PERIODS:
            if period in col_str:
                result[period] = {}
                for tl in time_labels:
                    if tl in idx_str:
                        try:
                            val = df.loc[tl, period]
                            result[period][key_map[tl]] = _safe_float(val)
                        except: pass

    # Orientation B: index=periods, columns=time_labels
    elif any(p in idx_str for p in PERIODS):
        for period in PERIODS:
            if period in idx_str:
                result[period] = {}
                for tl in time_labels:
                    if tl in col_str:
                        try:
                            val = df.loc[period, tl]
                            result[period][key_map[tl]] = _safe_float(val)
                        except: pass

    return result

def fetch_ticker_data(ticker: str) -> dict:
    """
    yfinance 종합 데이터 수집
    - eps_trend (내장 7d/30d/60d/90d)
    - earnings_estimate (평균/고/저/애널리스트 수)
    - revenue_estimate
    - eps_revisions (상향/하향 카운트)
    - 현재가 / 목표가
    """
    result = {
        "ticker":           ticker,
        "date":             date.today().isoformat(),
        "fetch_ts":         now_kst().strftime("%Y-%m-%d %H:%M KST"),
        "errors":           [],
        "eps_trend":        {},   # parsed from yf eps_trend
        "earnings_estimate":{},   # period → {avg, low, high, n}
        "revenue_estimate": {},   # period → {avg, low, high, n, growth}
        "eps_revisions":    {},   # period → {up7d, up30d, down7d, down30d}
        "price":            None,
        "mean_target":      None,
        "n_analysts_price": None,
        "recommendation":   None,
        "data_quality":     "ok",
        "quality_notes":    [],
    }

    try:
        tkr = yf.Ticker(ticker)

        # ── 1. EPS Trend ──────────────────────────────────────────────────────
        try:
            df = None
            try:   df = tkr.get_eps_trend()
            except: pass
            if df is None or (hasattr(df, 'empty') and df.empty):
                df = tkr.eps_trend
            result["eps_trend"] = parse_eps_trend_df(df)
        except Exception as e:
            result["errors"].append(f"eps_trend: {e}")

        # ── 2. Earnings Estimate ──────────────────────────────────────────────
        try:
            df = None
            try:   df = tkr.get_earnings_estimate()
            except: pass
            if df is None or (hasattr(df, 'empty') and df.empty):
                df = tkr.earnings_estimate

            if df is not None and not df.empty:
                for period in PERIODS:
                    if period in df.index:
                        row = df.loc[period]
                        result["earnings_estimate"][period] = {
                            "avg":       _safe_float(row.get("avg")),
                            "low":       _safe_float(row.get("low")),
                            "high":      _safe_float(row.get("high")),
                            "n":         _safe_int(row.get("numberOfAnalysts")),
                            "growth":    _safe_float(row.get("growth")),
                            "yearAgoEps":_safe_float(row.get("yearAgoEps")),
                        }
        except Exception as e:
            result["errors"].append(f"earnings_estimate: {e}")

        # ── 3. Revenue Estimate ───────────────────────────────────────────────
        try:
            df = None
            try:   df = tkr.get_revenue_estimate()
            except: pass
            if df is None or (hasattr(df, 'empty') and df.empty):
                df = tkr.revenue_estimate

            if df is not None and not df.empty:
                for period in PERIODS:
                    if period in df.index:
                        row = df.loc[period]
                        result["revenue_estimate"][period] = {
                            "avg":    _safe_float(row.get("avg")),
                            "low":    _safe_float(row.get("low")),
                            "high":   _safe_float(row.get("high")),
                            "n":      _safe_int(row.get("numberOfAnalysts")),
                            "growth": _safe_float(row.get("growth")),
                        }
        except Exception as e:
            result["errors"].append(f"revenue_estimate: {e}")

        # ── 4. EPS Revisions ──────────────────────────────────────────────────
        try:
            df = None
            try:   df = tkr.get_eps_revisions()
            except: pass
            if df is None or (hasattr(df, 'empty') and df.empty):
                df = tkr.eps_revisions

            if df is not None and not df.empty:
                # yfinance 컬럼명 케이스 불일치 대응 (downLast7Days / downLast7days)
                col_map = {c.lower(): c for c in df.columns}

                def get_col(row, name_lower):
                    actual = col_map.get(name_lower, name_lower)
                    v = row.get(actual)
                    if v is None:  # fallback: 대소문자 무시 검색
                        for col in df.columns:
                            if col.lower() == name_lower:
                                v = row.get(col)
                                break
                    return _safe_int(v)

                for period in PERIODS:
                    if period in df.index:
                        row = df.loc[period]
                        result["eps_revisions"][period] = {
                            "up7d":   get_col(row, "uplast7days"),
                            "down7d": get_col(row, "downlast7days"),
                            "up30d":  get_col(row, "uplast30days"),
                            "down30d":get_col(row, "downlast30days"),
                        }
        except Exception as e:
            result["errors"].append(f"eps_revisions: {e}")

        # ── 5. 현재가 / 목표가 ────────────────────────────────────────────────
        try:
            info = tkr.info
            result["price"]            = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
            result["mean_target"]      = _safe_float(info.get("targetMeanPrice"))
            result["n_analysts_price"] = _safe_int(info.get("numberOfAnalystOpinions"))
            result["recommendation"]   = info.get("recommendationKey", "")
        except Exception as e:
            result["errors"].append(f"info: {e}")

        # ── 6. 데이터 정합성 교차 검증 ───────────────────────────────────────
        _cross_validate(result)

    except Exception as e:
        result["errors"].append(f"fatal: {e}")
        result["data_quality"] = "error"

    return result

def _cross_validate(data: dict):
    """eps_trend.current vs earnings_estimate.avg 교차 검증"""
    for period in PERIODS:
        et  = data["eps_trend"].get(period, {})
        ee  = data["earnings_estimate"].get(period, {})
        v1  = et.get("current")
        v2  = ee.get("avg")
        if v1 is not None and v2 is not None and v2 != 0:
            diff_pct = abs(v1 - v2) / abs(v2)
            if diff_pct > EPS_CROSS_VALIDATE_THRESHOLD:
                note = (f"{PERIOD_LABELS.get(period, period)} EPS: "
                        f"eps_trend={v1:.2f} vs estimate_avg={v2:.2f} "
                        f"(차이 {diff_pct*100:.1f}%) ← 데이터 불일치")
                data["quality_notes"].append(note)
                data["data_quality"] = "warn"

# ─── 히스토리 관리 ───────────────────────────────────────────────────────────
def load_history() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            h = json.load(f)
        # 호환성: TICKERS 없으면 초기화
        for t in TICKERS:
            if t not in h:
                h[t] = []
        return h
    return {t: [] for t in TICKERS}

def save_history(history: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def prune_history(history: dict) -> dict:
    cutoff = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    for t in TICKERS:
        history[t] = [r for r in history.get(t, []) if r.get("date", "") >= cutoff]
    return history

def make_snapshot(data: dict) -> dict:
    """히스토리 저장용 핵심 수치 추출"""
    snap = {
        "date":  data["date"],
        "price": data.get("price"),
    }
    for period in PERIODS:
        # EPS: eps_trend.current 우선, fallback → earnings_estimate.avg
        et  = data["eps_trend"].get(period, {})
        ee  = data["earnings_estimate"].get(period, {})
        eps = et.get("current") if et.get("current") is not None else ee.get("avg")
        snap[f"eps_{period}"] = eps
        snap[f"eps_{period}_n"] = ee.get("n")

        # 매출
        rv = data["revenue_estimate"].get(period, {})
        snap[f"rev_{period}"] = rv.get("avg")
    return snap

# ─── 추세 분석 ───────────────────────────────────────────────────────────────
def analyze_trend(history: list, field: str) -> dict:
    """
    히스토리 기반 추세 분석
    Returns: {
        direction: "up"|"down"|"flat"|"unknown",
        consecutive: int,
        reversal: bool,
        pct_1m: float|None,
        pct_3m: float|None,
        pct_6m: float|None,
        sparkline_vals: list,
    }
    """
    sorted_h = sorted(
        [r for r in history if r.get(field) is not None],
        key=lambda x: x["date"]
    )
    vals  = [r[field] for r in sorted_h]
    dates = [r["date"] for r in sorted_h]
    today = date.today().isoformat()

    def pct_change_ago(days: int) -> float | None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        past   = [r for r in sorted_h if r["date"] <= cutoff]
        if not past or not vals:
            return None
        old = past[-1][field]
        cur = vals[-1]
        if old and old != 0:
            return round((cur - old) / abs(old) * 100, 2)
        return None

    # 방향성 배열
    directions = []
    for i in range(1, len(vals)):
        diff = vals[i] - vals[i - 1]
        if   diff >  abs(vals[i - 1]) * 0.001:  directions.append("up")
        elif diff < -abs(vals[i - 1]) * 0.001:  directions.append("down")
        else:                                    directions.append("flat")

    current_dir = directions[-1] if directions else "unknown"

    # 연속 카운트
    consecutive = 1
    if directions:
        for d in reversed(directions[:-1]):
            if d == current_dir: consecutive += 1
            else: break

    # 추세꺽임: 이전 주 방향 vs 현재 방향 (최소 REVERSAL_MIN_HISTORY 필요)
    reversal = False
    if len(sorted_h) >= REVERSAL_MIN_HISTORY and len(directions) >= 3:
        prev_dirs = directions[:-1]
        # 이전 방향의 최빈값
        non_flat  = [d for d in prev_dirs if d != "flat"]
        if non_flat:
            prev_main = max(set(non_flat), key=non_flat.count)
            if current_dir not in ("flat", "unknown") and current_dir != prev_main:
                reversal = True

    return {
        "direction":   current_dir,
        "consecutive": consecutive,
        "reversal":    reversal,
        "pct_1m":      pct_change_ago(30),
        "pct_3m":      pct_change_ago(90),
        "pct_6m":      pct_change_ago(180),
        "sparkline_vals": vals,
        "n_points":    len(vals),
    }

def build_analysis(ticker: str, history: list) -> dict:
    """모든 EPS 필드에 대한 추세 분석"""
    analysis = {"eps": {}, "rev": {}, "reversals": []}
    for period in PERIODS:
        field = f"eps_{period}"
        tr    = analyze_trend(history, field)
        analysis["eps"][period] = tr
        if tr["reversal"]:
            analysis["reversals"].append({
                "label":       PERIOD_LABELS.get(period, period),
                "field":       field,
                "direction":   tr["direction"],
                "consecutive": tr["consecutive"],
                "pct_1m":      tr["pct_1m"],
            })
        # 매출 추세도 분석
        rev_field = f"rev_{period}"
        analysis["rev"][period] = analyze_trend(history, rev_field)
    return analysis

# ─── 텔레그램 메시지 생성 ─────────────────────────────────────────────────────
def fmt_pct(v: float | None, prefix: str = "") -> str:
    if v is None: return ""
    arrow = "▲" if v > 0 else "▼" if v < 0 else "─"
    return f"{arrow}{prefix}{v:+.1f}%"

def fmt_billion(v: float | None) -> str:
    if v is None: return "N/A"
    return f"${v / 1e9:.2f}B"

def fmt_eps(v: float | None) -> str:
    if v is None: return "N/A"
    return f"${v:.2f}"

def build_ticker_section(ticker: str, current: dict, history: list, analysis: dict) -> str:
    """종목별 섹션 — 전문 리서치 스타일 compact 포맷"""
    name      = TICKER_NAMES.get(ticker, ticker)
    recom_map = {
        "strongbuy": "강력매수", "buy": "매수",
        "hold": "보유", "sell": "매도", "strongsell": "강력매도"
    }

    lines = []

    # ── 종목 헤더 ─────────────────────────────────────────────────────────
    price  = current.get("price")
    target = current.get("mean_target")
    recom  = recom_map.get((current.get("recommendation") or "").lower(), current.get("recommendation", ""))
    n_a    = current.get("n_analysts_price")

    price_str  = f"${price:.2f}" if price else "N/A"
    target_str = f"  목표 ${target:.0f}({(target-price)/price*100:+.1f}%)" if (target and price) else ""
    analyst_str = f"  [{n_a}명·{recom}]" if n_a else ""
    lines.append(f"<b>{ticker} ({name})</b>  {price_str}{target_str}{analyst_str}")

    # ── EPS 추정치 (컨센서스) ────────────────────────────────────────────
    et_data = current.get("eps_trend", {})
    ee_data = current.get("earnings_estimate", {})
    eps_rows = []
    for period in PERIODS:
        et  = et_data.get(period, {})
        ee  = ee_data.get(period, {})
        cur = et.get("current") if et.get("current") is not None else ee.get("avg")
        if cur is None: continue
        d30 = et.get("30dAgo"); d90 = et.get("90dAgo")
        c30 = f"  {('▲' if (cur-d30)/abs(d30)*100>0 else '▼')}30d {(cur-d30)/abs(d30)*100:+.1f}%" if d30 else ""
        c90 = f"  {('▲' if (cur-d90)/abs(d90)*100>0 else '▼')}90d {(cur-d90)/abs(d90)*100:+.1f}%" if d90 else ""
        label = PERIOD_LABELS.get(period, period)
        eps_rows.append(f"  {label:<8} {fmt_eps(cur):>7}{c30}{c90}")
    if eps_rows:
        lines.append("<b>EPS 추정치</b>")
        lines.extend(eps_rows)

    # ── 매출 추정치 ──────────────────────────────────────────────────────
    re_data = current.get("revenue_estimate", {})
    rev_rows = []
    for period in PERIODS:
        rv  = re_data.get(period, {})
        avg = rv.get("avg")
        if avg is None: continue
        g   = rv.get("growth")
        g_s = f"  YoY {g*100:+.1f}%" if g else ""
        rev_rows.append(f"  {PERIOD_LABELS.get(period, period):<8} {fmt_billion(avg)}{g_s}")
    if rev_rows:
        lines.append("<b>매출 추정치</b>")
        lines.extend(rev_rows)

    # ── 추정치 수정 방향 ─────────────────────────────────────────────────
    er_data = current.get("eps_revisions", {})
    rev_dir_rows = []
    for period in PERIODS:
        rev = er_data.get(period, {}) or {}
        u7  = rev.get("up7d",0) or 0; d7  = rev.get("down7d",0) or 0
        u30 = rev.get("up30d",0) or 0; d30 = rev.get("down30d",0) or 0
        if u7+d7+u30+d30 == 0: continue
        b7  = "▲우세" if u7>d7 else "▼우세" if d7>u7 else "중립"
        b30 = "▲우세" if u30>d30 else "▼우세" if d30>u30 else "중립"
        rev_dir_rows.append(f"  {PERIOD_LABELS.get(period,period):<8}  7일 ▲{u7}/▼{d7}({b7})  30일 ▲{u30}/▼{d30}({b30})")
    if rev_dir_rows:
        lines.append("<b>수정 방향</b>")
        lines.extend(rev_dir_rows)

    # ── EPS 추이 요약 (6개월) ────────────────────────────────────────────
    sorted_h = sorted(history, key=lambda x: x["date"])
    trend_rows = []
    for period in ["0y", "+1y"]:
        tr   = analysis["eps"].get(period, {})
        vals = tr.get("sparkline_vals", [])
        if len(vals) < 2: continue
        first = vals[0]; last = vals[-1]
        p1m = fmt_pct(tr.get("pct_1m")); p3m = fmt_pct(tr.get("pct_3m")); p6m = fmt_pct(tr.get("pct_6m"))
        d_arrow = {"up": "▲", "down": "▼", "flat": "─"}.get(tr.get("direction",""), "─")
        label = PERIOD_LABELS.get(period, period)
        trend_rows.append(f"  {label:<8}  ${first:.2f}→${last:.2f}  1M:{p1m}  3M:{p3m}  6M:{p6m}  {d_arrow}{tr.get('consecutive',1)}회 연속")
    if trend_rows:
        lines.append("<b>EPS 추이 (6개월)</b>")
        lines.extend(trend_rows)

    # ── 추세꺽임 ─────────────────────────────────────────────────────────
    reversals = analysis.get("reversals", [])
    if reversals:
        lines.append("<b>추세꺽임 감지</b>")
        for rv in reversals:
            dir_str = "상향→하향" if rv["direction"] == "down" else "하향→상향"
            p_str   = f"  1M {rv['pct_1m']:+.1f}%" if rv.get("pct_1m") is not None else ""
            lines.append(f"  {rv['label']}: {dir_str} ({rv['consecutive']}회 연속){p_str}")
    elif len(sorted_h) >= REVERSAL_MIN_HISTORY:
        lines.append("추세꺽임: 없음 (현재 방향 지속)")
    else:
        lines.append(f"추세꺽임: 데이터 누적 중 ({len(sorted_h)}일/{REVERSAL_MIN_HISTORY}일)")

    return "\n".join(lines)



# ─── Git 히스토리 커밋 ────────────────────────────────────────────────────────
def git_commit_history():
    try:
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True, capture_output=True
        )
        subprocess.run(["git", "add", str(HISTORY_FILE)], check=True, capture_output=True)
        r = subprocess.run(
            ["git", "commit", "-m", f"data: estimates snapshot {date.today()}"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            subprocess.run(["git", "push"], check=True, capture_output=True)
            print("[GIT] ✓ history committed & pushed")
        else:
            print("[GIT] nothing new to commit")
    except Exception as e:
        print(f"[GIT] commit error: {e}")

# ─── 텔레그램 전송 ───────────────────────────────────────────────────────────
def send_telegram(msg: str, token: str, chat_id: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r   = requests.post(url, json={
        "chat_id":    chat_id,
        "text":       msg,
        "parse_mode": "HTML",
    }, timeout=30)
    r.raise_for_status()
    print(f"[TG] ✓ sent ({len(msg)} chars)")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    cfg      = load_config()
    token    = cfg["telegram_token"]
    chat_id  = cfg["telegram_chat_id"]
    now      = now_kst()

    print(f"[START] {now.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"[TG]    token={'✓' if token else '✗'}  chat_id={'✓' if chat_id else '✗'}")

    # 히스토리 로드 & 정리
    history = load_history()
    history = prune_history(history)

    # 헤더 메시지
    header = (
        f"📊 <b>이익추정치 모니터</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗓 {now.strftime('%Y-%m-%d (%a) %H:%M')} KST\n"
        f"📡 출처: Yahoo Finance 컨센서스\n"
        f"🔄 NVDA·MU EPS/매출 추정치 + 추세꺽임\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )

    messages = [header]

    for ticker in TICKERS:
        print(f"\n[{ticker}] 데이터 수집 중...")
        current = fetch_ticker_data(ticker)

        if current.get("errors"):
            print(f"  오류: {current['errors']}")

        # 스냅샷 기록 (오늘 기존 데이터 덮어쓰기)
        today_str    = date.today().isoformat()
        ticker_hist  = [r for r in history.get(ticker, []) if r.get("date") != today_str]
        snap         = make_snapshot(current)
        ticker_hist.append(snap)
        history[ticker] = ticker_hist

        # 추세 분석
        analysis = build_analysis(ticker, ticker_hist)

        # 메시지 생성
        msg = build_ticker_section(ticker, current, ticker_hist, analysis)
        messages.append(msg)
        print(msg)
        print()

    # 히스토리 저장 → Git 커밋
    save_history(history)
    git_commit_history()

    # 텔레그램 발송
    if token and chat_id:
        for msg in messages:
            try:
                send_telegram(msg, token, chat_id)
            except Exception as e:
                print(f"[TG] 전송 실패: {e}")
    else:
        print("[WARN] Telegram 미설정 — 콘솔 출력만")

    print(f"\n[DONE] {now_kst().strftime('%H:%M KST')}")

if __name__ == "__main__":
    main()
