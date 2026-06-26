import unittest

from app import (
    intraday_confirmation,
    normalize_symbol,
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
