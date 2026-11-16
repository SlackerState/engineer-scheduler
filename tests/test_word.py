"""电子工单（服务订单 Word）。运行：python -m unittest discover tests"""
import base64
import unittest
from unittest import mock

from backend.d365 import D365, WORD_JS


class ServiceOrderWord(unittest.TestCase):
    def d365(self, result):
        browser = mock.Mock()
        browser.eval_d365.return_value = result
        return D365(browser, lambda: {"d365_url": "https://x.crm4.dynamics.com"}), browser

    def test_returns_file_generated_in_d365_page(self):
        d, browser = self.d365({"name": "2611013263.docx", "b64": base64.b64encode(b"PK-docx").decode()})
        name, data = d.service_order_word("{86368B18-059F-490D-959D-42DB8E3A7634}")
        self.assertEqual((name, data), ("2611013263.docx", b"PK-docx"))
        base, js, arg = browser.eval_d365.call_args[0]
        self.assertIs(js, WORD_JS)
        self.assertEqual(arg, ["86368b18-059f-490d-959d-42db8e3a7634"], "D365 id 去掉花括号、转小写")

    def test_error_from_page_is_raised(self):
        d, _ = self.d365({"error": "D365 里这单没有电子工单模板"})
        with self.assertRaisesRegex(RuntimeError, "没有电子工单模板"):
            d.service_order_word("abc")

    def test_uses_d365_own_template_action(self):
        # 和 D365 表单「服务完工 → 电子工单」一样：同一个动作、同一个 ActionName、D365 自己的脚本库
        self.assertIn("ms_GenerateFileTemplateData", WORD_JS)
        self.assertIn("ServiceOrderTemplate", WORD_JS)
        self.assertIn("/WebResources/mcs_/CommonCore/Scripts/", WORD_JS)
