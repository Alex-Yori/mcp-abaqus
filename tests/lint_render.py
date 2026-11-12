"""模板渲染口径回归：`submit_batch` 只做纯文本替换。

两件事必须钉住（都在真 Abaqus 上踩过）：
  * 值里带 `\\n` 会凭空多出一整行 —— 如果那是关键字卡，就真的改变模型；
  * 给空串会留下空数据行，Abaqus 会把它当某张卡的数据。
另外确认渲染**不重排卡序**：渲染件与模板只有占位符处不同。

跑法（在 mcp-abaqus 根目录）：
    .venv/Scripts/python tests/lint_render.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_abaqus.batch import dedupe_warnings, render, render_warnings, validate_params  # noqa: E402
from mcp_abaqus.config import ToolError  # noqa: E402

TMPL = """\
*Step, name=Step-1
*Boundary
*Boundary
{{INJECT}}TOP, 3, 3, {{DISP}}
{{MS}}
*Output, field
*End Step
"""


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + detail) if detail and not ok else ''}")
    return 0 if ok else 1


def main() -> int:
    failed = 0

    # 1. newline injection: the MFS keyword ends up BEFORE the BC data line
    injected = render(TMPL, {"INJECT": "*Fixed Mass Scaling, factor=100\n", "MS": "", "DISP": "0.01"})
    lines = injected.splitlines()
    mfs_at = lines.index("*Fixed Mass Scaling, factor=100") if "*Fixed Mass Scaling, factor=100" in lines else -1
    top_at = lines.index("TOP, 3, 3, 0.01") if "TOP, 3, 3, 0.01" in lines else -1
    failed += check("a \\n in a value injects a whole extra line", len(lines) == len(TMPL.splitlines()) + 1)
    failed += check(
        "injected card lands before the data line when it is prefixed to it",
        mfs_at != -1 and top_at != -1 and mfs_at < top_at,
        f"mfs_at={mfs_at} top_at={top_at}",
    )

    # 2. same deck, card placed by its own slot: order comes from the template alone
    placed = render(TMPL, {"INJECT": "", "MS": "*Fixed Mass Scaling, factor=100", "DISP": "0.01"})
    plines = placed.splitlines()
    failed += check(
        "template order is preserved when the value carries no newline",
        plines.index("TOP, 3, 3, 0.01") < plines.index("*Fixed Mass Scaling, factor=100"),
    )
    failed += check(
        "empty value on its own line leaves an empty data line",
        "" in render("*Step\n{{MS}}\n*End Step\n", {"MS": ""}).splitlines(),
    )

    # 3. plain substitution is inline and lossless
    plain = render(TMPL, {"INJECT": "", "MS": "*Bulk Viscosity", "DISP": "0.05"})
    failed += check("plain substitution keeps the line count", len(plain.splitlines()) == len(TMPL.splitlines()))
    failed += check("value is written verbatim", "TOP, 3, 3, 0.05" in plain)
    failed += check("no placeholder survives", "{{" not in plain and "}}" not in plain)

    # 4. str() is part of the contract: it decides what number text reaches Abaqus
    failed += check('float 100.0 renders as "100.0"', render("{{V}}", {"V": 100.0}) == "100.0")
    failed += check('scientific 1e-5 renders as "1e-05"', render("{{V}}", {"V": 1e-5}) == "1e-05")
    failed += check('int 100 renders as "100" (not "100.0")', render("{{V}}", {"V": 100}) == "100")

    # 5. a value carrying a brace would leave a second-order {{slot}} in the deck
    try:
        validate_params("X01", {"DISP": "{{DISP}}"})
        failed += check("validate_params rejects a nested placeholder", False, "no exception")
    except ToolError:
        failed += check("validate_params rejects a nested placeholder", True)

    # 6. advisory signals: multi-line values and over-long rendered lines
    warns = render_warnings("B01", render(TMPL, {"INJECT": "x\n", "MS": "", "DISP": "0.01"}),
                            {"INJECT": "x\n", "MS": "", "DISP": "0.01"})
    failed += check("multi-line value is reported as a warning",
                    any(w["kind"] == "multiline_value" and w["key"] == "INJECT" for w in warns))
    long_warns = render_warnings("L01", "x" * 300 + "\n", {"V": "1"})
    failed += check("over-long rendered line is reported",
                    any(w["kind"] == "long_line" and w["chars"] == 300 for w in long_warns))

    # 7. a param the template never reads: the case runs with the template's value
    unused = render_warnings("U01", "TOP, 3, 3, 0.01\n", {"DISP": "0.01", "TEMP": "350"}, {"DISP"})
    failed += check("unused param is reported (not raised)",
                    any(w["kind"] == "unused_param" and w["keys"] == ["TEMP"] for w in unused))
    failed += check("params the template does read are not reported as unused",
                    all(w.get("keys") != ["DISP"] for w in unused))

    # 8. one long template line must not be repeated once per case
    two = (render_warnings("A", "y" * 300 + "\n", {"V": "1"})
           + render_warnings("B", "y" * 300 + "\n", {"V": "1"}))
    deduped = dedupe_warnings(two)
    failed += check("identical warnings collapse to one entry with a case count",
                    len(deduped) == 1 and deduped[0]["cases"] == 2)
    failed += check("merged entry names every case it stands for",
                    sorted(deduped[0]["case_names"]) == ["A", "B"])

    # 9. same key, different injection size -> different findings, must not merge
    differ = [
        {"case": "W01", "kind": "multiline_value", "key": "INJECT", "lines_added": 1},
        {"case": "W02", "kind": "multiline_value", "key": "INJECT", "lines_added": 3},
    ]
    kept = dedupe_warnings(differ)
    failed += check("1-line vs 3-line injection are not merged", len(kept) == 2, str(kept))
    failed += check("both injection sizes survive with their own line count",
                    sorted(w["lines_added"] for w in kept) == [1, 3])

    # 10. names stay bounded on a big sweep
    many = [{"case": f"C{i:02d}", "kind": "long_line", "line": 5, "chars": 300} for i in range(12)]
    merged = dedupe_warnings(many)
    failed += check("case names are capped with an ...+N more marker",
                    len(merged) == 1 and merged[0]["cases"] == 12
                    and merged[0]["case_names"][-1] == "…+7 more", str(merged))

    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
