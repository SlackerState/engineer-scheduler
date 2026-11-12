"""GSC 维修调度（维修三快）。运行：python -m unittest discover tests"""
import unittest
from unittest import mock

from backend.gsc import Gsc


def warn_row(no, node, flag, text, secs, **kw):
    # flag：-1 没预警、0 已预警、1 已超时（GSC residueOverTimeStr 的前缀）
    over = flag == 1
    return {"orderNumber": no, "serviceOrderId": "3F2722D9-5FDB-4828-B71D-9341BBC9957C", "currentNodeDesc": node,
            "isWarn": 1 if flag >= 0 else 0, "isOvertime": 1 if over else 0,
            "currentNode": kw.get("code", "DEPARTURE_NODE"), "residueOverTimeStr": f"{flag},{text}", "overTime": secs if over else None,
            "residueTime": None if over else secs, "serviceOrderModeName": "直销",
            "repairMaterialOrderNos": kw.get("materials")}


COUNTS = {"totalCount": 3, "acceptance": 0, "departure": 2, "arriving": 1, "completion": 0}
ALARMS = {"acceptanceMap": {"alarm": 0, "overTime": 0}, "departureMap": {"alarm": 2, "overTime": 1},
          "arrivingMap": {"alarm": 0, "overTime": 0}, "completionMap": {"pauseRepair": 0, "alarm": 0, "overTime": 0}}


class Board(unittest.TestCase):
    def board(self, rows, settings=None):
        calls = []

        def fake(self, method, path, body=None, token=None):
            calls.append((method, path, body))
            if path.endswith("/order-status-stat"):
                return COUNTS
            if path.endswith("/alarm-overtime-stat"):
                return ALARMS
            return {"content": rows, "totalElements": str(len(rows))}

        g = Gsc(browser=None, get_settings=lambda: settings or {}, set_setting=None)
        with mock.patch.object(Gsc, "_call", fake):
            return g.board(), calls

    def test_only_direct_and_joint_venture(self):
        # 只要直销（10）和合资公司（30），不要服务经销商（20）；三个接口都要带上这个筛选
        _, calls = self.board([])
        self.assertEqual(len(calls), 3)
        for method, path, body in calls:
            if method == "POST":
                self.assertEqual(body["serviceOrderMode"], "10,30")
                self.assertEqual((body["owningBusinessUnit"], body["region"]), ("R06", "N029"))
            else:
                self.assertIn("serviceOrderMode=10%2C30", path)

    def test_overtime_first_then_warning_then_least_time_left(self):
        rows = [warn_row("A", "待出发", -1, "4天1时", 350000),
                warn_row("B", "待出发", 0, "15天3时", 1300000),
                warn_row("C", "待到达", -1, "7时18分", 26000),
                warn_row("D", "待出发", 1, "73天1时", 6310000, materials="3200067662,3200085665")]
        r, _ = self.board(rows)
        self.assertEqual([x["order_no"] for x in r["rows"]], ["D", "B", "C", "A"])
        d = r["rows"][0]
        self.assertEqual((d["over"], d["warn"], d["left"]), (True, True, "73天1时"))
        self.assertEqual(d["materials"], ["3200067662", "3200085665"])
        self.assertEqual(d["d365_id"], "3f2722d9-5fdb-4828-b71d-9341bbc9957c", "D365 id 按本地的小写存")

    def test_row_node_matches_card_by_code(self):
        # 卡片叫「待完工」，单子上的节点名是「待现场完工」，要按代码对上
        r, _ = self.board([warn_row("E", "待现场完工", -1, "3时", 10800, code="COMPLETION_NODE")])
        self.assertEqual(r["rows"][0]["node_key"], "completion")
        self.assertIn("completion", [n["key"] for n in r["nodes"]])
        # 没见过的代码按名字对：「待接收」的单
        r, _ = self.board([warn_row("F", "待接收", -1, "1时", 3600, code="ACCEPT_NODE")])
        self.assertEqual(r["rows"][0]["node_key"], "acceptance")

    def test_node_cards(self):
        r, _ = self.board([])
        dep = next(n for n in r["nodes"] if n["key"] == "departure")
        self.assertEqual((dep["name"], dep["count"], dep["warn"], dep["over"]), ("待出发", 2, 2, 1))
        self.assertEqual([n["name"] for n in r["nodes"]], ["待接收", "待出发", "待到达", "待完工"])
