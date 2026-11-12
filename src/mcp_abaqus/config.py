"""Env-driven settings and path sandbox for the ABAQUS MCP server.

Every env var is read once at tool-call time (not import time) so a client can
restart the server with a different configuration without touching code.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ABAQUS_BAT = r"D:\ABAQUS2018\Commands\abaqus.bat"
DEFAULT_ENV_BAT = (
    r"C:\Users\Alex\AppData\Local\hermes\profiles\research-assistant\cache\scratch\abaqus_env.bat"
)


class ToolError(RuntimeError):
    """Actionable failure surfaced to the model as {"ok": false, "error": ...}."""


def _split_paths(raw: str | None) -> list[Path]:
    if not raw:
        return []
    return [Path(p.strip().strip('"')) for p in raw.split(os.pathsep) if p.strip()]


def _default_roots() -> list[Path]:
    roots = [Path.cwd()]
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        roots.append(Path(home))
    if Path("D:/files").exists():
        roots.append(Path("D:/files"))
    return roots


@dataclass(frozen=True)
class Settings:
    abaqus_cmd: str
    env_bat: str | None
    roots: tuple[Path, ...]
    workdir: Path
    default_cpus: int
    odb_timeout_s: int
    check_timeout_s: int

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls) -> "Settings":
        roots = _split_paths(os.environ.get("ABAQUS_MCP_ROOTS")) or _default_roots()
        roots = tuple(p.resolve() for p in roots)

        workdir_raw = os.environ.get("ABAQUS_MCP_WORKDIR")
        workdir = Path(workdir_raw).expanduser() if workdir_raw else roots[0] / "abq_jobs"
        workdir = workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)

        env_bat = os.environ.get("ABAQUS_ENV_BAT")
        if env_bat is None:
            env_bat = DEFAULT_ENV_BAT if Path(DEFAULT_ENV_BAT).is_file() else ""

        return cls(
            abaqus_cmd=os.environ.get("ABAQUS_CMD") or DEFAULT_ABAQUS_BAT,
            env_bat=env_bat or None,
            roots=roots,
            workdir=workdir,
            default_cpus=int(os.environ.get("ABAQUS_MCP_CPUS") or 4),
            odb_timeout_s=int(os.environ.get("ABAQUS_MCP_ODB_TIMEOUT") or 900),
            check_timeout_s=int(os.environ.get("ABAQUS_MCP_CHECK_TIMEOUT") or 600),
        )

    # ------------------------------------------------------------ resolution
    def resolve_abaqus(self) -> str:
        """Absolute path (or PATH name) of the abaqus launcher; fails loudly."""
        cand = self.abaqus_cmd
        if Path(cand).is_file():
            return str(Path(cand))
        found = shutil.which(cand)
        if found:
            return found
        for name in ("abaqus.bat", "abaqus"):
            found = shutil.which(name)
            if found:
                return found
        raise ToolError(
            f"abaqus launcher not found: '{cand}'. Set ABAQUS_CMD to abaqus.bat "
            f"(e.g. {DEFAULT_ABAQUS_BAT})."
        )

    def resolve_path(self, raw: str, *, must_exist: bool = False, base: Path | None = None) -> Path:
        """Resolve a user/model-supplied path and confine it to the allowed roots."""
        if not raw or not str(raw).strip():
            raise ToolError("path is empty")
        p = Path(str(raw).strip().strip('"')).expanduser()
        if not p.is_absolute():
            p = (base or self.workdir) / p
        p = p.resolve()
        if not any(_inside(p, root) for root in self.roots):
            allowed = os.pathsep.join(str(r) for r in self.roots)
            raise ToolError(f"path outside allowed roots ({allowed}): {p}")
        if must_exist and not p.exists():
            raise ToolError(f"path does not exist: {p}")
        return p


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def job_name_ok(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "._-" for c in name) and len(name) <= 80
