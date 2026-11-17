"""确定性统计计算：真实区间估计，纯标准库实现。

实现内容：
  - 标准正态分位数 norm_ppf（Acklam 有理逼近 + Newton 精修）
  - 正则化不完全 Beta 函数与 Student-t 分布函数/分位数
  - 单比例 Wilson 区间、比例差 Newcombe 区间
  - 均值差 Welch-t 区间、单样本均值 t 区间
  - 固定种子的百分位置信 bootstrap（确定性，便于复算）

本模块不做模型训练；所有结果均可由输入数据确定性复现。
观察性数据的区间估计只描述抽样不确定性，不构成因果断言。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# 标准正态分布函数与分位数
# ---------------------------------------------------------------------------

def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# Acklam (2003) 逆正态 CDF 系数
_ACKLAM_A = (
    -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
    1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
    6.680131188771972e01, -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
    -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
    3.754408661907416e00,
)


def norm_ppf(p: float) -> float:
    """标准正态分位数函数 Φ⁻¹(p)，相对误差约 1e-9 量级。"""
    if not 0.0 < p < 1.0:
        raise ValueError("norm_ppf 要求 0 < p < 1")
    p_low, p_high = 0.02425, 0.97575
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        num = (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q
                 + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5])
        den = (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q
               + _ACKLAM_D[3]) * q + 1.0
        x = num / den
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        num = (((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r
                 + _ACKLAM_A[3]) * r + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q
        den = (((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r
                 + _ACKLAM_B[3]) * r + _ACKLAM_B[4]) * r + 1.0)
        x = num / den
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        num = (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q
                 + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q + _ACKLAM_C[5])
        den = (((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q
               + _ACKLAM_D[3]) * q + 1.0
        x = -num / den
    # 一次 Newton 精修：x <- x - (Φ(x)-p)/φ(x)
    refine = (norm_cdf(x) - p) / math.exp(-0.5 * x * x) * math.sqrt(2.0 * math.pi)
    return x - refine


# ---------------------------------------------------------------------------
# 正则化不完全 Beta 函数与 Student-t
# ---------------------------------------------------------------------------

def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz 连分式，Numerical Recipes 第 6 章。"""
    max_iter, eps, fpmin = 300, 3.0e-14, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def beta_regularized(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
              + a * math.log(x) + b * math.log1p(-x))
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _beta_continued_fraction(a, b, x) / a
    return 1.0 - bt * _beta_continued_fraction(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    """Student-t 分布函数。"""
    if df <= 0:
        raise ValueError("自由度必须为正")
    x = df / (df + t * t)
    tail = 0.5 * beta_regularized(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def t_ppf(p: float, df: float) -> float:
    """Student-t 分位数，二分法反演 t_cdf。"""
    if not 0.0 < p < 1.0:
        raise ValueError("t_ppf 要求 0 < p < 1")
    if df <= 0:
        raise ValueError("自由度必须为正")
    lo, hi = -1.0e4, 1.0e4
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0e-12:
            break
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 二分类结局：Wilson 区间 + Newcombe 差值区间
# ---------------------------------------------------------------------------

def wilson_interval(events: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    z = norm_ppf(1.0 - alpha / 2.0)
    p = events / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (center - half, center + half)


def newcombe_diff_interval(p1: float, n1: int, p2: float, n2: int,
                           alpha: float = 0.05) -> tuple[float, float]:
    """比例差 p1-p2 的 Newcombe hybrid-score 区间。"""
    z = norm_ppf(1.0 - alpha / 2.0)

    def wilson(p: float, n: int) -> tuple[float, float]:
        denom = 1.0 + z * z / n
        center = (p + z * z / (2.0 * n)) / denom
        half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
        return center - half, center + half

    l1, u1 = wilson(p1, n1)
    l2, u2 = wilson(p2, n2)
    d = p1 - p2
    low = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return low, high


def two_proportion_z_test(events1: int, n1: int,
                          events2: int, n2: int) -> float:
    """合并方差两比例 z 检验，返回双侧 p 值。"""
    if n1 <= 0 or n2 <= 0:
        return float("nan")
    p1, p2 = events1 / n1, events2 / n2
    pooled = (events1 + events2) / (n1 + n2)
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n1 + 1.0 / n2))
    if se == 0:
        return 1.0
    z = (p1 - p2) / se
    return 2.0 * (1.0 - norm_cdf(abs(z)))


# ---------------------------------------------------------------------------
# 连续结局：Welch-t 区间
# ---------------------------------------------------------------------------

def mean_t_interval(values: Sequence[float],
                    alpha: float = 0.05) -> tuple[float, float]:
    n = len(values)
    if n < 2:
        return (float("nan"), float("nan"))
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    tcrit = t_ppf(1.0 - alpha / 2.0, n - 1)
    half = tcrit * math.sqrt(var / n)
    return mean - half, mean + half


def welch_diff_interval(values1: Sequence[float], values2: Sequence[float],
                        alpha: float = 0.05
                        ) -> tuple[float, float, float, float, float]:
    """返回 (差值, 下界, 上界, Welch 自由度, 双侧 p 值)。"""
    n1, n2 = len(values1), len(values2)
    if n1 < 2 or n2 < 2:
        raise ValueError("两组都至少需要 2 个观测才能做 Welch 区间")
    m1, m2 = sum(values1) / n1, sum(values2) / n2
    v1 = sum((v - m1) ** 2 for v in values1) / (n1 - 1)
    v2 = sum((v - m2) ** 2 for v in values2) / (n2 - 1)
    a, b = v1 / n1, v2 / n2
    se = math.sqrt(a + b)
    df = (a + b) ** 2 / (a * a / (n1 - 1) + b * b / (n2 - 1))
    tcrit = t_ppf(1.0 - alpha / 2.0, df)
    d = m1 - m2
    p_value = 2.0 * (1.0 - t_cdf(abs(d / se), df)) if se > 0 else 1.0
    return d, d - tcrit * se, d + tcrit * se, df, p_value


# ---------------------------------------------------------------------------
# 固定种子百分位 bootstrap（确定性复算用，非推断主方法）
# ---------------------------------------------------------------------------

class _SeededRng:
    """可复现 LCG（Numerical Recipes 常数），避免依赖全局随机状态。"""

    def __init__(self, seed: int):
        self._state = seed & 0xFFFFFFFF

    def next_uint(self) -> int:
        self._state = (1664525 * self._state + 1013904223) & 0xFFFFFFFF
        return self._state

    def randint(self, n: int) -> int:
        return self.next_uint() % n


def bootstrap_diff_interval(values1: Sequence[float], values2: Sequence[float],
                            reps: int = 2000, seed: int = 20261117,
                            alpha: float = 0.05) -> tuple[float, float]:
    """均值差的百分位 bootstrap 区间；重抽样与种子完全确定。"""
    if reps < 10:
        raise ValueError("bootstrap 次数过少")
    rng = _SeededRng(seed)
    n1, n2 = len(values1), len(values2)
    m1, m2 = sum(values1) / n1, sum(values2) / n2
    deltas: list[float] = []
    for _ in range(reps):
        s1 = sum(values1[rng.randint(n1)] for _ in range(n1)) / n1
        s2 = sum(values2[rng.randint(n2)] for _ in range(n2)) / n2
        deltas.append(s1 - s2)
    deltas.sort()
    lo_idx = max(0, int(math.floor((alpha / 2.0) * reps)) - 1)
    hi_idx = min(reps - 1, int(math.ceil((1.0 - alpha / 2.0) * reps)) - 1)
    return deltas[lo_idx], deltas[hi_idx], m1 - m2  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 顶层分析入口
# ---------------------------------------------------------------------------

def _sample_variance(values: Sequence[float], mean: float) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    return sum((v - mean) ** 2 for v in values) / (n - 1)


def analyze_binary(records: Iterable[dict], treatment: str, control: str,
                   alpha: float = 0.05) -> dict:
    """二分类结局分析。records: [{group, value(0/1)}, ...]。"""
    groups: dict[str, dict] = {}
    for rec in records:
        g = rec["group"]
        value = int(rec["value"])
        if value not in (0, 1):
            raise ValueError(f"二分类结局只能取 0/1，收到 {value!r}")
        bucket = groups.setdefault(g, {"n": 0, "events": 0, "values": []})
        bucket["n"] += 1
        bucket["events"] += value
        bucket["values"].append(value)

    result = {
        "outcome_type": "binary",
        "alpha": alpha,
        "confidence_level": 1.0 - alpha,
        "treatment": treatment,
        "control": control,
        "groups": {},
        "difference": None,
        "method": "Wilson 单组区间 + Newcombe hybrid-score 差值区间",
        "causal_note": "区间仅描述估计的抽样不确定性；除非随机分配且假设成立，"
                       "不能据此宣称处理与结局存在因果关系。",
    }
    for label, bucket in groups.items():
        n, k = bucket["n"], bucket["events"]
        p = k / n if n else float("nan")
        low, high = wilson_interval(k, n, alpha) if n else (float("nan"), float("nan"))
        result["groups"][label] = {
            "n": n, "events": k, "risk": p,
            "ci_method": "Wilson", "ci_low": low, "ci_high": high,
        }
    t_bucket, c_bucket = groups.get(treatment), groups.get(control)
    if t_bucket and c_bucket:
        p1, p2 = t_bucket["events"] / t_bucket["n"], c_bucket["events"] / c_bucket["n"]
        low, high = newcombe_diff_interval(p1, t_bucket["n"], p2, c_bucket["n"], alpha)
        result["difference"] = {
            "contrast": f"{treatment} - {control}",
            "estimate": p1 - p2,
            "ci_low": low,
            "ci_high": high,
            "ci_method": "Newcombe hybrid-score",
            "p_value": two_proportion_z_test(t_bucket["events"], t_bucket["n"],
                                             c_bucket["events"], c_bucket["n"]),
            "se_under_null": math.sqrt(
                ((t_bucket["events"] + c_bucket["events"])
                 / (t_bucket["n"] + c_bucket["n"]))
                * (1 - (t_bucket["events"] + c_bucket["events"])
                   / (t_bucket["n"] + c_bucket["n"]))
                * (1 / t_bucket["n"] + 1 / c_bucket["n"])),
        }
    return result


def analyze_continuous(records: Iterable[dict], treatment: str, control: str,
                       alpha: float = 0.05,
                       bootstrap: bool = False,
                       bootstrap_seed: int = 20261117,
                       bootstrap_reps: int = 2000) -> dict:
    """连续结局分析。records: [{group, value(数值)}, ...]。"""
    groups: dict[str, list[float]] = {}
    for rec in records:
        groups.setdefault(rec["group"], []).append(float(rec["value"]))

    result = {
        "outcome_type": "continuous",
        "alpha": alpha,
        "confidence_level": 1.0 - alpha,
        "treatment": treatment,
        "control": control,
        "groups": {},
        "difference": None,
        "method": "Welch 不等方差 t 区间（主方法）",
        "causal_note": "区间仅描述估计的抽样不确定性；除非随机分配且假设成立，"
                       "不能据此宣称处理与结局存在因果关系。",
    }
    for label, values in groups.items():
        n = len(values)
        mean = sum(values) / n if n else float("nan")
        sd = math.sqrt(_sample_variance(values, mean)) if n >= 2 else float("nan")
        low, high = mean_t_interval(values, alpha) if n >= 2 else (float("nan"), float("nan"))
        result["groups"][label] = {
            "n": n, "mean": mean, "sd": sd,
            "ci_method": "Student-t", "ci_low": low, "ci_high": high,
        }
    v1, v2 = groups.get(treatment), groups.get(control)
    if v1 is not None and v2 is not None and len(v1) >= 2 and len(v2) >= 2:
        d, low, high, df, p_value = welch_diff_interval(v1, v2, alpha)
        diff = {
            "contrast": f"{treatment} - {control}",
            "estimate": d,
            "ci_low": low,
            "ci_high": high,
            "ci_method": "Welch-t",
            "welch_df": df,
            "p_value": p_value,
        }
        if bootstrap:
            blo, bhi, bpoint = bootstrap_diff_interval(
                v1, v2, reps=bootstrap_reps, seed=bootstrap_seed, alpha=alpha)
            diff["bootstrap"] = {
                "ci_low": blo, "ci_high": bhi, "point_estimate": bpoint,
                "reps": bootstrap_reps, "seed": bootstrap_seed,
                "method": "固定种子百分位 bootstrap（敏感性核对，非主方法）",
            }
        result["difference"] = diff
    return result


def analyze(records: Iterable[dict], config: dict) -> dict:
    """按分析配置分发。config 至少包含 outcome_type/treatment/control/alpha。"""
    records = list(records)
    outcome_type = config["outcome_type"]
    treatment, control = config["treatment"], config["control"]
    alpha = float(config.get("alpha", 0.05))
    if outcome_type == "binary":
        return analyze_binary(records, treatment, control, alpha)
    if outcome_type == "continuous":
        return analyze_continuous(
            records, treatment, control, alpha,
            bootstrap=bool(config.get("bootstrap", False)),
            bootstrap_seed=int(config.get("bootstrap_seed", 20261117)),
            bootstrap_reps=int(config.get("bootstrap_reps", 2000)),
        )
    raise ValueError(f"未知结局类型 {outcome_type!r}（仅支持 binary/continuous）")
