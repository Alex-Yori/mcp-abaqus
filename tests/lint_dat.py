"""lint_dat 的口径回归：只抓"数据卡被静默丢弃"，不抓多 step deck 的惯常告警。

跑法（在 mcp-abaqus 根目录）：
    .venv/Scripts/python tests/lint_dat.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_abaqus.parsers import LINT_MAX_FINDINGS, lint_dat  # noqa: E402

BROKEN = """\
*Heading
 model echo
 ***WARNING: DATA CARDS AFTER *FIXED MASS SCALING  ARE IGNORED
  *boundary
  *endstep
"""

BENIGN = """\
*Heading
 ***WARNING: THE OPTION *BOUNDARY,TYPE=DISPLACEMENT HAS BEEN USED; CHECK STATUS
             FILE BETWEEN STEPS FOR WARNINGS ON ANY JUMPS PRESCRIBED ACROSS THE
             STEPS IN DISPLACEMENT VALUES OF TRANSLATIONAL DOF. ALL JUMPS IN
             DISPLACEMENTS ACROSS STEPS ARE IGNORED
  *boundary
  *endstep
"""

CLEAN = "*Heading\n *Step\n *Dynamic, Explicit\n *End Step\n"


def _broken_lines(n: int) -> str:
    return "".join(
        f" ***WARNING: DATA CARDS AFTER *KEYWORD{i:03d}/X  ARE IGNORED\n" for i in range(n)
    )


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # name -> (text, expected findings, expected first keyword, expected truncated)
        cases = {
            "broken.dat": (BROKEN, 1, "FIXED MASS SCALING", False),
            "benign.dat": (BENIGN, 0, None, False),
            "clean.dat": (CLEAN, 0, None, False),
            # exactly at the cap: nothing was dropped, so truncated must stay False
            "at_cap.dat": (_broken_lines(LINT_MAX_FINDINGS), LINT_MAX_FINDINGS, None, False),
            # one past the cap: capped list + truncated
            "over_cap.dat": (_broken_lines(LINT_MAX_FINDINGS + 1), LINT_MAX_FINDINGS, None, True),
        }
        for name, (text, want_n, want_kw, want_trunc) in cases.items():
            p = d / name
            p.write_text(text, encoding="utf-8")
            res = lint_dat(p)
            ok = (
                len(res.findings) == want_n
                and res.truncated is want_trunc
                and res.present is True
                and (want_kw is None or res.findings[0]["keyword"] == want_kw)
            )
            print(
                f"[{'PASS' if ok else 'FAIL'}] {name}: "
                f"findings={len(res.findings)} truncated={res.truncated}"
            )
            if not ok:
                failed += 1
                print("   ", res)
        res = lint_dat(d / "nope.dat")
        ok = res.findings == [] and res.truncated is False and res.present is False
        print(f"[{'PASS' if ok else 'FAIL'}] missing file -> ([], False, present=False)")
        failed += 0 if ok else 1
    print("OK" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
