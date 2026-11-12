"""batch_status 的四个终态字段各只承担一个含义——离线回归，不需要 Abaqus。

`done` 说的是"不会再变了"（含 `unknown`：有产物但没完成标记，比如被硬杀/重启/掉许可），
`stalled` 说的是"done 了但没跑完，要人看"。两者分开，否则一个 `unknown` 会让
`done` 永远为 False，轮询方只能等到超时，而"卡住"和"慢"在输出里长得一样。

跑法（在 mcp-abaqus 根目录）：
    .venv/Scripts/python tests/batch_states.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_abaqus.server import _batch_rollup  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + detail) if detail and not ok else ''}")
    return 0 if ok else 1


def row(name: str, state: str, **kw) -> dict:
    r = {"case": name, "state": state}
    r.update(kw)
    return r


def main() -> int:
    failed = 0

    # 1. normal finish: done and nothing stalled
    ok_rows = [row("A", "completed", datalint=[], dat_present=True), row("B", "completed", datalint=[], dat_present=True)]
    r = _batch_rollup(ok_rows)
    failed += check("completed cases: done true, stalled empty", r["done"] is True and r["stalled"] == [], str(r))

    # 2. an unknown case: done must become true (it will not change) and stalled must name it
    mixed = ok_rows + [row("C", "unknown", datalint=[], dat_present=True)]
    r = _batch_rollup(mixed)
    failed += check("unknown case still resolves done", r["done"] is True, str(r))
    failed += check("unknown case is reported as stalled", r["stalled"] == ["C"], str(r))
    failed += check("stalled does not pollute suspicious_cases", r["suspicious_cases"] == [], str(r))

    # 3. a pending case is neither settled nor stalled - polling must keep going
    pending = ok_rows + [row("D", "pending")]
    r = _batch_rollup(pending)
    failed += check("pending case keeps done false", r["done"] is False, str(r))
    failed += check("pending case is not stalled", r["stalled"] == [], str(r))
    failed += check("pending case is not counted as datalint-unchecked", r["datalint_unchecked"] == [], str(r))

    # 4. settled case whose .dat is gone: audit gap, not a 'clean' result
    gone = [row("E", "completed", datalint=[], dat_present=False)]
    r = _batch_rollup(gone)
    failed += check("completed case without .dat is unchecked", r["datalint_unchecked"] == ["E"], str(r))

    # 5. suspicious + truncated travel together, unchanged by the done/stalled split
    sus = [row("F", "completed", datalint=[{"line": 98}], dat_present=True, datalint_truncated=True)]
    r = _batch_rollup(sus)
    failed += check("suspicious case named", r["suspicious_cases"] == ["F"], str(r))
    failed += check("truncation surfaced", r["datalint_truncated"] is True, str(r))

    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
