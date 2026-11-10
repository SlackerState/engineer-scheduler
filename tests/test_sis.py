"""SIS 配件图片地址。运行：python -m unittest discover tests"""
import unittest
from unittest import mock

from backend.sis import Sis

# SIS 返回的图片：fileThumbnailPath 名为缩略图，实际和原图一样是 6016×4016
ROWS = [{"filePath": "gmps/photo/P1/P1-01.jpg", "fileThumbnailPath": "gmps/thumbnail/P1/P1-01.jpg"},
        {"filePath": "gmps/prod/P1/P1-02.JPG", "fileThumbnailPath": "gmps/thumbnail/P1/P1-02.JPG"}]


class PhotoUrls(unittest.TestCase):
    def photos(self):
        s = Sis(browser=None, get_settings=lambda: {})
        with mock.patch.object(Sis, "_call", return_value=[dict(r) for r in ROWS]):
            return s.photos("P1")

    def test_list_and_preview_images_are_downsized(self):
        # 批量查询一页最多 50 个配件，每个配件都显示图片；原图在 Mac 版窗口（WebKit）里按原尺寸解码，
        # 几十张就让页面卡住。所以除了全屏看原图，都要让图片服务器（腾讯云 COS）按宽度缩小后再给
        for p in self.photos():
            self.assertRegex(p["thumb"], r"\?imageMogr2/thumbnail/\d{2,3}x$")
            self.assertRegex(p["preview"], r"\?imageMogr2/thumbnail/\d{3,4}x$")
            self.assertTrue(p["thumb"].startswith(p["full"] + "?"), "缩略图应由原图按尺寸缩小")

    def test_full_is_original(self):
        p = self.photos()[0]
        self.assertEqual(p["full"], "https://gmps-cos.sany.com.cn/gmps/photo/P1/P1-01.jpg")


if __name__ == "__main__":
    unittest.main()


def repl_row(no, name, machine, direction="单向", qty="1", scope="基于主机"):
    return {"relationType": "1对1替换", "replaceReason": "其它", "groupMachineName": machine, "replaceType": scope,
            "replaceDirection": direction, "isForce": "否",
            "newPartChildrenLst": [{"newChildrenPart": no, "newChildrenName": name, "newChildrenQty": qty}]}


class Replacements(unittest.TestCase):
    def test_same_substitute_on_several_machines_is_one_entry(self):
        # SIS 按适用的主机一行行列出，同一个替代件在 5 台主机上就是 5 行，界面上合并成一条，主机放在适用范围里
        rows = [repl_row("B2", "滤芯", f"SY{i}主体", qty="2") for i in range(5)]
        rows.append(repl_row("GLQ1", "安全滤芯", "汽车起重机", direction="双向", scope="基于产品组"))
        s = Sis(browser=None, get_settings=lambda: {})
        with mock.patch.object(Sis, "_form", return_value={"data": rows, "totalElements": len(rows)}) as form:
            out = s.replacements("P1")
            s.replacements("P1")
        self.assertEqual(form.call_count, 1, "同一物料号只查一次")
        self.assertEqual([g["parts"][0]["no"] for g in out], ["B2", "GLQ1"])
        self.assertEqual(out[0]["parts"][0]["qty"], "2")
        self.assertEqual(len(out[0]["machines"]), 5)
        self.assertEqual((out[1]["direction"], out[1]["scope"], out[1]["forced"]), ("双向", ["基于产品组"], False))

    def test_no_substitutes(self):
        s = Sis(browser=None, get_settings=lambda: {})
        with mock.patch.object(Sis, "_form", return_value={"data": [], "totalElements": 0}):
            self.assertEqual(s.replacements("P1"), [])
