# -*- coding: utf-8 -*-
"""Tests for the anomaly stop: objective item (4)'s "出现异常标记当天停手".

Checks both halves:
  * marker detection (must fire on the real notice, must NOT fire on normal pages)
  * the stop behaviour (flag -> decide() says wait even with budget available)

The flag file is removed afterwards so a test run cannot block real work.

    .venv\\Scripts\\python.exe -X utf8 test_anomaly.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"[OK  ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}: got={got!r} want={want!r}")


def main() -> int:
    import anomaly
    from schedule_resume import decide

    print("=" * 70)
    print("anomaly stop")
    print("=" * 70)

    # ---- detection ------------------------------------------------------
    real_notice = (
        "目前发现有同学存在软件刷课行为，系统已提醒有不良记录。这部分同学学习记录"
        "已全部清空，请抓紧时间重新开始学习，否则期末成绩一律作为不及格处理，即下学期"
        "重修。请其他同学引以为戒，若再有此情况，将参照学生条例，以作舞弊论处。")
    normal_pages = [
        "回到课程 光波的衍射 上一节 下一节 目录 讨论 笔记 完成条件 观看时长需≥总时长的90%",
        "1.1.1 国家安全的内涵 任务点 待完成 视频 章节测验",
        "第1章 第一学期 理论专题 1.1 总论 1.1.1 认识课程 1.1.2 我的大学如何度过",
        "",
    ]

    hits = anomaly.scan(real_notice)
    check("真实通知能被识别", len(hits) >= 3, True)
    check("识别出「软件刷课」", "软件刷课" in hits, True)
    check("识别出「学习记录已全部清空」", "学习记录已全部清空" in hits, True)

    false_positives = [p[:20] for p in normal_pages if anomaly.scan(p)]
    check("正常页面不误报", false_positives, [])

    # ---- stop behaviour -------------------------------------------------
    today = time.strftime("%Y-%m-%d")
    check("无标记时额度够就该跑", decide(today, 0, 60, 10, today), "run")

    anomaly.raise_flag(["软件刷课"], where="test")
    try:
        check("有标记时即使额度够也不跑",
              decide(today, 0, 60, 10, today), "wait")
        check("blocks_today 为真", anomaly.blocks_today(), True)
        check("is_set 能读回", bool(anomaly.is_set()), True)
    finally:
        anomaly.clear()

    check("清除后恢复可跑", decide(today, 0, 60, 10, today), "run")
    check("清除后 blocks_today 为假", anomaly.blocks_today(), False)

    print("=" * 70)
    print(f"通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
