"""混合批次口径回归：一好一坏两个 case，只有 MFS 的位置不同。

判据分三层（坏 case 的 .msg 是 0 字节，所以"jumps==0"必须和"文件存在且为空"绑在一起，
否则路径写错时这条测试永远绿）：

  1. 输入处理：G01 无 datalint、B01 命中 line 98 / FIXED MASS SCALING；
  2. 物理痕迹：G01 的 .msg 有 "*Boundary ... with a jump" 警告，B01 一条没有；
  3. 期望条数从 fixture 自己算（TOP 集合的节点数），不硬编码 4，另加 >=1 兜底。

这个 8 节点玩具件**只能验输入处理**：G01 的 ODB 里 U/RF 全帧精确为 0（.dat 被
*Preprint, echo=NO 关掉了数据行回显，echo-grep 在这类 deck 上恒为 0），
任何位移/反力结论都不能从它出。物理对照要换真算例。

跑法（在 mcp-abaqus 根目录）：
    .venv/Scripts/python tests/mixed_batch.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp_abaqus.parsers import lint_dat  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "mixed_batch"
JUMP = "with a jump"


def jumps(dat_msg: Path) -> int:
    return dat_msg.read_text(encoding="utf-8", errors="replace").count(JUMP)


def set_size(inp: Path, nset: str) -> int:
    """Node count of one `*Nset, nset=NAME, generate` line (start, end, step)."""
    lines = inp.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith(f"*nset, nset={nset.lower()}") and "generate" in ln.lower():
            start, end, step = (int(x) for x in lines[i + 1].split(",")[:3])
            return len(range(start, end + 1, step))
    raise AssertionError(f"no generate nset {nset!r} in {inp.name}")


def boundary_magnitude(inp: Path, nset: str, dof: int) -> str:
    """The data line `NSET, dof, dof, value` for one boundary block, if present."""
    lines = inp.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("*boundary"):
            data = lines[i + 1].replace(" ", "")
            if data.lower().startswith(nset.lower() + ","):
                return data
    return ""


def check(label: str, ok: bool, detail: str = "") -> int:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + detail) if detail and not ok else ''}")
    return 0 if ok else 1


def main() -> int:
    failed = 0
    g, b = FIX / "G01", FIX / "B01"

    # 1. input handling
    gres, bres = lint_dat(g / "G01.dat"), lint_dat(b / "B01.dat")
    failed += check("G01 datalint==[]", gres.findings == [] and not gres.truncated, str(gres))
    failed += check(
        "B01 datalint hits line 98 / FIXED MASS SCALING",
        len(bres.findings) == 1
        and bres.findings[0]["line"] == 98
        and bres.findings[0]["keyword"] == "FIXED MASS SCALING",
        str(bres.findings),
    )

    # 2. physical trace in .msg (the only usable criterion for this toy deck)
    gmsg, bmsg = g / "G01.msg", b / "B01.msg"
    failed += check("G01.msg exists and is non-empty", gmsg.is_file() and gmsg.stat().st_size > 0)
    # a 0-byte .msg makes "jumps == 0" true for the wrong reason, so pin the size too
    failed += check("B01.msg exists and is 0 bytes", bmsg.is_file() and bmsg.stat().st_size == 0)
    gj, bj = jumps(gmsg), jumps(bmsg)
    failed += check("G01 reports the jump warning at least once", gj >= 1, f"jumps={gj}")
    failed += check("B01 reports no jump warning", bj == 0, f"jumps={bj}")

    # 3. expected count derived from the fixture, not hard-coded
    want = set_size(g / "G01.inp", "TOP")
    failed += check(f"G01 jump count == TOP set size ({want})", gj == want, f"jumps={gj}")

    # fixture intent: identical decks, only the MFS card's position differs
    failed += check(
        "G01 keeps the BC data line, B01 lost it to the MFS card",
        boundary_magnitude(g / "G01.inp", "TOP", 3) != ""
        and boundary_magnitude(b / "B01.inp", "TOP", 3) == "",
    )
    gtxt, btxt = (g / "G01.inp").read_text(encoding="utf-8"), (b / "B01.inp").read_text(encoding="utf-8")
    mfs = re.compile(r"^\*Fixed Mass Scaling", re.IGNORECASE | re.MULTILINE)
    failed += check(
        "both decks carry exactly one MFS card (only the position differs)",
        len(mfs.findall(gtxt)) == 1 and len(mfs.findall(btxt)) == 1,
    )

    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
