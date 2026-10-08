"""Offline tests for the Tencent/Sina index fallback."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import sources  # noqa: E402


TENCENT = (
    'v_sh000001="1~上证指数~000001~3851.62~3842.19~3838.98~0~~20261008100130~9.43";'
    'v_hkHSI="100~恒生指数~HSI~24090.140~24130.500~24031.660~~2026/10/08 09:46:31~-40.360";'
    'v_ukUKX="300~富时100~UKX~10458.50~10541.69~10542.18~~2026-10-07 16:35:29~-83.19";'
    'v_pv_none_match="1";'
)
SINA = (
    'var hq_str_s_sh000001="上证指数,3851.65,9.46,0.25";'
    'var hq_str_int_hangseng="恒生指数,24130.50,0.00,0.00";'
    'var hq_str_b_UKX="富时100指数,10458.5000,-83.19,-0.79,9/26/2025,2025-09-26,2026-10-07,23:35:29";'
    'var hq_str_b_DAX="德国DAX指数,25104.3600,-344.83,-1.35,9/26/2025,2025-09-26,2026-10-07,23:30:00";'
    'var hq_str_b_SX5E="欧洲斯托克50,6180.3000,-91.94,-1.47,9/26/2025,2025-09-26,2026-10-08,00:00:00";'
    'var hq_str_int_dax="";'
)


class AsiaQuoteTests(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()
        sources.clear_asia_cache()

    def test_parsers_keep_prices_and_dates(self):
        tx = sources._parse_tencent_body(TENCENT)
        self.assertEqual(tx["sh000001"][-1], ("2026-10-08", 3851.62))
        self.assertEqual(tx["hkHSI"][-1][0], "2026-10-08")
        self.assertEqual(tx["ukUKX"][-1], ("2026-10-07", 10458.50))
        self.assertNotIn("pv_none_match", tx)
        sina = sources._parse_sina_body(SINA)
        self.assertEqual(sina["b_DAX"][-1], ("2026-10-07", 25104.36))
        self.assertAlmostEqual(sina["b_DAX"][0][1], 25104.36 - (-344.83))
        self.assertEqual(sina["b_SX5E"][-1][0], "2026-10-08")
        self.assertNotIn("int_dax", sina)

    def test_cascade_uses_tencent_then_sina(self):
        def fake_get(url, timeout=None, headers=None, **kwargs):
            if "qt.gtimg.cn" in url:
                return TENCENT
            if "hq.sinajs.cn" in url:
                return SINA
            raise RuntimeError(f"unexpected {url}")

        with mock.patch.object(sources, "_yahoo_history", side_effect=RuntimeError("yahoo")), \
                mock.patch.object(sources, "_cnbc_history", side_effect=RuntimeError("cnbc")), \
                mock.patch.object(sources, "_stooq_raw_history", side_effect=RuntimeError("stooq")), \
                mock.patch.object(sources, "_get", side_effect=fake_get):
            sh = sources.stooq_history("^shc", days=30)
            dax = sources.stooq_history("^dax", days=30)
        self.assertEqual(sh[-1][1], 3851.62)
        self.assertEqual(sources._LAST_SOURCE["^shc"][0], "tencent:sh000001")
        self.assertEqual(dax[-1][1], 25104.36)
        self.assertEqual(sources._LAST_SOURCE["^dax"][0], "sina:b_DAX")


if __name__ == "__main__":
    unittest.main()
