from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import io
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
SINA_MINLINE_URL = (
    "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20data=/"
    "CN_MinlineService.getMinlineData?symbol={symbol}"
)
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_STOCK_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
EASTMONEY_COMPANY_SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
EASTMONEY_BUSINESS_ANALYSIS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
EASTMONEY_ANNOUNCEMENT_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_SEARCH_NEWS_URL = "https://searchapi.eastmoney.com/bussiness/Web/GetSearchList"
TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_MARKET_CENTER_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

QUOTE_TTL_SECONDS = 6
INTRADAY_TTL_SECONDS = 30
DAILY_TTL_SECONDS = 60 * 60
MARKET_LIST_TTL_SECONDS = 90
FUNDAMENTAL_TTL_SECONDS = 60 * 10
COMPANY_PROFILE_TTL_SECONDS = 60 * 60 * 24 * 7
BUSINESS_ANALYSIS_TTL_SECONDS = 60 * 60 * 24
CATALYST_TTL_SECONDS = 60 * 30

DEFAULT_UNIVERSE = [
    "sh600000",
    "sh600036",
    "sh600050",
    "sh600089",
    "sh600150",
    "sh600276",
    "sh600309",
    "sh600519",
    "sh600585",
    "sh600690",
    "sh600703",
    "sh600745",
    "sh600809",
    "sh600887",
    "sh601012",
    "sh601088",
    "sh601166",
    "sh601318",
    "sh601398",
    "sh601688",
    "sh601899",
    "sh603259",
    "sh603501",
    "sh603986",
    "sz000001",
    "sz000063",
    "sz000100",
    "sz000333",
    "sz000338",
    "sz000651",
    "sz000725",
    "sz000768",
    "sz000858",
    "sz002027",
    "sz002049",
    "sz002129",
    "sz002142",
    "sz002179",
    "sz002230",
    "sz002236",
    "sz002241",
    "sz002304",
    "sz002352",
    "sz002371",
    "sz002415",
    "sz002459",
    "sz002460",
    "sz002466",
    "sz002475",
    "sz002594",
    "sz002714",
    "sz002812",
    "sz300014",
    "sz300015",
    "sz300033",
    "sz300059",
    "sz300122",
    "sz300124",
    "sz300274",
    "sz300308",
    "sz300347",
    "sz300394",
    "sz300433",
    "sz300450",
    "sz300454",
    "sz300496",
    "sz300498",
    "sz300750",
    "sz300760",
    "sz300782",
]


class MarketDataError(RuntimeError):
    pass


class TTLCache:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._values.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < time.time():
                self._values.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            self._values[key] = (time.time() + ttl_seconds, value)


cache = TTLCache()


def request_text(url: str, referer: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset()
                content_encoding = resp.headers.get("Content-Encoding", "")
            break
        except urllib.error.HTTPError as exc:
            raise MarketDataError(f"HTTP {exc.code}: {url}") from exc
        except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionResetError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            if isinstance(exc, urllib.error.URLError):
                raise MarketDataError(f"Network error: {exc.reason}") from exc
            raise MarketDataError(str(exc)) from exc
    else:
        raise MarketDataError(str(last_error))
    return decode_response_body(raw, charset, content_encoding)


def decode_response_body(raw: bytes, charset: str | None = None, content_encoding: str = "") -> str:
    if raw.startswith(b"\x1f\x8b") or "gzip" in content_encoding.lower():
        raw = gzip.decompress(raw)
    encodings = [charset] if charset else []
    encodings.extend(["utf-8", "gb18030"])
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def request_text_powershell(url: str, referer: str, timeout: float = 20.0) -> str:
    safe_url = url.replace("'", "''")
    safe_referer = referer.replace("'", "''")
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"(Invoke-WebRequest -UseBasicParsing -Uri '{safe_url}' "
        f"-Headers @{{'User-Agent'='Mozilla/5.0';'Referer'='{safe_referer}'}}).Content"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise MarketDataError(f"PowerShell request failed: {exc}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise MarketDataError(f"PowerShell request failed: {message}")
    return completed.stdout.strip()


def normalize_symbol(symbol: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z]", "", symbol).lower()
    if cleaned.startswith(("sh", "sz", "bj")) and len(cleaned) == 8:
        return cleaned
    code = re.sub(r"\D", "", cleaned)
    if len(code) != 6:
        raise ValueError(f"Invalid A-share symbol: {symbol}")
    if code.startswith(("60", "68", "90")):
        return f"sh{code}"
    if code.startswith(("00", "20", "30")):
        return f"sz{code}"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"bj{code}"
    return f"sz{code}"


def eastmoney_secid(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    code = normalized[2:]
    if normalized.startswith("sh"):
        return f"1.{code}"
    return f"0.{code}"


def eastmoney_symbol(code: str, market: int | str) -> str:
    prefix = "sh" if str(market) == "1" else "sz"
    return f"{prefix}{code}"


def parse_number(value: Any, default: float = 0.0) -> float:
    if value in (None, "-", ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_sina_quotes(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r'var hq_str_([a-z]{2}\d{6})="(.*?)";', text, re.S):
        symbol, payload = match.group(1), match.group(2)
        values = payload.split(",")
        if len(values) < 32 or not values[0]:
            continue
        try:
            open_price = float(values[1] or 0)
            prev_close = float(values[2] or 0)
            current = float(values[3] or 0)
            high = float(values[4] or 0)
            low = float(values[5] or 0)
            volume = float(values[8] or 0)
            amount = float(values[9] or 0)
        except ValueError:
            continue
        change = current - prev_close if prev_close else 0.0
        change_pct = change / prev_close * 100 if prev_close else 0.0
        rows.append(
            {
                "symbol": symbol,
                "code": symbol[2:],
                "name": values[0],
                "open": open_price,
                "prev_close": prev_close,
                "price": current,
                "high": high,
                "low": low,
                "bid": float(values[6] or 0),
                "ask": float(values[7] or 0),
                "volume": volume,
                "amount": amount,
                "change": round(change, 3),
                "change_pct": round(change_pct, 3),
                "date": values[30] if len(values) > 30 else "",
                "time": values[31] if len(values) > 31 else "",
                "source": "sina",
            }
        )
    return rows


def get_realtime_quote(symbols: list[str]) -> list[dict[str, Any]]:
    normalized = sorted({normalize_symbol(symbol) for symbol in symbols})
    if not normalized:
        return []
    cache_key = "quote:" + ",".join(normalized)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    url = SINA_QUOTE_URL.format(symbols=",".join(normalized))
    text = request_text(url, referer="https://finance.sina.com.cn/")
    quotes = parse_sina_quotes(text)
    cache.set(cache_key, quotes, QUOTE_TTL_SECONDS)
    return quotes


def parse_eastmoney_spot(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = payload.get("data") or {}
    total = int(data.get("total") or 0)
    rows: list[dict[str, Any]] = []
    for item in data.get("diff") or []:
        code = str(item.get("f12") or "")
        market = item.get("f13")
        if len(code) != 6 or market not in (0, 1, "0", "1"):
            continue
        symbol = eastmoney_symbol(code, market)
        price = parse_number(item.get("f2"))
        prev_close = parse_number(item.get("f18"))
        change = parse_number(item.get("f4"))
        change_pct = parse_number(item.get("f3"))
        rows.append(
            {
                "symbol": symbol,
                "code": code,
                "name": str(item.get("f14") or ""),
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": parse_number(item.get("f5")),
                "amount": parse_number(item.get("f6")),
                "high": parse_number(item.get("f15")),
                "low": parse_number(item.get("f16")),
                "open": parse_number(item.get("f17")),
                "prev_close": prev_close,
                "source": "eastmoney_spot",
            }
        )
    return rows, total


def parse_sina_market_rows(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in payload:
        symbol = str(item.get("symbol") or "")
        if not re.fullmatch(r"(sh|sz|bj)\d{6}", symbol):
            continue
        code = str(item.get("code") or symbol[2:])
        price = parse_number(item.get("trade"))
        prev_close = parse_number(item.get("settlement"))
        rows.append(
            {
                "symbol": symbol,
                "code": code,
                "name": str(item.get("name") or ""),
                "price": price,
                "change": parse_number(item.get("pricechange")),
                "change_pct": parse_number(item.get("changepercent")),
                "volume": parse_number(item.get("volume")),
                "amount": parse_number(item.get("amount")),
                "high": parse_number(item.get("high")),
                "low": parse_number(item.get("low")),
                "open": parse_number(item.get("open")),
                "prev_close": prev_close,
                "quote_time": str(item.get("ticktime") or ""),
                "source": "sina_market",
            }
        )
    return rows


def scaled_number(value: Any, scale: float = 100.0) -> float | None:
    if value in (None, "-", ""):
        return None
    number = parse_number(value, default=math.nan)
    if math.isnan(number):
        return None
    return number / scale


def parse_eastmoney_snapshot(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    data = payload.get("data") or {}
    if not data:
        raise MarketDataError(f"No Eastmoney snapshot returned for {symbol}")
    return {
        "symbol": normalize_symbol(symbol),
        "code": str(data.get("f57") or normalize_symbol(symbol)[2:]),
        "name": str(data.get("f58") or ""),
        "price": scaled_number(data.get("f43")),
        "open": scaled_number(data.get("f46")),
        "high": scaled_number(data.get("f44")),
        "low": scaled_number(data.get("f45")),
        "prev_close": scaled_number(data.get("f60")),
        "change_pct": scaled_number(data.get("f170")),
        "amplitude": scaled_number(data.get("f171")),
        "volume": parse_number(data.get("f47")),
        "amount": parse_number(data.get("f48")),
        "market_cap": parse_number(data.get("f116")),
        "float_market_cap": parse_number(data.get("f117")),
        "pe_dynamic": scaled_number(data.get("f162")),
        "pb": scaled_number(data.get("f167")),
        "turnover": scaled_number(data.get("f168")),
        "source": "eastmoney_snapshot",
    }


def get_stock_snapshot(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    cache_key = f"stock-snapshot:{normalized}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "secid": eastmoney_secid(normalized),
        "fields": ",".join(
            [
                "f57",
                "f58",
                "f43",
                "f44",
                "f45",
                "f46",
                "f47",
                "f48",
                "f60",
                "f116",
                "f117",
                "f162",
                "f167",
                "f168",
                "f170",
                "f171",
                "f292",
            ]
        ),
    }
    url = EASTMONEY_STOCK_URL + "?" + urllib.parse.urlencode(params)
    try:
        text = request_text(url, referer="https://quote.eastmoney.com/", timeout=8)
    except MarketDataError:
        text = request_text_powershell(url, referer="https://quote.eastmoney.com/", timeout=18)
    payload = json.loads(text)
    if payload.get("rc") != 0:
        raise MarketDataError(f"Eastmoney snapshot error {payload.get('rc')}: {normalized}")
    result = parse_eastmoney_snapshot(payload, normalized)
    cache.set(cache_key, result, FUNDAMENTAL_TTL_SECONDS)
    return result


def eastmoney_f10_code(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    prefix = "SH" if normalized.startswith("sh") else "SZ" if normalized.startswith("sz") else "BJ"
    return f"{prefix}{normalized[2:]}"


def announcement_stock_code(symbol: str) -> str:
    return normalize_symbol(symbol)[2:]


def clean_text(value: Any, max_length: int | None = None) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    text = re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()
    if max_length and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else text[:10]


def ratio_to_pct(value: Any) -> float | None:
    number = parse_number(value, default=math.nan)
    if math.isnan(number):
        return None
    if abs(number) <= 1:
        number *= 100
    return round(number, 2)


def parse_company_survey(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    basic = (payload.get("jbzl") or [{}])[0] or {}
    overview = (payload.get("gsgk") or [{}])[0] or {}
    intro = (
        overview.get("CONTENT")
        or overview.get("ORG_PROFILE")
        or basic.get("ORG_PROFILE")
        or basic.get("MAIN_BUSINESS")
        or ""
    )
    return {
        "symbol": normalize_symbol(symbol),
        "name": clean_text(basic.get("SECURITY_NAME_ABBR") or basic.get("ORG_NAME") or ""),
        "full_name": clean_text(basic.get("ORG_NAME") or ""),
        "industry": clean_text(basic.get("EM2016") or basic.get("INDUSTRYCSRC1") or ""),
        "csrc_industry": clean_text(basic.get("INDUSTRYCSRC1") or ""),
        "market": clean_text(basic.get("TRADE_MARKET") or basic.get("SECURITY_TYPE") or ""),
        "province": clean_text(basic.get("PROVINCE") or ""),
        "website": clean_text(basic.get("ORG_WEB") or ""),
        "listing_date": normalize_date(basic.get("LISTING_DATE") or basic.get("LISTINGDATE")),
        "employees": parse_number(basic.get("EMP_NUM")),
        "intro": clean_text(intro, 180),
        "source": "eastmoney_f10_company",
    }


def parse_business_analysis(payload: dict[str, Any], top: int = 5) -> dict[str, Any]:
    scope = clean_text(((payload.get("zyfw") or [{}])[0] or {}).get("BUSINESS_SCOPE"), 220)
    rows = payload.get("zygcfx") or []
    dated_rows = [row for row in rows if row.get("REPORT_DATE")]
    latest_date = max((normalize_date(row.get("REPORT_DATE")) for row in dated_rows), default="")
    current = [row for row in dated_rows if normalize_date(row.get("REPORT_DATE")) == latest_date]
    product_rows = [row for row in current if str(row.get("MAINOP_TYPE") or "1") == "1"] or current
    product_rows.sort(key=lambda row: parse_number(row.get("MBI_RATIO"), default=0), reverse=True)
    items = []
    for row in product_rows[:top]:
        items.append(
            {
                "name": clean_text(row.get("ITEM_NAME") or row.get("MAIN_BUSINESS") or "-"),
                "income": parse_number(row.get("MAIN_BUSINESS_INCOME")),
                "income_ratio_pct": ratio_to_pct(row.get("MBI_RATIO")),
                "gross_margin_pct": ratio_to_pct(row.get("GROSS_RPOFIT_RATIO")),
                "type": str(row.get("MAINOP_TYPE") or ""),
            }
        )
    return {
        "business_scope": scope,
        "report_date": latest_date,
        "items": items,
        "source": "eastmoney_f10_business",
    }


def parse_announcements(payload: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    notices = ((payload.get("data") or {}).get("list") or [])[:limit]
    rows = []
    for item in notices:
        title = clean_text(item.get("title_ch") or item.get("title") or "")
        if not title:
            continue
        columns = item.get("columns") or []
        rows.append(
            {
                "title": title,
                "date": normalize_date(item.get("notice_date") or item.get("display_time")),
                "source": "东方财富公告",
                "category": "、".join(clean_text(col.get("column_name")) for col in columns if col.get("column_name")),
                "url": f"https://data.eastmoney.com/notices/detail/{item.get('art_code')}.html" if item.get("art_code") else "",
            }
        )
    return rows


def parse_news_search(payload: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    raw_items = (
        payload.get("Data")
        or payload.get("data")
        or payload.get("result")
        or payload.get("items")
        or []
    )
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("Items") or raw_items.get("List") or raw_items.get("list") or []
    rows = []
    for item in raw_items[:limit]:
        title = clean_text(item.get("Title") or item.get("title") or item.get("Art_Title") or "")
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "date": normalize_date(item.get("ShowTime") or item.get("date") or item.get("PublishTime")),
                "source": clean_text(item.get("Source") or item.get("source") or "东方财富新闻"),
                "url": clean_text(item.get("Url") or item.get("url") or ""),
            }
        )
    return rows


def get_company_survey(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    cache_key = f"company-survey:{normalized}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    url = EASTMONEY_COMPANY_SURVEY_URL + "?" + urllib.parse.urlencode({"code": eastmoney_f10_code(normalized)})
    text = request_text(url, referer="https://emweb.securities.eastmoney.com/", timeout=10)
    result = parse_company_survey(json.loads(text), normalized)
    cache.set(cache_key, result, COMPANY_PROFILE_TTL_SECONDS)
    return result


def get_business_analysis(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    cache_key = f"business-analysis:{normalized}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    url = EASTMONEY_BUSINESS_ANALYSIS_URL + "?" + urllib.parse.urlencode({"code": eastmoney_f10_code(normalized)})
    text = request_text(url, referer="https://emweb.securities.eastmoney.com/", timeout=10)
    result = parse_business_analysis(json.loads(text))
    cache.set(cache_key, result, BUSINESS_ANALYSIS_TTL_SECONDS)
    return result


def get_stock_announcements(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    normalized = normalize_symbol(symbol)
    cache_key = f"announcements:{normalized}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "sr": "-1",
        "page_size": str(limit),
        "page_index": "1",
        "ann_type": "A",
        "client_source": "web",
        "stock_list": announcement_stock_code(normalized),
    }
    url = EASTMONEY_ANNOUNCEMENT_URL + "?" + urllib.parse.urlencode(params)
    text = request_text(url, referer="https://data.eastmoney.com/", timeout=10)
    result = parse_announcements(json.loads(text), limit=limit)
    cache.set(cache_key, result, CATALYST_TTL_SECONDS)
    return result


def get_stock_news(symbol: str, name: str, limit: int = 5) -> list[dict[str, Any]]:
    if not name:
        return []
    normalized = normalize_symbol(symbol)
    cache_key = f"stock-news:{normalized}:{limit}:{name}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "type": "401",
        "pageindex": "1",
        "pagesize": str(limit),
        "keyword": name,
        "name": "normal",
    }
    url = EASTMONEY_SEARCH_NEWS_URL + "?" + urllib.parse.urlencode(params)
    try:
        text = request_text(url, referer="https://so.eastmoney.com/", timeout=8)
        result = parse_news_search(json.loads(text), limit=limit)
    except Exception:
        result = []
    cache.set(cache_key, result, CATALYST_TTL_SECONDS)
    return result


def price_volume_stats(daily: dict[str, Any]) -> dict[str, Any]:
    points = daily.get("points") or []
    if len(points) < 2:
        return {"return_5d": None, "return_10d": None, "return_20d": None, "volume_ratio_20d": None}
    latest = points[-1]

    def ret(days: int) -> float | None:
        if len(points) <= days:
            return None
        base = points[-days - 1].get("close")
        close = latest.get("close")
        if not base or not close:
            return None
        return round((close - base) / base * 100, 2)

    recent_volumes = [float(p.get("volume") or 0) for p in points[-21:-1]]
    avg_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
    latest_volume = float(latest.get("volume") or 0)
    volume_ratio = round(latest_volume / avg_volume, 2) if avg_volume else None
    return {
        "return_5d": ret(5),
        "return_10d": ret(10),
        "return_20d": ret(20),
        "volume_ratio_20d": volume_ratio,
    }


def catalyst_strength(title: str, source: str) -> str:
    strong_words = ["业绩", "预增", "订单", "合同", "回购", "重组", "中标", "获批", "定增", "收购"]
    medium_words = ["合作", "产品", "政策", "涨价", "扩产", "产业", "景气", "板块", "概念"]
    if source == "公告" and any(word in title for word in strong_words):
        return "强"
    if any(word in title for word in strong_words + medium_words):
        return "中"
    return "弱"


def choose_catalysts(
    announcements: list[dict[str, Any]],
    news: list[dict[str, Any]],
    price_stats: dict[str, Any],
    limit: int = 3,
) -> list[dict[str, Any]]:
    catalysts: list[dict[str, Any]] = []
    for item in announcements:
        title = clean_text(item.get("title"))
        if not title:
            continue
        catalysts.append(
            {
                "title": title,
                "date": item.get("date") or "",
                "source": "公告",
                "strength": catalyst_strength(title, "公告"),
                "note": "公司公告优先级最高，可作为核验上涨线索的主要证据。",
                "url": item.get("url") or "",
            }
        )
    for item in news:
        title = clean_text(item.get("title"))
        if not title:
            continue
        catalysts.append(
            {
                "title": title,
                "date": item.get("date") or "",
                "source": item.get("source") or "新闻",
                "strength": catalyst_strength(title, "新闻"),
                "note": "新闻只作为线索，需要回到公告、财报或行业数据进一步确认。",
                "url": item.get("url") or "",
            }
        )
    if not catalysts:
        r5 = price_stats.get("return_5d")
        volume_ratio = price_stats.get("volume_ratio_20d")
        if r5 is not None and abs(r5) >= 5:
            title = f"近 5 日涨跌幅 {r5}%，最新量能约为 20 日均量 {volume_ratio or '-'} 倍"
            note = "未找到明确公告或新闻催化，更像价格量能推动，需要防止题材解释后验化。"
        else:
            title = "未找到明确公开催化"
            note = "当前公开信息不足，先按技术面和资金面线索观察。"
        catalysts.append(
            {
                "title": title,
                "date": "",
                "source": "价格量能",
                "strength": "弱",
                "note": note,
                "url": "",
            }
        )
    strength_order = {"强": 0, "中": 1, "弱": 2}
    catalysts.sort(key=lambda item: (strength_order.get(item.get("strength"), 9), 0 if item.get("source") == "公告" else 1))
    return catalysts[:limit]


def build_analyst_notes(
    snapshot: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    business: dict[str, Any] | None,
    catalysts: list[dict[str, Any]],
    price_stats: dict[str, Any],
) -> list[str]:
    fundamentals = snapshot or {}
    notes = []
    industry = (profile or {}).get("industry") or "行业信息暂缺"
    notes.append(f"公司所处行业：{industry}；上涨线索需和主营业务、公告或行业景气交叉验证。")
    if business and business.get("items"):
        top = business["items"][0]
        ratio = top.get("income_ratio_pct")
        notes.append(f"最近一期收入占比最高的是“{top.get('name')}”{f'，约 {ratio}%' if ratio is not None else ''}，催化若不在核心业务上，持续性要打折。")
    else:
        notes.append("主营构成暂缺，暂时无法判断催化是否真正落在核心收入来源。")
    pe = fundamentals.get("pe_dynamic")
    pb = fundamentals.get("pb")
    turnover = fundamentals.get("turnover")
    if pe and pe > 80:
        notes.append("动态市盈率偏高，若催化没有业绩兑现，高估值会放大回撤风险。")
    elif pe:
        notes.append(f"动态市盈率约 {pe}，仍需和行业估值、利润增速一起看。")
    if pb and pb > 8:
        notes.append("市净率偏高，说明市场已给较高预期，追高容错率较低。")
    if turnover and turnover > 12:
        notes.append("换手率偏高，短线资金博弈较强，催化失效时波动可能放大。")
    if catalysts and catalysts[0].get("source") == "公告":
        notes.append("当前最强线索来自公告，优先核对公告正文里的时间、金额和执行条件。")
    elif catalysts and catalysts[0].get("source") == "价格量能":
        notes.append("暂未发现明确公开催化，本轮更偏技术面或资金推动，不宜把原因解释得过满。")
    r20 = price_stats.get("return_20d")
    if r20 is not None and r20 > 25:
        notes.append(f"近 20 日涨幅约 {r20}%，已不属于低位启动，需重点看回撤承受位。")
    return notes[:6]


def build_fundamental_story(
    symbol: str,
    snapshot: dict[str, Any] | None,
    daily: dict[str, Any],
    profile: dict[str, Any] | None = None,
    business: dict[str, Any] | None = None,
    announcements: list[dict[str, Any]] | None = None,
    news: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fundamentals = snapshot or {}
    profile = profile or {}
    business = business or {}
    price_stats = price_volume_stats(daily)
    catalysts = choose_catalysts(announcements or [], news or [], price_stats)
    profile_items = [
        {"label": "公司全称", "value": profile.get("full_name") or fundamentals.get("name") or normalize_symbol(symbol).upper()},
        {"label": "所属行业", "value": profile.get("industry") or "-"},
        {"label": "上市地点", "value": profile.get("market") or "-"},
        {"label": "上市时间", "value": profile.get("listing_date") or "-"},
        {"label": "员工人数", "value": profile.get("employees")},
        {"label": "官网", "value": profile.get("website") or "-"},
    ]
    valuation_items = [
        {"label": "总市值", "value": fundamentals.get("market_cap")},
        {"label": "流通市值", "value": fundamentals.get("float_market_cap")},
        {"label": "动态市盈率", "value": fundamentals.get("pe_dynamic")},
        {"label": "市净率", "value": fundamentals.get("pb")},
        {"label": "换手率", "value": fundamentals.get("turnover"), "suffix": "%"},
        {"label": "成交额", "value": fundamentals.get("amount")},
    ]
    return {
        "profile": {
            "title": "公司画像",
            "summary": profile.get("intro") or "公司简介暂缺，先以交易快照和公开公告做基础核验。",
            "items": profile_items,
            "source": profile.get("source") or "",
        },
        "business_mix": {
            "title": "收入结构",
            "summary": f"报告期 {business.get('report_date')}" if business.get("report_date") else "主营构成暂缺",
            "business_scope": business.get("business_scope") or "",
            "items": business.get("items") or [],
            "source": business.get("source") or "",
        },
        "catalysts": catalysts,
        "price_stats": price_stats,
        "analyst_notes": build_analyst_notes(snapshot, profile, business, catalysts, price_stats),
        "valuation_items": valuation_items,
    }


def get_market_spot_universe(page_size: int = 80) -> dict[str, Any]:
    cache_key = f"market-spot-sina:{page_size}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    all_rows: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "page": str(page),
            "num": str(page_size),
            "sort": "symbol",
            "asc": "1",
            "node": "hs_a",
            "symbol": "",
            "_s_r_a": "init",
        }
        url = SINA_MARKET_CENTER_URL + "?" + urllib.parse.urlencode(params)
        text = request_text(url, referer="https://finance.sina.com.cn/", timeout=12)
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise MarketDataError("Sina market list returned an unexpected payload")
        rows = parse_sina_market_rows(payload)
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        page += 1
        if page > 120:
            break
    result = {
        "rows": all_rows,
        "total": len(all_rows),
        "source": "sina_market",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cache.set(cache_key, result, MARKET_LIST_TTL_SECONDS)
    return result


def market_prefilter(rows: list[dict[str, Any]], deep_limit: int, min_amount: float) -> list[dict[str, Any]]:
    filtered = []
    for row in rows:
        name = row.get("name", "")
        price = row.get("price") or 0
        amount = row.get("amount") or 0
        if not row["symbol"].startswith(("sh", "sz")):
            continue
        if "ST" in name.upper() or "退" in name or name.startswith(("N", "C")):
            continue
        if price <= 2 or amount < min_amount:
            continue
        filtered.append(row)
    by_amount = sorted(filtered, key=lambda row: row.get("amount") or 0, reverse=True)[: max(40, deep_limit // 2)]
    by_momentum = sorted(
        filtered,
        key=lambda row: ((row.get("change_pct") or 0), (row.get("amount") or 0)),
        reverse=True,
    )[: max(40, deep_limit // 2)]
    combined: dict[str, dict[str, Any]] = {row["symbol"]: row for row in by_amount + by_momentum}
    return list(combined.values())[:deep_limit]


def parse_eastmoney_klines(payload: dict[str, Any], symbol: str, period: int) -> dict[str, Any]:
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    points: list[dict[str, Any]] = []
    for line in klines:
        cells = str(line).split(",")
        if len(cells) < 7:
            continue
        try:
            points.append(
                {
                    "time": cells[0],
                    "open": float(cells[1]),
                    "close": float(cells[2]),
                    "high": float(cells[3]),
                    "low": float(cells[4]),
                    "volume": float(cells[5]),
                    "amount": float(cells[6]),
                    "amplitude": float(cells[7]) if len(cells) > 7 and cells[7] else 0.0,
                    "change_pct": float(cells[8]) if len(cells) > 8 and cells[8] else 0.0,
                    "change": float(cells[9]) if len(cells) > 9 and cells[9] else 0.0,
                    "turnover": float(cells[10]) if len(cells) > 10 and cells[10] else 0.0,
                }
            )
        except ValueError:
            continue
    if not points:
        raise MarketDataError(f"No kline data returned for {symbol}")
    return {
        "symbol": normalize_symbol(symbol),
        "code": data.get("code") or normalize_symbol(symbol)[2:],
        "name": data.get("name") or "",
        "period": period,
        "source": "eastmoney",
        "latest_time": points[-1]["time"],
        "points": points,
    }


def get_eastmoney_kline(symbol: str, period: int, limit: int = 320) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if period not in {1, 5, 15, 30, 60, 101}:
        raise ValueError("period must be one of 1, 5, 15, 30, 60, 101")
    cache_key = f"em:{normalized}:{period}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {
        "secid": eastmoney_secid(normalized),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": str(period),
        "fqt": "1",
        "beg": "0",
        "end": "20500101",
    }
    url = EASTMONEY_KLINE_URL + "?" + urllib.parse.urlencode(params)
    text = request_text(url, referer="https://quote.eastmoney.com/")
    payload = json.loads(text)
    if payload.get("rc") != 0:
        raise MarketDataError(f"Eastmoney error {payload.get('rc')}: {normalized}")
    result = parse_eastmoney_klines(payload, normalized, period)
    if limit > 0:
        result["points"] = result["points"][-limit:]
    ttl = DAILY_TTL_SECONDS if period == 101 else INTRADAY_TTL_SECONDS
    cache.set(cache_key, result, ttl)
    return result


def parse_sina_minline(text: str, symbol: str) -> dict[str, Any]:
    match = re.search(r"var data=\((\[.*\])\);?", text, re.S)
    if not match:
        raise MarketDataError(f"No Sina minline payload for {symbol}")
    raw_points = json.loads(match.group(1))
    points = []
    for row in raw_points:
        try:
            price = float(row["p"])
            volume = float(row["v"])
            avg_price = float(row.get("avg_p") or 0)
        except (ValueError, KeyError):
            continue
        points.append(
            {
                "time": row["m"],
                "price": price,
                "volume": volume,
                "avg_price": avg_price,
            }
        )
    return {
        "symbol": normalize_symbol(symbol),
        "source": "sina_minline",
        "latest_time": points[-1]["time"] if points else "",
        "points": points,
    }


def get_sina_minline(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    cache_key = f"sina-minline:{normalized}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    url = SINA_MINLINE_URL.format(symbol=normalized)
    text = request_text(url, referer="https://finance.sina.com.cn/")
    result = parse_sina_minline(text, normalized)
    cache.set(cache_key, result, INTRADAY_TTL_SECONDS)
    return result


def get_intraday_kline(symbol: str, period: int = 1) -> dict[str, Any]:
    try:
        return get_eastmoney_kline(symbol, period)
    except Exception:
        fallback = get_sina_minline(symbol)
        fallback["warning"] = "东方财富分钟K线暂不可用，已切换新浪分时线"
        return fallback


def parse_tencent_daily(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    stock_data = (payload.get("data") or {}).get(normalized)
    if not stock_data:
        raise MarketDataError(f"No Tencent daily data returned for {normalized}")
    raw_rows = stock_data.get("qfqday") or stock_data.get("day") or []
    points: list[dict[str, Any]] = []
    for row in raw_rows:
        if len(row) < 6:
            continue
        try:
            open_price = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5])
        except ValueError:
            continue
        points.append(
            {
                "time": row[0],
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": close * volume * 100,
                "amplitude": (high - low) / close * 100 if close else 0.0,
                "change_pct": 0.0,
                "change": 0.0,
                "turnover": 0.0,
            }
        )
    for idx, point in enumerate(points):
        if idx == 0:
            continue
        prev_close = points[idx - 1]["close"]
        point["change"] = point["close"] - prev_close
        point["change_pct"] = point["change"] / prev_close * 100 if prev_close else 0.0
    if not points:
        raise MarketDataError(f"Empty Tencent daily data for {normalized}")
    quote = stock_data.get("qt", {}).get(normalized, [])
    name = quote[1] if len(quote) > 1 else ""
    return {
        "symbol": normalized,
        "code": normalized[2:],
        "name": name,
        "period": "daily",
        "source": "tencent",
        "latest_time": points[-1]["time"],
        "points": points,
    }


def get_tencent_daily_history(symbol: str, days: int = 220) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    count = max(80, min(360, days))
    cache_key = f"tencent-daily:{normalized}:{count}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    params = {"param": f"{normalized},day,,,{count},qfq"}
    url = TENCENT_DAILY_URL + "?" + urllib.parse.urlencode(params)
    text = request_text(url, referer="https://gu.qq.com/")
    payload = json.loads(text)
    if payload.get("code") != 0:
        raise MarketDataError(f"Tencent error {payload.get('code')}: {normalized}")
    result = parse_tencent_daily(payload, normalized)
    result["points"] = result["points"][-days:]
    cache.set(cache_key, result, DAILY_TTL_SECONDS)
    return result


def get_daily_history(symbol: str, days: int = 220) -> dict[str, Any]:
    return get_tencent_daily_history(symbol, days)


def sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for idx, value in enumerate(values):
        total += value
        if idx >= window:
            total -= values[idx - window]
        if idx + 1 >= window:
            out.append(total / window)
        else:
            out.append(None)
    return out


def enrich_daily(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes = [point["close"] for point in points]
    volumes = [point["volume"] for point in points]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    vma20 = sma(volumes, 20)
    enriched: list[dict[str, Any]] = []
    for idx, point in enumerate(points):
        item = dict(point)
        item["ma5"] = ma5[idx]
        item["ma10"] = ma10[idx]
        item["ma20"] = ma20[idx]
        item["ma60"] = ma60[idx]
        item["vma20"] = vma20[idx]
        enriched.append(item)
    return enriched


def score_rejuvenation(daily_points: list[dict[str, Any]]) -> dict[str, Any]:
    points = enrich_daily(daily_points)
    if len(points) < 80:
        return {
            "status": "data_gap",
            "score": 0,
            "reason": "日线不足 80 根，暂不参与筛选",
            "tags": ["数据不足"],
        }
    latest = points[-1]
    closes = [p["close"] for p in points]
    recent = points[-45:]
    base_low = min(p["low"] for p in points[-80:-20])
    peak = max(recent[:-3], key=lambda p: p["high"])
    pullback_low = min(p["low"] for p in recent[recent.index(peak) + 1 :] or recent[-10:])
    first_wave_pct = (peak["high"] - base_low) / base_low * 100 if base_low else 0.0
    pullback_pct = (peak["high"] - pullback_low) / peak["high"] * 100 if peak["high"] else 0.0
    ma20 = latest.get("ma20") or 0.0
    ma60 = latest.get("ma60") or 0.0
    ma20_prev = points[-6].get("ma20") or ma20
    volume_ratio = (
        latest["volume"] / latest["vma20"]
        if latest.get("vma20") and latest.get("vma20") > 0
        else 0.0
    )
    above_ma20 = latest["close"] >= ma20 if ma20 else False
    ma20_support = pullback_low >= ma20 * 0.985 if ma20 else False
    trend_ok = bool(ma20 and ma60 and latest["close"] > ma20 > ma60 and ma20 >= ma20_prev)
    wave_ok = first_wave_pct >= 15
    pullback_ok = 3 <= pullback_pct <= 14
    reclaim_ok = latest.get("ma5") and latest.get("ma10") and latest["close"] >= latest["ma5"] >= latest["ma10"] * 0.995
    volume_ok = 0.85 <= volume_ratio <= 2.8

    score = 0
    score += 24 if wave_ok else max(0, min(18, first_wave_pct))
    score += 20 if pullback_ok else 8
    score += 20 if ma20_support and above_ma20 else 0
    score += 18 if trend_ok else 6 if above_ma20 else 0
    score += 12 if reclaim_ok else 0
    score += 6 if volume_ok else 0
    score = int(max(0, min(100, round(score))))

    if score >= 78 and trend_ok and ma20_support and reclaim_ok:
        status = "buy_watch"
    elif score >= 60 and above_ma20:
        status = "watch"
    else:
        status = "avoid"

    stop_candidates = [pullback_low, ma20 * 0.98 if ma20 else pullback_low]
    stop_price = min(c for c in stop_candidates if c > 0)
    observe_price = max(latest.get("ma5") or latest["close"], latest.get("ma10") or latest["close"])
    distance_to_ma20 = (latest["close"] - ma20) / ma20 * 100 if ma20 else 0.0
    tags = []
    if wave_ok:
        tags.append("一波上涨")
    if ma20_support:
        tags.append("回踩不破MA20")
    if trend_ok:
        tags.append("趋势向上")
    if reclaim_ok:
        tags.append("站回短均线")
    if volume_ok:
        tags.append("量能温和")
    if not tags:
        tags.append("形态不足")
    reason = "；".join(tags)
    if status == "avoid":
        reason += "；等待更清晰的二波确认"

    return {
        "status": status,
        "score": score,
        "reason": reason,
        "tags": tags,
        "first_wave_pct": round(first_wave_pct, 2),
        "pullback_pct": round(pullback_pct, 2),
        "distance_to_ma20": round(distance_to_ma20, 2),
        "volume_ratio": round(volume_ratio, 2),
        "observe_price": round(observe_price, 2),
        "stop_price": round(stop_price, 2),
        "latest_date": latest["time"],
        "ma": {
            "ma5": round(latest["ma5"], 3) if latest.get("ma5") else None,
            "ma10": round(latest["ma10"], 3) if latest.get("ma10") else None,
            "ma20": round(latest["ma20"], 3) if latest.get("ma20") else None,
            "ma60": round(latest["ma60"], 3) if latest.get("ma60") else None,
        },
    }


def intraday_confirmation(daily_signal: dict[str, Any], intraday: dict[str, Any], quote: dict[str, Any] | None) -> dict[str, Any]:
    points = intraday.get("points") or []
    if not points:
        return {"status": "unknown", "reason": "暂无分时数据"}
    last = points[-1]
    if "close" in last:
        last_price = float(last["close"])
        last_volume = float(last.get("volume") or 0)
        recent_volumes = [float(p.get("volume") or 0) for p in points[-20:]]
    else:
        last_price = float(last["price"])
        last_volume = float(last.get("volume") or 0)
        recent_volumes = [float(p.get("volume") or 0) for p in points[-20:]]
    avg_recent_volume = sum(recent_volumes[:-1]) / max(1, len(recent_volumes[:-1]))
    volume_expansion = last_volume > avg_recent_volume * 1.3 if avg_recent_volume else False
    stop_price = daily_signal.get("stop_price") or 0
    observe_price = daily_signal.get("observe_price") or 0
    above_observe = last_price >= observe_price if observe_price else False
    stop_alive = last_price > stop_price if stop_price else True
    source_gap = ""
    if quote and quote.get("price"):
        diff = abs(float(quote["price"]) - last_price) / max(last_price, 0.01) * 100
        if diff > 1.5:
            source_gap = f"行情源价差 {diff:.2f}%"
    if stop_alive and above_observe and volume_expansion:
        status = "confirmed"
        reason = "盘中站上观察价且分钟量能放大"
    elif stop_alive and above_observe:
        status = "pending_volume"
        reason = "价格站上观察价，量能仍需确认"
    elif stop_alive:
        status = "waiting"
        reason = "未跌破失效位，等待站上观察价"
    else:
        status = "invalid"
        reason = "盘中跌破失效位"
    if source_gap:
        reason += f"；{source_gap}"
    return {
        "status": status,
        "reason": reason,
        "last_price": round(last_price, 3),
        "volume_expansion": volume_expansion,
        "last_time": last.get("time"),
    }


def build_detail_analysis(
    daily: dict[str, Any],
    signal: dict[str, Any],
    intraday: dict[str, Any],
    confirmation: dict[str, Any],
    quote: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    fundamental_story: dict[str, Any] | None = None,
) -> dict[str, Any]:
    points = daily.get("points") or []
    latest = points[-1] if points else {}
    previous = points[-2] if len(points) >= 2 else {}
    close = latest.get("close")
    previous_close = previous.get("close")
    day_change_pct = (
        (close - previous_close) / previous_close * 100
        if close and previous_close
        else quote.get("change_pct")
        if quote
        else None
    )
    ma = signal.get("ma") or {}
    fundamentals = snapshot or {}
    technical_items = [
        {
            "label": "日线信号",
            "value": f"{signal.get('score', 0)} 分 / {signal.get('reason', '-')}",
        },
        {
            "label": "一波与回踩",
            "value": f"一波涨幅 {signal.get('first_wave_pct', 0)}%，回踩 {signal.get('pullback_pct', 0)}%",
        },
        {
            "label": "均线位置",
            "value": (
                f"收盘 {round(close, 2) if close else '-'}，MA5 {ma.get('ma5') or '-'}，"
                f"MA10 {ma.get('ma10') or '-'}，MA20 {ma.get('ma20') or '-'}，MA60 {ma.get('ma60') or '-'}"
            ),
        },
        {
            "label": "量能",
            "value": f"最新量能约为 20 日均量的 {signal.get('volume_ratio', 0)} 倍",
        },
    ]
    fundamental_items = [
        {"label": "总市值", "value": fundamentals.get("market_cap")},
        {"label": "流通市值", "value": fundamentals.get("float_market_cap")},
        {"label": "动态市盈率", "value": fundamentals.get("pe_dynamic")},
        {"label": "市净率", "value": fundamentals.get("pb")},
        {"label": "换手率", "value": fundamentals.get("turnover"), "suffix": "%"},
        {"label": "成交额", "value": fundamentals.get("amount")},
    ]
    story_symbol = fundamentals.get("symbol") or "sh000001"
    story = fundamental_story or build_fundamental_story(story_symbol, snapshot, daily)
    positive = []
    if signal.get("status") == "buy_watch":
        positive.append("日线形态满足买入观察条件，适合继续看盘中确认。")
    elif signal.get("status") == "watch":
        positive.append("日线形态接近策略条件，但还需要价格或量能进一步确认。")
    else:
        positive.append("当前日线形态没有满足核心买入观察条件。")
    if signal.get("distance_to_ma20") is not None:
        positive.append(f"收盘价相对 MA20 偏离 {signal.get('distance_to_ma20')}%，可用来判断追高风险。")
    if confirmation.get("reason"):
        positive.append(f"盘中确认：{confirmation.get('reason')}")
    if day_change_pct is not None:
        positive.append(f"最近交易日涨跌幅约 {round(day_change_pct, 2)}%。")

    risks = []
    stop_price = signal.get("stop_price")
    observe_price = signal.get("observe_price")
    if stop_price:
        risks.append(f"策略失效参考位 {stop_price}，跌破后回踩结构不再有效。")
    if observe_price:
        risks.append(f"观察价 {observe_price}，未有效站上前不宜把分钟波动当成二波启动。")
    if fundamentals.get("pe_dynamic") and fundamentals["pe_dynamic"] > 80:
        risks.append("动态市盈率较高，基本面估值容错率偏低。")
    if fundamentals.get("turnover") and fundamentals["turnover"] > 12:
        risks.append("换手率偏高，短线资金博弈较强，波动风险更大。")
    if intraday.get("warning"):
        risks.append("分钟 K 线主源不可用，当前使用备用分时数据，OHLC 细节可能不完整。")
    if not snapshot:
        risks.append("基本面快照暂不可用，需要结合财报、行业和公告进一步确认。")

    return {
        "technical": {
            "title": "技术面",
            "summary": signal.get("reason", "-"),
            "items": technical_items,
            "positives": positive,
        },
        "fundamental": {
            "title": "基本面与催化",
            "summary": "公司画像、主营结构和公开催化的证据型核验，不把线索直接当确定因果。",
            "items": story.get("valuation_items") or fundamental_items,
            "source": fundamentals.get("source") if fundamentals else "",
            "profile": story.get("profile") or {},
            "business_mix": story.get("business_mix") or {},
            "catalysts": story.get("catalysts") or [],
            "price_stats": story.get("price_stats") or {},
            "analyst_notes": story.get("analyst_notes") or [],
        },
        "risk": {
            "title": "风险与验证",
            "items": risks,
        },
    }


def screen_symbols(symbols: list[str]) -> list[dict[str, Any]]:
    normalized = [normalize_symbol(symbol) for symbol in symbols]
    quote_map = {q["symbol"]: q for q in get_realtime_quote(normalized)}
    rows = []
    for symbol in normalized:
        try:
            daily = get_daily_history(symbol, 220)
            signal = score_rejuvenation(daily["points"])
            quote = quote_map.get(symbol)
            if quote and ("ST" in quote.get("name", "").upper() or quote.get("price") == 0):
                signal["status"] = "avoid"
                signal["score"] = min(signal.get("score", 0), 30)
                signal["reason"] = "ST/停牌或无有效现价，剔除"
            rows.append(
                {
                    "symbol": symbol,
                    "code": symbol[2:],
                    "name": (quote or {}).get("name") or daily.get("name") or symbol,
                    "price": (quote or {}).get("price"),
                    "change_pct": (quote or {}).get("change_pct"),
                    "amount": (quote or {}).get("amount"),
                    "quote_time": " ".join(filter(None, [(quote or {}).get("date"), (quote or {}).get("time")])),
                    "signal": signal,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "symbol": symbol,
                    "code": symbol[2:],
                    "name": symbol,
                    "signal": {
                        "status": "error",
                        "score": 0,
                        "reason": str(exc),
                        "tags": ["数据错误"],
                    },
                }
            )
    rows.sort(key=lambda row: row["signal"].get("score", 0), reverse=True)
    return rows


def score_spot_candidate(spot: dict[str, Any]) -> dict[str, Any]:
    symbol = spot["symbol"]
    daily = get_daily_history(symbol, 220)
    signal = score_rejuvenation(daily["points"])
    return {
        "symbol": symbol,
        "code": spot["code"],
        "name": spot.get("name") or daily.get("name") or symbol,
        "price": spot.get("price"),
        "change_pct": spot.get("change_pct"),
        "amount": spot.get("amount"),
        "quote_time": signal.get("latest_date"),
        "signal": signal,
    }


def recommend_market(top: int = 10, deep_limit: int = 160, min_amount: float = 100_000_000) -> dict[str, Any]:
    market = get_market_spot_universe()
    candidates = market_prefilter(market["rows"], deep_limit=deep_limit, min_amount=min_amount)
    scored: list[dict[str, Any]] = []
    errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(score_spot_candidate, candidate): candidate for candidate in candidates}
        for future in concurrent.futures.as_completed(future_map):
            candidate = future_map[future]
            try:
                scored.append(future.result())
            except Exception:
                errors += 1
                scored.append(
                    {
                        "symbol": candidate["symbol"],
                        "code": candidate["code"],
                        "name": candidate.get("name") or candidate["symbol"],
                        "price": candidate.get("price"),
                        "change_pct": candidate.get("change_pct"),
                        "amount": candidate.get("amount"),
                        "signal": {
                            "status": "error",
                            "score": 0,
                            "reason": "深度评分数据暂不可用",
                            "tags": ["数据错误"],
                        },
                    }
                )
    qualified = [row for row in scored if row["signal"].get("status") in {"buy_watch", "watch"}]
    qualified.sort(
        key=lambda row: (
            row["signal"].get("score", 0),
            row.get("amount") or 0,
            row.get("change_pct") or 0,
        ),
        reverse=True,
    )
    fallback = sorted(scored, key=lambda row: row["signal"].get("score", 0), reverse=True)
    rows = (qualified or fallback)[:top]
    latest_trade_date = ""
    for row in rows:
        latest_trade_date = row["signal"].get("latest_date") or latest_trade_date
        if latest_trade_date:
            break
    return {
        "rows": rows,
        "universe_size": market["total"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "仅供研究观察，不构成投资建议。",
        "scan_meta": {
            "mode": "market",
            "market_total": market["total"],
            "market_loaded": len(market["rows"]),
            "prefiltered": len(candidates),
            "deep_scanned": len(scored),
            "qualified": len(qualified),
            "returned": len(rows),
            "errors": errors,
            "latest_trade_date": latest_trade_date,
            "min_amount": min_amount,
        },
    }


def trading_day_index(points: list[dict[str, Any]], lookback_days: int) -> int:
    if not points:
        raise MarketDataError("No daily points for backtest")
    latest_date = datetime.strptime(points[-1]["time"], "%Y-%m-%d").date()
    target_date = latest_date - timedelta(days=lookback_days)
    index = 0
    for idx, point in enumerate(points):
        point_date = datetime.strptime(point["time"], "%Y-%m-%d").date()
        if point_date <= target_date:
            index = idx
        else:
            break
    return index


def score_backtest_candidate(candidate: dict[str, Any], lookback_days: int) -> dict[str, Any]:
    symbol = candidate["symbol"]
    daily = get_daily_history(symbol, 300)
    points = daily["points"]
    idx = trading_day_index(points, lookback_days)
    if idx < 79:
        raise MarketDataError("回测日前日线不足 80 根")
    history_at_then = points[: idx + 1]
    signal = score_rejuvenation(history_at_then)
    entry = history_at_then[-1]
    latest = points[-1]
    after_points = points[idx + 1 :] or [latest]
    entry_price = float(entry["close"])
    latest_price = float(latest["close"])
    max_high = max(float(point["high"]) for point in after_points)
    min_low = min(float(point["low"]) for point in after_points)
    drawdown_pct = (min_low - entry_price) / entry_price * 100 if entry_price else 0
    return {
        "symbol": symbol,
        "code": candidate.get("code") or symbol[2:],
        "name": candidate.get("name") or daily.get("name") or symbol,
        "as_of_date": entry["time"],
        "latest_date": latest["time"],
        "entry_price": round(entry_price, 3),
        "latest_price": round(latest_price, 3),
        "return_pct": round((latest_price - entry_price) / entry_price * 100, 2) if entry_price else 0,
        "max_gain_pct": round((max_high - entry_price) / entry_price * 100, 2) if entry_price else 0,
        "max_drawdown_pct": round(min(0, drawdown_pct), 2),
        "signal": signal,
    }


def summarize_backtest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"avg_return_pct": 0, "win_rate_pct": 0, "best_return_pct": 0, "worst_return_pct": 0}
    returns = [row.get("return_pct", 0) for row in rows]
    wins = [value for value in returns if value > 0]
    return {
        "avg_return_pct": round(sum(returns) / len(returns), 2),
        "win_rate_pct": round(len(wins) / len(returns) * 100, 2),
        "best_return_pct": round(max(returns), 2),
        "worst_return_pct": round(min(returns), 2),
    }


def backtest_strategy(
    symbols: list[str] | None = None,
    top: int = 10,
    deep_limit: int = 160,
    lookback_days: int = 30,
    min_amount: float = 100_000_000,
) -> dict[str, Any]:
    market_total = len(symbols or [])
    market_loaded = market_total
    if symbols:
        candidates = [{"symbol": normalize_symbol(symbol), "code": normalize_symbol(symbol)[2:], "name": normalize_symbol(symbol)} for symbol in symbols]
        mode = "custom"
    else:
        market = get_market_spot_universe()
        candidates = market_prefilter(market["rows"], deep_limit=deep_limit, min_amount=min_amount)
        market_total = market["total"]
        market_loaded = len(market["rows"])
        mode = "market_fast"

    scored: list[dict[str, Any]] = []
    errors = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(score_backtest_candidate, candidate, lookback_days): candidate for candidate in candidates}
        for future in concurrent.futures.as_completed(future_map):
            try:
                scored.append(future.result())
            except Exception:
                errors += 1

    qualified = [row for row in scored if row["signal"].get("status") in {"buy_watch", "watch"}]
    ranked_source = qualified or scored
    ranked_source.sort(
        key=lambda row: (
            row["signal"].get("score", 0),
            row.get("max_gain_pct", 0),
            row.get("return_pct", 0),
        ),
        reverse=True,
    )
    rows = ranked_source[:top]
    as_of_date = rows[0]["as_of_date"] if rows else ""
    latest_date = rows[0]["latest_date"] if rows else ""
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return {
        "rows": rows,
        "summary": summarize_backtest(rows),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disclaimer": "历史回测仅供研究，不代表未来收益，不构成投资建议。",
        "backtest_meta": {
            "mode": mode,
            "lookback_days": lookback_days,
            "as_of_date": as_of_date,
            "latest_date": latest_date,
            "market_total": market_total,
            "market_loaded": market_loaded,
            "prefiltered": len(candidates),
            "deep_scanned": len(scored),
            "qualified": len(qualified),
            "returned": len(rows),
            "errors": errors,
            "min_amount": min_amount,
            "entry_rule": "回测日收盘价",
            "exit_rule": "最新交易日收盘价",
        },
    }


def json_response(handler: SimpleHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: SimpleHTTPRequestHandler, exc: Exception, status: int = 500) -> None:
    json_response(handler, {"error": str(exc), "type": exc.__class__.__name__}, status=status)


class StockSiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def handle_api(self, parsed: urllib.parse.ParseResult) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/quote":
                symbols = parse_symbols_param(params)
                json_response(self, {"quotes": get_realtime_quote(symbols), "cache_ttl": QUOTE_TTL_SECONDS})
            elif parsed.path == "/api/intraday":
                symbol = one_param(params, "symbol", "sh600000")
                period = int(one_param(params, "period", "1"))
                json_response(self, get_intraday_kline(symbol, period))
            elif parsed.path == "/api/daily":
                symbol = one_param(params, "symbol", "sh600000")
                days = int(one_param(params, "days", "220"))
                daily = get_daily_history(symbol, days)
                daily["points"] = enrich_daily(daily["points"])
                daily["signal"] = score_rejuvenation(daily["points"])
                json_response(self, daily)
            elif parsed.path == "/api/screen":
                symbols = parse_symbols_param(params)
                limit = int(one_param(params, "limit", "10"))
                if not symbols:
                    deep_limit = int(one_param(params, "deep_limit", "160"))
                    min_amount = float(one_param(params, "min_amount", "100000000"))
                    json_response(
                        self,
                        recommend_market(
                            top=max(1, min(limit, 20)),
                            deep_limit=max(40, min(deep_limit, 360)),
                            min_amount=min_amount,
                        ),
                    )
                else:
                    rows = screen_symbols(symbols[: max(1, min(limit, 100))])
                    latest_trade_date = next((row["signal"].get("latest_date") for row in rows if row["signal"].get("latest_date")), "")
                    json_response(
                        self,
                        {
                            "rows": rows,
                            "universe_size": len(symbols),
                            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "disclaimer": "仅供研究观察，不构成投资建议。",
                            "scan_meta": {
                                "mode": "custom",
                                "market_total": len(symbols),
                                "market_loaded": len(symbols),
                                "prefiltered": len(symbols),
                                "deep_scanned": len(rows),
                                "qualified": len([row for row in rows if row["signal"].get("status") in {"buy_watch", "watch"}]),
                                "returned": len(rows),
                                "errors": len([row for row in rows if row["signal"].get("status") == "error"]),
                                "latest_trade_date": latest_trade_date,
                            },
                        },
                    )
            elif parsed.path == "/api/backtest":
                symbols = parse_symbols_param(params)
                limit = int(one_param(params, "limit", "10"))
                deep_limit = int(one_param(params, "deep_limit", "160"))
                lookback_days = int(one_param(params, "lookback_days", "30"))
                min_amount = float(one_param(params, "min_amount", "100000000"))
                json_response(
                    self,
                    backtest_strategy(
                        symbols=symbols,
                        top=max(1, min(limit, 20)),
                        deep_limit=max(40, min(deep_limit, 360)),
                        lookback_days=max(5, min(lookback_days, 180)),
                        min_amount=min_amount,
                    ),
                )
            elif parsed.path == "/api/detail":
                symbol = normalize_symbol(one_param(params, "symbol", "sh600000"))
                period = int(one_param(params, "period", "1"))
                quotes = get_realtime_quote([symbol])
                daily = get_daily_history(symbol, 220)
                daily["points"] = enrich_daily(daily["points"])
                signal = score_rejuvenation(daily["points"])
                intraday = get_intraday_kline(symbol, period)
                quote = quotes[0] if quotes else None
                snapshot = None
                warning_parts = []
                try:
                    snapshot = get_stock_snapshot(symbol)
                except Exception as exc:
                    warning_parts.append(f"估值快照：{exc}")
                profile = None
                business = None
                announcements: list[dict[str, Any]] = []
                news: list[dict[str, Any]] = []
                try:
                    profile = get_company_survey(symbol)
                except Exception as exc:
                    warning_parts.append(f"公司画像：{exc}")
                try:
                    business = get_business_analysis(symbol)
                except Exception as exc:
                    warning_parts.append(f"主营构成：{exc}")
                try:
                    announcements = get_stock_announcements(symbol)
                except Exception as exc:
                    warning_parts.append(f"公告：{exc}")
                try:
                    news_name = (profile or {}).get("name") or (snapshot or {}).get("name") or daily.get("name") or ""
                    news = get_stock_news(symbol, news_name)
                except Exception as exc:
                    warning_parts.append(f"新闻：{exc}")
                fundamental_story = build_fundamental_story(
                    symbol,
                    snapshot,
                    daily,
                    profile=profile,
                    business=business,
                    announcements=announcements,
                    news=news,
                )
                confirmation = intraday_confirmation(signal, intraday, quote)
                json_response(
                    self,
                    {
                        "symbol": symbol,
                        "quote": quote,
                        "daily": daily,
                        "intraday": intraday,
                        "fundamental": snapshot,
                        "fundamental_story": fundamental_story,
                        "fundamental_warning": "；".join(warning_parts),
                        "signal": signal,
                        "confirmation": confirmation,
                        "analysis": build_detail_analysis(
                            daily,
                            signal,
                            intraday,
                            confirmation,
                            quote,
                            snapshot,
                            fundamental_story,
                        ),
                    },
                )
            elif parsed.path == "/api/universe":
                json_response(self, {"symbols": DEFAULT_UNIVERSE})
            else:
                error_response(self, FileNotFoundError(parsed.path), status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            error_response(self, exc, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            error_response(self, exc, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_symbols_param(params: dict[str, list[str]]) -> list[str]:
    raw = one_param(params, "symbols", "")
    if not raw:
        return []
    reader = csv.reader(io.StringIO(raw.replace("\n", ",")))
    values = next(reader, [])
    return [normalize_symbol(value) for value in values if value.strip()]


def one_param(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    return values[0] if values and values[0] != "" else default


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share real-time kline stock screener")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    if not STATIC_DIR.exists():
        raise SystemExit("static directory is missing")
    server = ThreadingHTTPServer((args.host, args.port), StockSiteHandler)
    print(f"Serving stock screener at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
