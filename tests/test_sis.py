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
