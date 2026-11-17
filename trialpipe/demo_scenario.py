#!/usr/bin/env python3 -I
"""端到端演示驱动：从空目录构建虚构实验数据管道。

覆盖场景：
  1. 入组 + 分组接入（12 人，治疗 T / 对照 C，二分类 + 连续两个结局）
  2. 首次冻结样本 M1（结局尚未成熟，冻结不含任何结局）
  3. 结局 v1 成熟 -> 报告 R1 草稿 -> 批准
  4. 迟到退出：冻结后才到达，证明 M1 人群不变；as-of 更晚的 M2 才排除
  5. 分组记录修订（带 reason，旧值前提校验），M1 的冻结分组不变
  6. 身份纠错（带 reason，别名链可追溯）
  7. 结局 v2 分版补录 -> 同人群新报告 R2 批准 -> R1 变 superseded 但可还原
  8. 计算中断 -> interrupted -> resume 复算结果一致
  9. 重建一份新人群报告 R3，与 R2 比较：不同人群，直接相减被拦截
所有数据均为虚构，仅做区间计算，不训练模型，不作因果断言。
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = {8101: "http://127.0.0.1:8101",
        8102: "http://127.0.0.1:8102",
        8103: "http://127.0.0.1:8103"}


def call(port: int, method: str, path: str, body: dict | None = None) -> dict:
    url = BASE[port] + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {payload}") from exc


def banner(title: str) -> None:
    print("\n" + "=" * 74)
    print(" " + title)
    print("=" * 74)


def show(label: str, obj, limit: int | None = None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    if limit:
        lines = text.splitlines()
        text = "\n".join(lines[:limit]) + (
            f"\n... ({len(lines) - limit} 行省略)" if len(lines) > limit else "")
    print(f"-- {label} --")
    print(text)


def main() -> None:
    # 前置保护：台账必须为空，否则拒绝重跑（重复入组会被台账拒绝）
    existing = call(8101, "GET", "/events")
    if existing["event_count"] != 0:
        raise SystemExit(
            "台账非空（%d 条事件）。演示只能从空目录运行：\n"
            "  1) 停止服务：pkill -f run_services.py\n"
            "  2) 清空数据：rm -rf data\n"
            "  3) 执行 ./run_demo.sh" % existing["event_count"])
    # 健康检查
    for port in (8101, 8102, 8103):
        assert call(port, "GET", "/health")["status"] == "ok"
    print("三服务健康检查通过（空台账，从空目录开始）")

    banner("步骤 1  入组与分组接入（虚构研究 STUDY-DEMO，12 名受试者）")
    # 6 治疗 / 6 对照；时间戳保持字典序可比
    plan = [
        ("S01", "T"), ("S02", "C"), ("S03", "T"), ("S04", "C"),
        ("S05", "T"), ("S06", "C"), ("S07", "T"), ("S08", "C"),
        ("S09", "T"), ("S10", "C"), ("S11", "T"), ("S12", "C"),
    ]
    events = []
    for i, (sid, grp) in enumerate(plan):
        events.append({
            "type": "enrollment", "subject_id": sid,
            "enrolled_at": f"2026-11-01T09:{10 + i:02d}:00",
            "study_id": "STUDY-DEMO",
        })
        events.append({
            "type": "assignment", "subject_id": sid, "group": grp,
            "assigned_at": f"2026-11-01T10:{10 + i:02d}:00",
            "method": "fictional_randomization",
        })
    call(8101, "POST", "/events/batch", {"events": events})
    head_after_enroll = call(8101, "GET", "/verify")
    show("台账验真", head_after_enroll)

    banner("步骤 2  首次冻结 M1：as_of=2026-11-05（结局尚未成熟）")
    m1 = call(8102, "POST", "/freeze", {
        "as_of": "2026-11-05T00:00:00",
        "study_id": "STUDY-DEMO",
        "groups": ["T", "C"],
    })
    print(f"manifest_id = {m1['manifest_id']}  n={m1['n_members']}  "
          f"分组计数={m1['group_counts']}  排除={len(m1['exclusions'])} 人")
    print("冻结清单不含任何结局字段：",
          "value" not in json.dumps(m1), "（True 表示确实没有）")

    banner("步骤 3  结局 v1 成熟并接入（二分类缓解 + 连续评分）")
    # 虚构：T 组缓解 5/6，C 组缓解 2/6；连续评分 T 整体更高
    binary_v1 = {"S01": 1, "S03": 1, "S05": 1, "S07": 1, "S09": 1, "S11": 0,
                 "S02": 0, "S04": 1, "S06": 0, "S08": 0, "S10": 1, "S12": 0}
    cont_v1 = {"S01": 74, "S03": 78, "S05": 71, "S07": 80, "S09": 76, "S11": 69,
               "S02": 66, "S04": 70, "S06": 64, "S08": 68, "S10": 62, "S12": 67}
    outcome_events = []
    for sid, val in binary_v1.items():
        outcome_events.append({
            "type": "outcome", "subject_id": sid,
            "outcome_name": "remission", "value": val,
            "version": 1, "as_of": "2026-11-10T00:00:00",
        })
    for sid, val in cont_v1.items():
        outcome_events.append({
            "type": "outcome", "subject_id": sid,
            "outcome_name": "score", "value": val,
            "version": 1, "as_of": "2026-11-10T00:00:00",
        })
    call(8101, "POST", "/events/batch", {"events": outcome_events})

    r1 = call(8103, "POST", "/reports", {
        "manifest_id": m1["manifest_id"],
        "created_at": "2026-11-11T09:00:00",
        "config": {"outcome_name": "remission", "outcome_version": 1,
                   "outcome_type": "binary", "treatment": "T",
                   "control": "C", "alpha": 0.05},
    })
    show("R1 二分类报告（draft）", r1["result"])
    r1 = call(8103, "POST", f"/reports/{r1['report_id']}/approve",
              {"approved_at": "2026-11-11T10:00:00", "approver": "lead-stat"})
    print(f"R1 已批准：{r1['report_id']} 状态={r1['status']}")

    banner("步骤 4  迟到退出：S10 在冻结之后退出（2026-11-12）")
    call(8101, "POST", "/events", {
        "type": "withdrawal", "subject_id": "S10",
        "withdrawn_at": "2026-11-12T08:00:00",
        "reason": "失访（虚构演示：退出晚于 M1 冻结时点）",
    })
    m1_reloaded = call(8102, "GET", f"/manifests/{m1['manifest_id']}")
    still_in = [m["subject_id"] for m in m1_reloaded["members"] if m["subject_id"] == "S10"]
    print(f"M1 仍含 S10：{bool(still_in)}（后补事件无法改变已冻结人群）")
    print(f"M1 哈希保持：{m1_reloaded['manifest_hash'] == m1['manifest_hash']}")

    banner("步骤 5  分组记录修订：S07 原始分组录入错误，T -> C（带原因）")
    call(8101, "POST", "/events", {
        "type": "group_revision", "subject_id": "S07",
        "old_group": "T", "new_group": "C",
        "revised_at": "2026-11-13T09:00:00",
        "reason": "源系统核对发现随机化编号转录错误（虚构演示）",
    })
    m1_again = call(8102, "GET", f"/manifests/{m1['manifest_id']}")
    s07_frozen = next(m for m in m1_again["members"] if m["subject_id"] == "S07")
    print(f"M1 中 S07 冻结分组仍为：{s07_frozen['group_frozen']}（历史不可变）")
    subj = call(8101, "GET", "/subjects/S07")
    print(f"台账当前视图 S07 分组为：{subj['current_group']}（修订生效于新查询）")
    # 错误前提的修订必须被拒绝
    try:
        call(8101, "POST", "/events", {
            "type": "group_revision", "subject_id": "S07",
            "old_group": "T", "new_group": "T",
            "revised_at": "2026-11-13T10:00:00", "reason": "无效修订测试",
        })
        raise AssertionError("新旧组相同的修订应当被拒绝")
    except RuntimeError as exc:
        print("无效修订被拦截：", str(exc)[:90], "...")

    banner("步骤 6  身份纠错：S12 的 ID 录入错误 -> S12-R（带原因，别名可追溯）")
    call(8101, "POST", "/events", {
        "type": "id_correction", "subject_id": "S12",
        "old_subject_id": "S12", "new_subject_id": "S12-R",
        "corrected_at": "2026-11-13T14:00:00",
        "reason": "两名同姓受试者编号互换，按登记表纠错（虚构演示）",
    })
    subj12 = call(8101, "GET", "/subjects/S12")
    print(f"旧 ID S12 解析到当前 ID：{subj12['current_id']}，"
          f"分组={subj12['current_group']}，历史事件数={len(subj12['events'])}")

    banner("步骤 7  结局 v2 分版补录（数据清理后的成熟版本），同人群新报告 R2")
    # v2：S04 复核后缓解判定由 1 改 0；S12-R 连续评分更正为 73
    v2_events = [
        {"type": "outcome", "subject_id": sid, "outcome_name": "remission",
         "value": (0 if sid == "S04" else val), "version": 2,
         "as_of": "2026-11-15T00:00:00"}
        for sid, val in binary_v1.items()
    ]
    for sid, val in cont_v1.items():
        v2_events.append({
            "type": "outcome", "subject_id": sid, "outcome_name": "score",
            "value": (73 if sid == "S12" else val), "version": 2,
            "as_of": "2026-11-15T00:00:00",
        })
    call(8101, "POST", "/events/batch", {"events": v2_events})

    r2 = call(8103, "POST", "/reports", {
        "manifest_id": m1["manifest_id"],
        "created_at": "2026-11-16T09:00:00",
        "config": {"outcome_name": "remission", "outcome_version": 2,
                   "outcome_type": "binary", "treatment": "T",
                   "control": "C", "alpha": 0.05},
    })
    r2 = call(8103, "POST", f"/reports/{r2['report_id']}/approve",
              {"approved_at": "2026-11-16T10:00:00", "approver": "lead-stat"})
    call(8103, "POST", f"/reports/{r1['report_id']}/supersede",
          {"new_report_id": r2["report_id"]})
    r1_now = call(8103, "GET", f"/reports/{r1['report_id']}")
    print(f"R1 状态={r1_now['status']}，被 {r1_now['superseded_by']} 取代；文件仍在")
    show("R2（v2 结局，已批准）差值结果", r2["result"]["difference"])

    # 旧报告还原原始输入
    r1_inputs = call(8103, "GET", f"/reports/{r1['report_id']}/inputs")
    s04_v1 = next(r for r in r1_inputs["analysis_rows"] if r["subject_id"] == "S04")
    print(f"还原 R1 原始输入：S04 的 v1 缓解值={s04_v1['value']}，"
          f"输入快照哈希={r1_inputs['manifest_id'] and r1_now['input_snapshot_hash'][:12]}...")

    banner("步骤 8  计算中断演示：连续结局报告创建时模拟进程中断")
    r_int = call(8103, "POST", "/reports", {
        "manifest_id": m1["manifest_id"],
        "created_at": "2026-11-16T11:00:00",
        "simulate_interruption": True,
        "config": {"outcome_name": "score", "outcome_version": 1,
                   "outcome_type": "continuous", "treatment": "T",
                   "control": "C", "alpha": 0.05, "bootstrap": True,
                   "bootstrap_reps": 1000},
    })
    print(f"中断报告状态={r_int['status']}，result 是否为空：{r_int['result'] is None}")
    print("中断说明：", r_int["interruption"]["note"])
    # 未恢复前批准必须失败
    try:
        call(8103, "POST", f"/reports/{r_int['report_id']}/approve",
             {"approved_at": "2026-11-16T11:05:00"})
        raise AssertionError("中断报告不应能被批准")
    except RuntimeError as exc:
        print("中断态批准被拦截：", str(exc)[:80], "...")
    r_resumed = call(8103, "POST",
                     f"/reports/{r_int['report_id']}/resume",
                     {"resumed_at": "2026-11-16T11:10:00"})
    show("恢复后 Welch 结果（含确定性 bootstrap 核对）",
         r_resumed["result"]["difference"])
    r_resumed = call(8103, "POST",
                     f"/reports/{r_resumed['report_id']}/approve",
                     {"approved_at": "2026-11-16T11:15:00"})
    print(f"恢复报告已批准：{r_resumed['status']}")

    banner("步骤 9  重建新人群 M2（as_of=2026-11-16，迟到退出生效、修订/纠错生效）")
    m2 = call(8102, "POST", "/freeze", {
        "as_of": "2026-11-16T00:00:00",
        "study_id": "STUDY-DEMO",
        "groups": ["T", "C"],
    })
    show("M2 排除清单", m2["exclusions"])
    print(f"M1 n={m1['n_members']} 分组={m1['group_counts']} -> "
          f"M2 n={m2['n_members']} 分组={m2['group_counts']}")
    r3 = call(8103, "POST", "/reports", {
        "manifest_id": m2["manifest_id"],
        "created_at": "2026-11-16T14:00:00",
        "config": {"outcome_name": "remission", "outcome_version": 2,
                   "outcome_type": "binary", "treatment": "T",
                   "control": "C", "alpha": 0.05},
    })
    r3 = call(8103, "POST", f"/reports/{r3['report_id']}/approve",
              {"approved_at": "2026-11-16T15:00:00", "approver": "lead-stat"})
    print(f"新人群报告 R3 已批准：{r3['report_id']}")

    banner("步骤 10  比较前提展示：同人群分版 vs 不同人群（禁止直接相减）")
    same = call(8103, "POST", "/compare",
                {"report_id_a": r1_now["report_id"],
                 "report_id_b": r2["report_id"]})
    show("R1(v1) vs R2(v2)：同一冻结人群", {
        "same_frozen_population": same["same_frozen_population"],
        "comparison_kind": same["comparison_kind"],
        "side_by_side_effects": same.get("side_by_side_effects"),
    })
    diff_pop = call(8103, "POST", "/compare",
                    {"report_id_a": r2["report_id"],
                     "report_id_b": r3["report_id"]})
    show("R2(M1 人群) vs R3(M2 人群)：不同人群", diff_pop)
    assert diff_pop["direct_subtraction_allowed"] is False

    banner("步骤 11  逐样本解释（R2 抽样 3 人 + 哈希链最终验真）")
    explain = call(8103, "GET", f"/reports/{r2['report_id']}/explain")
    for item in explain["explanations"][:3]:
        show(item["subject_id"], item)
    verify = call(8101, "GET", "/verify")
    print("台账最终验真：", verify)

    banner("演示完成")
    print("产物目录：data/ledger(哈希链台账) data/manifests(不可变清单) "
          "data/reports(报告+输入快照)")
    print("提醒：全部为虚构数据；区间只量化抽样不确定性，"
          "本管道不训练模型，也不宣称观察数据证明因果。")


if __name__ == "__main__":
    main()
