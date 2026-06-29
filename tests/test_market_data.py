import gzip
import unittest

from app import (
    build_fundamental_story,
    choose_catalysts,
    decode_response_body,
    intraday_confirmation,
    normalize_symbol,
    parse_business_analysis,
    parse_company_survey,
    parse_eastmoney_snapshot,
    parse_eastmoney_spot,
    parse_eastmoney_klines,
    parse_sina_minline,
    parse_sina_market_rows,
    parse_sina_quotes,
    parse_tencent_daily,
    score_rejuvenation,
    summarize_backtest,
    trading_day_index,
)


class MarketDataParsingTests(unittest.TestCase):
    def test_decode_response_body_handles_gzip_without_header_charset(self):
        raw = gzip.compress('{"ok": true, "name": "药明康德"}'.encode("utf-8"))
        text = decode_response_body(raw, charset=None, content_encoding="")
        self.assertIn("药明康德", text)

    def test_normalize_symbol(self):
        self.assertEqual(normalize_symbol("600000"), "sh600000")
        self.assertEqual(normalize_symbol("000001"), "sz000001")
        self.assertEqual(normalize_symbol("SH600519"), "sh600519")

    def test_parse_sina_quotes(self):
        text = (
            'var hq_str_sh600000="浦发银行,9.200,9.240,9.090,9.250,9.070,'
            '9.080,9.090,83656385,763280627.000,466070,9.080,557600,'
            '9.070,630100,9.060,1034000,9.050,107400,9.040,708653,'
            '9.090,105200,9.100,210800,9.110,153700,9.120,311200,'
            '9.130,2026-06-18,15:00:02,00,";'
        )
        rows = parse_sina_quotes(text)
        self.assertEqual(rows[0]["symbol"], "sh600000")
        self.assertEqual(rows[0]["name"], "浦发银行")
        self.assertAlmostEqual(rows[0]["price"], 9.09)
        self.assertAlmostEqual(rows[0]["change_pct"], -1.623, places=3)

    def test_parse_eastmoney_klines(self):
        payload = {
            "rc": 0,
            "data": {
                "code": "600000",
                "market": 1,
                "name": "浦发银行",
                "klines": [
                    "2026-06-18 09:31,9.20,9.21,9.23,9.20,9096,8374545.00,0.32,-0.32,-0.03,0.00"
                ],
            },
        }
        parsed = parse_eastmoney_klines(payload, "sh600000", 1)
        self.assertEqual(parsed["latest_time"], "2026-06-18 09:31")
        self.assertEqual(parsed["points"][0]["open"], 9.2)
        self.assertEqual(parsed["points"][0]["close"], 9.21)

    def test_parse_eastmoney_spot(self):
        payload = {
            "rc": 0,
            "data": {
                "total": 2,
                "diff": [
                    {"f12": "600000", "f13": 1, "f14": "浦发银行", "f2": 9.09, "f3": -1.62, "f4": -0.15, "f5": 836563, "f6": 763280627, "f15": 9.25, "f16": 9.07, "f17": 9.2, "f18": 9.24},
                    {"f12": "000001", "f13": 0, "f14": "平安银行", "f2": 10.52, "f3": -2.41, "f4": -0.26, "f5": 1426893, "f6": 1511009564, "f15": 10.77, "f16": 10.52, "f17": 10.74, "f18": 10.78},
                ],
            },
        }
        rows, total = parse_eastmoney_spot(payload)
        self.assertEqual(total, 2)
        self.assertEqual(rows[0]["symbol"], "sh600000")
        self.assertEqual(rows[1]["symbol"], "sz000001")
        self.assertEqual(rows[0]["amount"], 763280627)

    def test_parse_eastmoney_snapshot(self):
        payload = {
            "rc": 0,
            "data": {
                "f57": "600667",
                "f58": "太极实业",
                "f43": 2092,
                "f48": 7410143554.0,
                "f116": 43755062363.76,
                "f117": 43755062363.76,
                "f162": 8471,
                "f167": 500,
                "f168": 1767,
                "f170": 999,
            },
        }
        parsed = parse_eastmoney_snapshot(payload, "sh600667")
        self.assertEqual(parsed["symbol"], "sh600667")
        self.assertEqual(parsed["name"], "太极实业")
        self.assertAlmostEqual(parsed["price"], 20.92)
        self.assertAlmostEqual(parsed["pe_dynamic"], 84.71)
        self.assertAlmostEqual(parsed["pb"], 5.0)
        self.assertAlmostEqual(parsed["turnover"], 17.67)

    def test_parse_sina_minline(self):
        text = 'var data=([{"m":"09:31:00","v":"1520500","p":"9.19","avg_p":"9.2"}]);'
        parsed = parse_sina_minline(text, "sh600000")
        self.assertEqual(parsed["points"][0]["time"], "09:31:00")
        self.assertEqual(parsed["points"][0]["price"], 9.19)

    def test_parse_sina_market_rows(self):
        rows = parse_sina_market_rows(
            [
                {
                    "symbol": "sh600000",
                    "code": "600000",
                    "name": "浦发银行",
                    "trade": "9.090",
                    "pricechange": -0.15,
                    "changepercent": -1.623,
                    "settlement": "9.240",
                    "open": "9.200",
                    "high": "9.250",
                    "low": "9.070",
                    "volume": 83656385,
                    "amount": 763280627,
                    "ticktime": "15:00:02",
                }
            ]
        )
        self.assertEqual(rows[0]["symbol"], "sh600000")
        self.assertEqual(rows[0]["name"], "浦发银行")
        self.assertEqual(rows[0]["price"], 9.09)

    def test_parse_tencent_daily(self):
        payload = {
            "code": 0,
            "data": {
                "sh600000": {
                    "qfqday": [
                        ["2026-06-17", "9.480", "9.240", "9.550", "9.220", "771904.000"],
                        ["2026-06-18", "9.200", "9.090", "9.250", "9.070", "836564.000"],
                    ],
                    "qt": {"sh600000": ["1", "浦发银行"]},
                }
            },
        }
        parsed = parse_tencent_daily(payload, "sh600000")
        self.assertEqual(parsed["source"], "tencent")
        self.assertEqual(parsed["name"], "浦发银行")
        self.assertEqual(parsed["points"][-1]["close"], 9.09)


class StrategyTests(unittest.TestCase):
    def test_parse_company_survey_profile(self):
        payload = {
            "jbzl": [
                {
                    "SECURITY_NAME_ABBR": "药明康德",
                    "ORG_NAME": "无锡药明康德新药开发股份有限公司",
                    "EM2016": "医药生物-医疗服务-CXO",
                    "INDUSTRYCSRC1": "研究和试验发展",
                    "TRADE_MARKET": "上海证券交易所",
                    "ORG_WEB": "www.wuxiapptec.com.cn",
                    "PROVINCE": "江苏",
                    "EMP_NUM": 33834,
                    "LISTING_DATE": "2018-05-08 00:00:00",
                }
            ],
            "gsgk": [{"CONTENT": "公司提供小分子药物发现、研发及生产服务。"}],
        }
        profile = parse_company_survey(payload, "sh603259")
        self.assertEqual(profile["name"], "药明康德")
        self.assertEqual(profile["industry"], "医药生物-医疗服务-CXO")
        self.assertIn("小分子药物", profile["intro"])

    def test_parse_business_analysis_uses_latest_report_and_top_mix(self):
        payload = {
            "zyfw": [{"BUSINESS_SCOPE": "药物研发服务；医药技术转让。"}],
            "zygcfx": [
                {
                    "REPORT_DATE": "2024-12-31 00:00:00",
                    "MAINOP_TYPE": "1",
                    "ITEM_NAME": "旧业务",
                    "MAIN_BUSINESS_INCOME": 100,
                    "MBI_RATIO": 0.9,
                    "GROSS_RPOFIT_RATIO": 0.2,
                },
                {
                    "REPORT_DATE": "2025-12-31 00:00:00",
                    "MAINOP_TYPE": "1",
                    "ITEM_NAME": "化学业务",
                    "MAIN_BUSINESS_INCOME": 36000000000,
                    "MBI_RATIO": 0.8022,
                    "GROSS_RPOFIT_RATIO": 0.5189,
                },
                {
                    "REPORT_DATE": "2025-12-31 00:00:00",
                    "MAINOP_TYPE": "1",
                    "ITEM_NAME": "测试业务",
                    "MAIN_BUSINESS_INCOME": 4000000000,
                    "MBI_RATIO": 0.0889,
                    "GROSS_RPOFIT_RATIO": 0.2925,
                },
            ],
        }
        business = parse_business_analysis(payload)
        self.assertEqual(business["report_date"], "2025-12-31")
        self.assertEqual(business["items"][0]["name"], "化学业务")
        self.assertAlmostEqual(business["items"][0]["income_ratio_pct"], 80.22)
        self.assertIn("药物研发", business["business_scope"])

    def test_choose_catalysts_prefers_announcements_then_news_then_price(self):
        catalysts = choose_catalysts(
            announcements=[
                {"title": "药明康德: 关于股份回购进展的公告", "date": "2026-06-26"}
            ],
            news=[{"title": "CXO板块走强", "date": "2026-06-25", "source": "东方财富"}],
            price_stats={"return_5d": 12.3, "volume_ratio_20d": 1.8},
        )
        self.assertEqual(catalysts[0]["source"], "公告")
        self.assertEqual(catalysts[0]["strength"], "强")
        self.assertIn("股份回购", catalysts[0]["title"])
        self.assertLessEqual(len(catalysts), 3)

    def test_build_fundamental_story_returns_stable_sections_without_sources(self):
        daily = {
            "points": [
                {"time": f"2026-06-{i:02d}", "close": 10 + i, "volume": 1000 + i * 10}
                for i in range(1, 22)
            ]
        }
        story = build_fundamental_story(
            "sh600000",
            {"name": "浦发银行", "pe_dynamic": 6.2, "pb": 0.5, "turnover": 1.2},
            daily,
            profile=None,
            business=None,
            announcements=[],
            news=[],
        )
        self.assertEqual(story["profile"]["title"], "公司画像")
        self.assertEqual(story["business_mix"]["title"], "收入结构")
        self.assertEqual(story["catalysts"][0]["source"], "价格量能")
        self.assertTrue(story["analyst_notes"])

    def test_rejuvenation_score_detects_setup(self):
        points = []
        price = 10.0
        for i in range(90):
            if i < 40:
                price += 0.03
            elif i < 60:
                price += 0.18
            elif i < 75:
                price -= 0.08
            else:
                price += 0.07
            points.append(
                {
                    "time": f"2026-03-{(i % 28) + 1:02d}",
                    "open": price - 0.05,
                    "close": price,
                    "high": price + 0.12,
                    "low": price - 0.12,
                    "volume": 100000 + i * 900,
                    "amount": price * (100000 + i * 900),
                }
            )
        signal = score_rejuvenation(points)
        self.assertGreaterEqual(signal["score"], 55)
        self.assertIn(signal["status"], {"buy_watch", "watch", "avoid"})
        self.assertIn("stop_price", signal)

    def test_intraday_confirmation_flags_invalid(self):
        daily_signal = {"stop_price": 9.0, "observe_price": 10.0}
        intraday = {"points": [{"time": "10:00", "open": 9.2, "close": 8.9, "high": 9.2, "low": 8.8, "volume": 100}]}
        result = intraday_confirmation(daily_signal, intraday, {"price": 8.9})
        self.assertEqual(result["status"], "invalid")

    def test_trading_day_index_uses_nearest_prior_date(self):
        points = [
            {"time": "2026-05-15"},
            {"time": "2026-05-16"},
            {"time": "2026-05-19"},
            {"time": "2026-06-18"},
        ]
        self.assertEqual(trading_day_index(points, 30), 2)

    def test_summarize_backtest(self):
        summary = summarize_backtest([{"return_pct": 10}, {"return_pct": -5}, {"return_pct": 0}])
        self.assertEqual(summary["avg_return_pct"], 1.67)
        self.assertEqual(summary["win_rate_pct"], 33.33)


if __name__ == "__main__":
    unittest.main()
