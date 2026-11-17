#!/usr/bin/env python3 -I
"""数学模块验证：教科书固定值 + 可选 scipy 对照。

直接 `python3 -I verify_stats.py` 运行；脚本目录自动进入 sys.path。
"""

import math
import os
import sys

# -I 隐含 -P，脚本目录不会自动进入 sys.path；显式插入项目根（包的父目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trialpipe import stats as s  # noqa: E402


def main() -> None:
    # 1) 正态分位数（Acklam + Newton 精修）
    assert abs(s.norm_ppf(0.975) - 1.959963984540054) < 1e-9
    assert abs(s.norm_ppf(0.025) + 1.959963984540054) < 1e-9
    assert abs(s.norm_ppf(0.5)) < 1e-12
    assert abs(s.norm_cdf(s.norm_ppf(0.314)) - 0.314) < 1e-12

    # 2) 正则化不完全 Beta 与 Student-t
    assert abs(s.beta_regularized(1, 1, 0.7) - 0.7) < 1e-12
    assert abs(s.beta_regularized(2, 3, 0.4) - 0.5248) < 1e-9
    assert abs(s.t_cdf(0, 10) - 0.5) < 1e-12
    assert abs(s.t_ppf(0.975, 10) - 2.2281388519649385) < 1e-9
    assert abs(s.t_ppf(0.975, 30) - 2.0422724554) < 1e-9
    assert abs(s.t_ppf(0.025, 5) + 2.5705818366) < 1e-9
    assert abs(s.t_cdf(s.t_ppf(0.9, 7.5), 7.5) - 0.9) < 1e-10

    # 3) Wilson 10/20（公式独立复算值，容差 1e-8）
    lo, hi = s.wilson_interval(10, 20)
    assert abs(lo - 0.29929800819821234) < 1e-10
    assert abs(hi - 0.7007019918017876) < 1e-10
    # 边缘：0 事件下界为 0（允许浮点零误差），全事件上界为 1
    lo0, hi0 = s.wilson_interval(0, 10)
    assert abs(lo0) < 1e-12 and 0 < hi0 < 0.3
    lo1, hi1 = s.wilson_interval(10, 10)
    assert 0.7 < lo1 < 1 and abs(hi1 - 1.0) < 1e-12

    # 4) Newcombe 8/20 vs 4/20
    lo, hi = s.newcombe_diff_interval(0.4, 20, 0.2, 20)
    print("Newcombe 差值 95% CI:", round(lo, 4), round(hi, 4))

    # 5) Welch 教科书例子：两组方差均为 2.5，SE=1，t=4，df=8
    d, lo, hi, df, p = s.welch_diff_interval(
        [27, 28, 25, 29, 26], [23, 22, 24, 21, 25])
    assert abs(d - 4.0) < 1e-12
    assert abs(df - 8.0) < 1e-10
    assert abs(p - 0.0039497728) < 1e-8
    assert abs(lo - 1.6939958648) < 1e-9 and abs(hi - 6.3060041352) < 1e-9
    print("Welch: d=%.3f df=%.3f p=%.6f CI=[%.3f, %.3f]" % (d, df, p, lo, hi))

    # 6) scipy 对照（如可用）
    try:
        from scipy import stats as ss

        for q in (0.025, 0.5, 0.975, 0.999):
            assert abs(s.norm_ppf(q) - ss.norm.ppf(q)) < 1e-8
        for dof in (1, 3, 10, 40, 120):
            assert abs(s.t_ppf(0.975, dof) - ss.t.ppf(0.975, dof)) < 1e-8
        r1 = s.analyze_continuous(
            [{"group": "A", "value": v} for v in [27, 28, 25, 29, 26]]
            + [{"group": "B", "value": v} for v in [23, 22, 24, 21, 25]],
            "A", "B")
        sp = ss.ttest_ind([27, 28, 25, 29, 26], [23, 22, 24, 21, 25],
                          equal_var=False)
        assert abs(r1["difference"]["p_value"] - sp.pvalue) < 1e-10
        assert abs(r1["difference"]["welch_df"] - sp.df) < 1e-8
        print("scipy 对照: 全部通过")
    except ImportError:
        print("scipy 未安装，跳过对照（教科书固定值已全部通过）")

    # 7) bootstrap 确定性
    b1 = s.bootstrap_diff_interval([1, 2, 3, 4, 5], [2, 3, 4, 5, 6],
                                   reps=500, seed=42)
    b2 = s.bootstrap_diff_interval([1, 2, 3, 4, 5], [2, 3, 4, 5, 6],
                                   reps=500, seed=42)
    assert b1 == b2
    print("bootstrap 确定性: OK",
          tuple(round(x, 4) if isinstance(x, float) else x for x in b1))
    print("全部数学验证通过")


if __name__ == "__main__":
    main()
