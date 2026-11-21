"""分批验收：在首查前锁定的批次顺序、显著性额度和净增门槛上做累计 McNemar 判定。"""

from collections import Counter
from fractions import Fraction
from decimal import Decimal

from .analysis import _display_p_value, _exact_p_value, _table, validate_rows


def batch_order(rows):
    """按数据中的出现顺序返回扫描批次，作为首查前锁定的批次顺序。"""
    order, seen = [], set()
    for row in rows:
        batch_id = row["batch_id"]
        if batch_id not in seen:
            seen.add(batch_id)
            order.append(batch_id)
    return order


def split_alpha(total, count):
    """把总显著性额度按百万分之一均分给各次查看，余数按顺序补给前面的批次。"""
    units = int(round(float(total) * 1_000_000))
    if count < 1:
        raise ValueError("至少需要一个扫描批次。")
    if units < count:
        raise ValueError("总显著性额度太小，无法覆盖每一次查看。")
    base, remainder = divmod(units, count)
    return [(base + (1 if index < remainder else 0)) / 1_000_000 for index in range(count)]


def build_schedule(rows, alphas, alpha_total, min_gain_pp):
    """锁定批次顺序、各次额度和净增门槛；各次额度之和不得超过总上限。"""
    order = batch_order(rows)
    if not order:
        raise ValueError("票据配对明细至少需要一个扫描批次。")
    values = split_alpha(alpha_total, len(order)) if alphas is None else [float(value) for value in alphas]
    if len(values) != len(order):
        raise ValueError(f"数据共 {len(order)} 个扫描批次，请为每个批次各提供一个显著性额度。")
    if any(value <= 0 or value >= 1 for value in values):
        raise ValueError("每次查看的显著性额度须在 0 与 1 之间且大于 0。")
    if sum((Decimal(str(value)) for value in values), Decimal('0')) > Decimal(str(alpha_total)):
        raise ValueError("各次显著性额度之和不能超过总上限。")
    return [
        {"sequence": index, "batch_id": batch_id, "alpha": alpha, "min_gain_pp": float(min_gain_pp)}
        for index, (batch_id, alpha) in enumerate(zip(order, values), 1)
    ]


def _gain_text(value):
    return f"{float(value):+.4f}".rstrip("0").rstrip(".")


def analyze_look(rows, cumulative_batches, entry, min_pairs, alpha_total, prior):
    """累计到当前批次后，用本次锁定的额度判定；返回结果和可封存的摘要。"""
    cumulative = set(cumulative_batches)
    selected = [row for row in rows if row["batch_id"] in cumulative]
    documents = validate_rows(selected)

    counts = Counter((row["a_pass"], row["b_pass"]) for row in documents)
    both_pass, a_only = counts[True, True], counts[True, False]
    b_only, both_fail = counts[False, True], counts[False, False]
    pair_count = len(documents)
    batch_pairs = sum(1 for row in documents if row["batch_id"] == entry["batch_id"])
    a_pass_count = both_pass + a_only
    b_pass_count = both_pass + b_only
    gain = Fraction(100 * (b_only - a_only), pair_count)
    gain_text = _gain_text(gain)
    p_value = _exact_p_value(a_only, b_only)
    p_text = _display_p_value(p_value)

    alpha = float(entry["alpha"])
    min_gain = float(entry["min_gain_pp"])
    alpha_fraction = Fraction(str(entry["alpha"]))
    min_gain_fraction = Fraction(str(entry["min_gain_pp"]))
    gates = [
        ("有效配对数", pair_count, f"≥ {min_pairs} 对", pair_count >= min_pairs),
        (f"双侧精确 p 值（本次额度 α = {entry['alpha']}）", p_text, f"≤ {entry['alpha']}",
         p_value <= alpha_fraction),
        ("候选 B 净增", f"{gain_text} 个百分点", f"≥ {min_gain} 个百分点且为正",
         gain > 0 and gain >= min_gain_fraction),
    ]
    failed_gates = [label for label, _, _, passed in gates if not passed]
    passed = not failed_gates
    conclusion = "pass" if passed else "continue"

    if passed:
        headline = f"第 {entry['sequence']} 次查看达到统计验收门槛，结论封存。"
        note = (f"累计 {len(cumulative_batches)} 个扫描批次、{pair_count} 对有效配对，"
                f"使用本次锁定的显著性额度 α = {entry['alpha']}。后续批次不再查看。")
    else:
        headline = f"第 {entry['sequence']} 次查看未达验收门槛。"
        note = (f"累计 {len(cumulative_batches)} 个扫描批次、{pair_count} 对有效配对，"
                f"使用本次锁定的显著性额度 α = {entry['alpha']}。"
                "发布本次结果后才能继续查看下一批。")

    disagreements = []
    for document in sorted(documents, key=lambda row: (row["b_pass"], row["document_id"])):
        if document["a_pass"] == document["b_pass"]:
            continue
        improved = document["b_pass"]
        disagreements.append({
            "document_id": document["document_id"],
            "batch_id": document["batch_id"],
            "document_type": document["document_type"],
            "outcome": "仅 B 通过" if improved else "仅 A 通过",
            "next_step": "抽核 B 的通过判定" if improved else "优先核对 B 的未通过字段",
        })

    ledger_rows = []
    for item in prior:
        ledger_rows.append({
            "sequence": item["sequence"], "batch_id": item["batch_id"],
            "pairs": item["pairs"], "alpha": item["alpha"], "min_gain_pp": item["min_gain_pp"],
            "conclusion": "达标封存" if item["conclusion"] == "pass" else "未达标",
            "published": "已发布" if item.get("published_at") else "未发布",
        })
    ledger_rows.append({
        "sequence": entry["sequence"], "batch_id": entry["batch_id"], "pairs": pair_count,
        "alpha": entry["alpha"], "min_gain_pp": entry["min_gain_pp"],
        "conclusion": "达标封存" if passed else "未达标", "published": "本次查看",
    })
    used_alpha = sum(float(item["alpha"]) for item in prior) + alpha

    result = {
        "state": "ok" if passed else "warning",
        "headline": headline,
        "note": note,
        "metrics": [
            {"label": "累计有效配对数", "value": pair_count,
             "detail": f"本次新增 {batch_pairs} 对，累计 {len(cumulative_batches)} 个批次"},
            {"label": "现用 A 通过率", "value": f"{a_pass_count / pair_count:.2%}",
             "detail": f"{a_pass_count}/{pair_count} 对"},
            {"label": "候选 B 通过率", "value": f"{b_pass_count / pair_count:.2%}",
             "detail": f"{b_pass_count}/{pair_count} 对"},
            {"label": "候选 B 净增（百分点）", "value": gain_text,
             "detail": f"仅 B 通过 {b_only} 对，减仅 A 通过 {a_only} 对"},
            {"label": "双侧精确 p 值", "value": p_text, "detail": f"本次额度 α = {entry['alpha']}"},
            {"label": "累计已用额度", "value": f"{used_alpha:g}",
             "detail": f"总上限 {alpha_total:g}，本次额度 {entry['alpha']}"},
        ],
        "tables": [
            _table("配对四格表", (("a", "现用 A"), ("b_pass", "候选 B 通过"), ("b_fail", "候选 B 未通过")), [
                {"a": "通过", "b_pass": both_pass, "b_fail": a_only},
                {"a": "未通过", "b_pass": b_only, "b_fail": both_fail},
            ]),
            _table("验收门槛", (("gate", "验收项"), ("observed", "观测值"), ("limit", "门槛"), ("status", "判断")), [
                {"gate": label, "observed": observed, "limit": limit, "status": "达标" if ok else "未达标"}
                for label, observed, limit, ok in gates
            ]),
            _table("分批查看台账", (
                ("sequence", "次序"), ("batch_id", "扫描批次"), ("pairs", "累计配对"),
                ("alpha", "显著性额度"), ("min_gain_pp", "净增门槛"), ("conclusion", "结论"),
                ("published", "发布状态"),
            ), ledger_rows),
            _table("差异票据复核清单", (
                ("document_id", "票据编号"), ("batch_id", "扫描批次"), ("document_type", "票据类型"),
                ("outcome", "配对判定"), ("next_step", "复核动作"),
            ), disagreements),
        ],
        "charts": [{
            "title": "四类配对判定",
            "kind": "bar",
            "series": [
                {"label": label, "value": count}
                for label, count in (("均通过", both_pass), ("仅 A 通过", a_only),
                                     ("仅 B 通过", b_only), ("均未通过", both_fail))
            ],
        }],
        "sections": [
            {"title": "分批查看口径", "text": (
                f"本计划总显著性额度为 {alpha_total:g}，各次额度在首查前锁定且之和不超过总上限。"
                f"第 {entry['sequence']} 次查看累计到「{entry['batch_id']}」，只用本次额度 "
                f"α = {entry['alpha']} 判定。达标即封存结论；未达标须先发布结果，才能查看下一批。"
            )},
            {"title": "复核与交付", "text": (
                "先核对仅 A 通过的票据，再抽核仅 B 通过的票据。按票据编号回看影像与人工底单，"
                "补录复核决定后导出配对四格表、门槛判断、分批查看台账和差异清单。"
            )},
        ],
    }
    summary = {
        "sequence": entry["sequence"], "batch_id": entry["batch_id"], "alpha": entry["alpha"],
        "min_gain_pp": entry["min_gain_pp"], "pairs": pair_count, "batch_pairs": batch_pairs,
        "a_only": a_only, "b_only": b_only, "gain_pp": gain_text, "p_value": p_text,
        "used_alpha": used_alpha, "conclusion": conclusion, "failed_gates": failed_gates,
    }
    return result, summary
