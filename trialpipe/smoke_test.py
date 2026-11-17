#!/usr/bin/env python3 -I
"""离线冒烟测试：不启动 HTTP，直接验证领域不变量。"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trialpipe.freezer import FreezeCriteria, SampleFreezer
from trialpipe.ledger import AppendOnlyLedger, LedgerError, SubjectView
from trialpipe.reporting import AnalysisConfig, ReportError, ReportingService

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main() -> None:
    root = tempfile.mkdtemp(prefix="trialpipe-smoke-")
    try:
        ledger = AppendOnlyLedger(os.path.join(root, "ledger.jsonl"))
        freezer = SampleFreezer(ledger, os.path.join(root, "manifests"))
        reporting = ReportingService(
            ledger, freezer, os.path.join(root, "reports"))

        # 1. 哈希链与非法事件
        try:
            ledger.append({"type": "outcome", "subject_id": "X"})
            check("缺字段事件被拒", False)
        except LedgerError:
            check("缺字段事件被拒", True)
        for i, sid in enumerate(["P1", "P2", "P3", "P4"]):
            ledger.append({"type": "enrollment", "subject_id": sid,
                           "enrolled_at": f"2026-11-01T09:0{i}:00"})
            ledger.append({"type": "assignment", "subject_id": sid,
                           "group": "T" if i % 2 == 0 else "C",
                           "assigned_at": f"2026-11-01T10:0{i}:00"})
        head1 = ledger.head_hash()
        # 篡改一行 -> 验真必须失败
        path = ledger.path
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1] + [lines[-1].replace('"C"', '"T"', 1)])
        try:
            ledger.read(verify=True)
            check("篡改台账被哈希链检出", False)
        except LedgerError:
            check("篡改台账被哈希链检出", True)
        # 还原
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        check("还原后哈希链恢复", ledger.head_hash() == head1)

        # 2. 冻结幂等且不可变
        crit = FreezeCriteria("2026-11-05T00:00:00", "ST", ("T", "C"))
        m_a = freezer.freeze(crit)
        m_b = freezer.freeze(crit)
        check("同标准重复冻结幂等", m_a == m_b and m_a["manifest_id"] == m_b["manifest_id"])
        check("冻结时 n=4", m_a["n_members"] == 4, str(m_a["group_counts"]))

        # 3. 迟到退出不影响已冻结清单
        ledger.append({"type": "withdrawal", "subject_id": "P2",
                       "withdrawn_at": "2026-11-12T08:00:00", "reason": "late"})
        m_old = freezer.load(m_a["manifest_id"])
        check("迟到退出不改旧清单",
              m_old["n_members"] == 4 and
              any(m["subject_id"] == "P2" for m in m_old["members"]))
        m2 = freezer.freeze(FreezeCriteria("2026-11-16T00:00:00", "ST", ("T", "C")))
        check("更晚 as_of 的新清单排除退出者",
              m2["n_members"] == 3 and
              all(e["reason"] == "withdrawn_by_as_of" for e in m2["exclusions"]))

        # 4. 分组修订前提校验 + 旧清单分组不变
        try:
            ledger.append({"type": "group_revision", "subject_id": "P1",
                           "old_group": "C", "new_group": "C",
                           "revised_at": "2026-11-13T09:00:00", "reason": "x"})
            check("非法修订被拒（同组）", False)
        except LedgerError:
            check("非法修订被拒（同组）", True)
        ledger.append({"type": "group_revision", "subject_id": "P1",
                       "old_group": "T", "new_group": "C",
                       "revised_at": "2026-11-13T09:00:00", "reason": "转录错误"})
        view = SubjectView(ledger.read().events)
        check("修订后当前分组生效", view.current_group("P1") == "C")
        m_old2 = freezer.load(m_a["manifest_id"])
        p1 = next(m for m in m_old2["members"] if m["subject_id"] == "P1")
        check("旧清单冻结分组保持 T", p1["group_frozen"] == "T")

        # 5. 身份纠错
        ledger.append({"type": "id_correction", "subject_id": "P4",
                       "old_subject_id": "P4", "new_subject_id": "P4-R",
                       "corrected_at": "2026-11-14T09:00:00",
                       "reason": "编号互换"})
        view = SubjectView(ledger.read().events)
        check("别名解析 P4 -> P4-R", view.current_id("P4") == "P4-R")
        # 身份纠错晚于首次冻结 as_of，旧清单身份必须保持 P4
        m_old3 = freezer.load(m_a["manifest_id"])
        ids_old = {m["subject_id"] for m in m_old3["members"]}
        check("晚到身份纠错不改旧清单身份",
              "P4" in ids_old and "P4-R" not in ids_old)
        # 同一 as_of 再冻结：身份 ID 必须一致（幂等），链头可变但文件不覆盖
        m_refreeze = freezer.freeze(
            FreezeCriteria("2026-11-05T00:00:00", "ST", ("T", "C")))
        check("同 as_of 再冻结身份幂等",
              m_refreeze["manifest_id"] == m_a["manifest_id"]
              and m_refreeze["members"] == m_old3["members"])
        try:
            ledger.append({"type": "id_correction", "subject_id": "P3",
                           "old_subject_id": "P3", "new_subject_id": "P4-R",
                           "corrected_at": "2026-11-14T10:00:00",
                           "reason": "撞 ID 测试"})
            check("撞 ID 纠错被拒", False)
        except LedgerError:
            check("撞 ID 纠错被拒", True)
        # 缺时间戳的身份纠错必须被拒
        try:
            ledger.append({"type": "id_correction", "subject_id": "P2",
                           "old_subject_id": "P2", "new_subject_id": "P2-X",
                           "reason": "缺时间戳"})
            check("缺 corrected_at 被拒", False)
        except LedgerError:
            check("缺 corrected_at 被拒", True)

        # 6. 结局分版 + 报告生命周期 + 输入还原
        for sid, val in {"P1": 1, "P2": 0, "P3": 1, "P4": 0}.items():
            ledger.append({"type": "outcome", "subject_id": sid,
                           "outcome_name": "y", "value": val,
                           "version": 1, "as_of": "2026-11-10T00:00:00"})
        cfg = AnalysisConfig("y", 1, "binary", "T", "C")
        r1 = reporting.create_report(m_a["manifest_id"], cfg, "2026-11-11T09:00:00")
        check("报告覆盖冻结 4 人（纠错后仍能取到 P4 结局）",
              r1["n_analyzed"] == 4, f"n={r1['n_analyzed']}")
        r1 = reporting.approve(r1["report_id"], "2026-11-11T10:00:00")
        check("批准成功", r1["status"] == "approved")
        # 同输入重复创建必须返回原报告，不能把已批准状态打回草稿
        r1_again = reporting.create_report(
            m_a["manifest_id"], cfg, "2026-11-11T12:00:00")
        check("重复创建不降级已批准报告",
              r1_again["report_id"] == r1["report_id"]
              and r1_again["status"] == "approved")
        try:
            reporting.approve(r1["report_id"], "2026-11-11T11:00:00")
            check("重复批准被拒", False)
        except ReportError:
            check("重复批准被拒", True)

        # v2 结局 + 同人群新报告 + 作废
        for sid, val in {"P1": 0, "P2": 0, "P3": 1, "P4": 0}.items():
            ledger.append({"type": "outcome", "subject_id": sid,
                           "outcome_name": "y", "value": val,
                           "version": 2, "as_of": "2026-11-15T00:00:00"})
        r2 = reporting.create_report(
            m_a["manifest_id"],
            AnalysisConfig("y", 2, "binary", "T", "C"), "2026-11-16T09:00:00")
        r2 = reporting.approve(r2["report_id"], "2026-11-16T10:00:00")
        reporting.supersede(r1["report_id"], r2["report_id"])
        check("旧报告标记 superseded 但仍可加载",
              reporting.load_report(r1["report_id"])["status"] == "superseded")
        snap = reporting.restore_input_snapshot(r1["report_id"])
        check("旧报告输入快照可还原且为 v1 数据",
              len(snap["analysis_rows"]) == 4
              and next(r for r in snap["analysis_rows"]
                       if r["subject_id"] == "P1")["value"] == 1)

        # 7. 中断与恢复
        r_int = reporting.create_report(
            m_a["manifest_id"],
            AnalysisConfig("y", 1, "binary", "T", "C"),
            "2026-11-16T11:00:00", simulate_interruption=True)
        check("中断报告无结果",
              r_int["status"] == "interrupted" and r_int["result"] is None)
        r_res = reporting.resume_report(r_int["report_id"], "2026-11-16T11:10:00")
        check("恢复后结果与未中断一致",
              r_res["status"] == "draft"
              and r_res["result"]["difference"]["estimate"]
              == r1["result"]["difference"]["estimate"])

        # 8. 不同人群比较拦截 + 同人群禁止跨人群作废
        r3 = reporting.create_report(
            m2["manifest_id"],
            AnalysisConfig("y", 2, "binary", "T", "C"), "2026-11-16T14:00:00")
        r3 = reporting.approve(r3["report_id"], "2026-11-16T15:00:00")
        premise = reporting.compare_reports(r2["report_id"], r3["report_id"])
        check("不同人群：直接相减被拦截",
              premise["same_frozen_population"] is False
              and premise["direct_subtraction_allowed"] is False)
        try:
            reporting.supersede(r2["report_id"], r3["report_id"])
            check("跨人群作废被拒", False)
        except ReportError:
            check("跨人群作废被拒", True)
        same = reporting.compare_reports(r1["report_id"], r2["report_id"])
        check("同人群分版比较被允许并列",
              same["same_frozen_population"] is True
              and "side_by_side_effects" in same)

        # 9. 逐样本解释覆盖全部冻结成员（含缺失者说明）
        expl = reporting.per_subject_explanation(r2["report_id"])
        check("逐样本解释人数=冻结人数", len(expl) == m_a["n_members"])
        check("解释包含统计参与方式",
              all("区间" in e["explanation"] for e in expl))

        print("\n存储目录树：")
        for dirpath, _dirs, files in os.walk(root):
            for f in sorted(files):
                print(" ", os.path.relpath(os.path.join(dirpath, f), root))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} 项失败：{FAILURES}")
        sys.exit(1)
    print("\n全部离线不变量验证通过")


if __name__ == "__main__":
    main()
