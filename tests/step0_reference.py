"""/reload-mcp 之后第 0 步的离线参照：期望值必须由这份 fixture 自己算出来。

背景：线上对值用的批次（`abq_jobs/smoke_relooad/run-*/_batch_*`）会被冒烟清理，一旦消失，
`batch_status` 就会显示 `not_started`，看起来像「reload 没生效」。所以把同一批的
`.dat/.msg/.sta/.inp` 快照进 `tests/fixtures/step0_reference/`，期望值和它的证据放在一起。

线上（reload 后 `batch_status`）与离线（本脚本）必须给出同一组数：
    suspicious_cases == ["S02"]   S02.datalint == [(98, "FIXED MASS SCALING")]
    S01/S03 datalint == []        datalint_truncated == False

`.msg` 字节数是与 datalint 互相独立的第二条信号：S02 的边界数据行被吞 → 没有 jump 警告 → 0 B；
S01/S03 各有 4 条 `with a jump` 警告 → 616 B。

跑法（在 mcp-abaqus 根目录）：
    .venv/Scripts/python tests/step0_reference.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_abaqus.parsers import lint_dat, parse_sta  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "step0_reference"
CASES = ("S01", "S02", "S03")


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + detail) if detail and not ok else ''}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    findings = {}
    for case in CASES:
        res = lint_dat(FIX / case / f"{case}.dat")
        findings[case] = res
        sta = parse_sta(FIX / case / f"{case}.sta")
        msg = FIX / case / f"{case}.msg"
        jumps = msg.read_text(encoding="utf-8", errors="replace").count("with a jump")
        print(
            f"  {case}: present={res.present} datalint={[(f['line'], f['keyword']) for f in res.findings]} "
            f"completed={sta.get('completed')} inc={sta.get('increment_count')} "
            f"msg_bytes={msg.stat().st_size} jumps={jumps}"
        )

    # 1. the three datlint verdicts
    failed += check("S01 has no findings", findings["S01"].findings == [])
    failed += check(
        "S02 hits line 98 / FIXED MASS SCALING",
        len(findings["S02"].findings) == 1
        and findings["S02"].findings[0]["line"] == 98
        and findings["S02"].findings[0]["keyword"] == "FIXED MASS SCALING",
        str(findings["S02"].findings),
    )
    failed += check("S03 has no findings", findings["S03"].findings == [])
    failed += check("nothing truncated", not any(r.truncated for r in findings.values()))

    # 2. every case is settled and every .dat is readable -> nothing "unchecked"
    failed += check("all .dat present", all(r.present for r in findings.values()))
    failed += check(
        "all three completed",
        all(parse_sta(FIX / c / f"{c}.sta").get("completed") is True for c in CASES),
    )

    # 3. the independent second signal: dropped BC data line leaves no jump warning
    s02_jumps = (FIX / "S02" / "S02.msg").stat().st_size
    others = [(FIX / c / f"{c}.msg").stat().st_size for c in ("S01", "S03")]
    failed += check("S02 .msg is empty (no jump warnings)", s02_jumps == 0, str(s02_jumps))
    failed += check("S01/S03 .msg are non-empty and equal", others[0] == others[1] > 0, str(others))

    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
