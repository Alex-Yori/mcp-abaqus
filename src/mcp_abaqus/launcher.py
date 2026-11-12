"""Abaqus process launching: toolchain env, command construction, detached submit, liveness, kill.

Two hard-won rules are baked in:

* The Intel/VS toolchain comes from ``ABAQUS_ENV_BAT``. Calling that .bat inline
  from a ``cmd /c "... & ..."`` line breaks on Windows argument quoting, so the
  environment is captured ONCE (``cmd /c call bat & set``) and handed to every
  child as ``env=``.
* Abaqus exits 0 even when it fails ("Abaqus Error: ...", "exited with errors"),
  so success is judged from artifacts and console text, never ``returncode``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .config import Settings, ToolError, job_name_ok

_ALLOWED_EXTRA = {
    "memory",
    "mp_mode",
    "global",
    "restart",
    "oldjob",
    "scratch",
    "output_precision",
    "double",
    "domains",
    "standard_parallel",
    "threads_per_mp_copy",
}

CONSOLE_FAILURE_MARKERS = (
    "abaqus error:",
    "abaqus/analysis exited with errors",
    "problem during compilation",
    "aborted with system error",
    "the analysis has not been completed",
)

_ENV_CACHE: dict[str, dict] = {}


def _q(token: str) -> str:
    token = str(token)
    return f'"{token}"' if (" " in token or "\t" in token or token == "") else token


def toolchain_env(settings: Settings) -> dict:
    """os.environ plus everything ABAQUS_ENV_BAT sets (cached per script path+mtime).

    The mtime is part of the key on purpose: the probe costs a `cmd /c call`, but
    caching by path alone means an edited .bat keeps serving the old toolchain until
    the server restarts, which reads as "my fix did nothing".
    """
    path = settings.env_bat or ""
    try:
        stamp = Path(path).stat().st_mtime_ns if path else 0
    except OSError:
        stamp = 0
    key = f"{path}|{stamp}"
    cached = _ENV_CACHE.get(key)
    if cached is not None:
        return cached
    if path and len(_ENV_CACHE) > 8:
        _ENV_CACHE.clear()
    env = dict(os.environ)
    if path:
        if not Path(path).is_file():
            raise ToolError(f"ABAQUS_ENV_BAT not found: {path}")
        probe = subprocess.run(
            f'cmd /c call "{path}" >nul 2>&1 & set',
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=180,
        )
        for line in (probe.stdout or "").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                env[name.strip()] = value
        if not env.get("PATH"):
            raise ToolError(f"toolchain script produced no PATH: {path}")
    _ENV_CACHE[key] = env
    return env


def abaqus_cmdline(settings: Settings, abaqus_args: list[str], *, python_script: list[str] | None = None) -> str:
    """`"<exe>" arg...` without any cmd wrapper."""
    exe = _q(settings.resolve_abaqus())
    tail = ["python", *python_script] if python_script else list(abaqus_args)
    return " ".join([exe, *(_q(a) for a in tail)])


def build_command(settings: Settings, abaqus_args: list[str], *, python_script: list[str] | None = None) -> str:
    """Command line string for `abaqus <args>` / `abaqus python <script>`.

    Passed to subprocess as a STRING on purpose: a list would be re-quoted by
    list2cmdline with backslash escapes that cmd.exe does not understand.
    """
    inner = abaqus_cmdline(settings, abaqus_args, python_script=python_script)
    return f"cmd /c {inner}" if os.name == "nt" else inner


def console_failures(text: str) -> list[str]:
    """Lines in Abaqus console/log output that mean the run failed (exit code lies)."""
    out = []
    for line in (text or "").splitlines():
        low = line.lower()
        if "warning" in low and "error:" not in low:
            continue
        if any(marker in low for marker in CONSOLE_FAILURE_MARKERS):
            out.append(line.strip())
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def meta_path(workdir: Path, job: str) -> Path:
    return workdir / f"{job}.mcp-job.json"


def read_meta(workdir: Path, job: str) -> dict:
    p = meta_path(workdir, job)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def process_state(workdir: Path, job: str) -> dict:
    meta = read_meta(workdir, job)
    pid = int(meta.get("pid") or 0)
    return {
        "pid": pid,
        "pid_alive": _pid_alive(pid) if pid else False,
        "lck_present": (workdir / f"{job}.lck").exists(),
        "launched_at": meta.get("launched_at"),
        "cmdline": meta.get("cmdline"),
    }


def console_tail(workdir: Path, job: str, lines: int = 12) -> list[str]:
    p = workdir / f"{job}.console.log"
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


def option_strings(options: dict | None) -> list[str]:
    args: list[str] = []
    for key, val in (options or {}).items():
        k = str(key).strip()
        if k not in _ALLOWED_EXTRA:
            raise ToolError(f"option '{k}' not allowed; allowed: {sorted(_ALLOWED_EXTRA)}")
        if isinstance(val, bool):
            if val:
                args.append(k)
            continue
        if val in (None, ""):
            args.append(k)
            continue
        v = str(val).strip()
        if any(c in v for c in '&|<>"^%\n\r'):
            raise ToolError(f"option value for {k} contains shell metacharacters")
        args.append(f"{k}={v}")
    return args


def submit(
    settings: Settings,
    *,
    inp: Path,
    job: str,
    cpus: int,
    user_subroutine: Path | None = None,
    options: dict | None = None,
    overwrite: bool = False,
    extra_cli: list[str] | None = None,
) -> dict:
    """Start `abaqus job=<job> input=<inp> interactive` detached; return launch info."""
    if not job_name_ok(job):
        raise ToolError("job name must be alphanumeric/._- (max 80 chars)")
    workdir = inp.parent
    if not inp.is_file():
        raise ToolError(f"input file not found: {inp}")

    blockers = [workdir / f"{job}.{ext}" for ext in ("odb", "lck", "sta", "msg")]
    existing = [p.name for p in blockers if p.exists()]
    if existing and not overwrite:
        raise ToolError(f"job '{job}' already has {existing}; pass overwrite=true to replace")

    args = [f"job={job}", f"input={inp.name}", "interactive", "ask_delete=OFF", f"cpus={int(cpus)}"]
    args.extend(o for o in option_strings(options) if not o.startswith("cpus="))
    if user_subroutine is not None:
        if not user_subroutine.is_file():
            raise ToolError(f"user subroutine not found: {user_subroutine}")
        args.append(f"user={user_subroutine.name}")
    for tok in extra_cli or []:
        tok = str(tok)
        if not tok.startswith("-") or any(c in tok for c in '&|<>"^%\n\r'):
            raise ToolError(f"refusing positional/unsafe token: {tok!r}")
        args.append(tok)

    meta = launch_detached(settings, workdir=workdir, job=job, args=args, cpus=int(cpus))
    meta["inp"] = str(inp)
    return meta


def launch_detached(settings: Settings, *, workdir: Path, job: str, args: list[str], cpus: int) -> dict:
    """Start `abaqus <args>` in *workdir* without a console window; record pid + command line."""
    cmdline = build_command(settings, args)
    log_path = workdir / f"{job}.console.log"
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: a console-less child makes Abaqus
    # route its progress output nowhere (empty .console.log, no compile errors).
    creationflags = 0
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
        try:
            proc = subprocess.Popen(
                cmdline,
                cwd=str(workdir),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=toolchain_env(settings),
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError as exc:
            raise ToolError(f"failed to launch abaqus: {exc}") from exc

    meta = {
        "job": job,
        "workdir": str(workdir),
        "pid": proc.pid,
        "cmdline": cmdline,
        "cpus": int(cpus),
        "launched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "console_log": str(log_path),
    }
    meta_path(workdir, job).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    time.sleep(0.5)
    return meta


def restart(
    settings: Settings,
    *,
    workdir: Path,
    job: str,
    mode: str = "recover",
    new_job: str = "",
    inp: Path | None = None,
    cpus: int = 4,
    user_subroutine: Path | None = None,
    options: dict | None = None,
) -> dict:
    """Continue an interrupted job (`recover`) or read restart data into a new job (`oldjob`)."""
    state = process_state(workdir, job)
    if state["pid_alive"] or state["lck_present"]:
        raise ToolError(f"job '{job}' still running; stop it before restarting")

    opts = [o for o in option_strings(options) if not o.startswith("cpus=")]
    if mode == "recover":
        if not (workdir / f"{job}.res").is_file():
            raise ToolError(f"{job}.res not found in {workdir}; the deck needs *RESTART, WRITE to be recoverable")
        args = [f"job={job}", "recover", "interactive", "ask_delete=OFF", f"cpus={int(cpus)}", *opts]
        target = job
    elif mode == "oldjob":
        if not new_job:
            raise ToolError("mode='oldjob' needs new_job")
        if not job_name_ok(new_job):
            raise ToolError("new_job must be alphanumeric/._-")
        if inp is None or not inp.is_file():
            raise ToolError("mode='oldjob' needs an .inp containing *RESTART, READ")
        args = [
            f"job={new_job}",
            f"oldjob={job}",
            f"input={inp.name}",
            "interactive",
            "ask_delete=OFF",
            f"cpus={int(cpus)}",
            *opts,
        ]
        if user_subroutine is not None:
            args.append(f"user={user_subroutine.name}")
        target = new_job
    else:
        raise ToolError("mode must be 'recover' or 'oldjob'")

    meta = launch_detached(settings, workdir=workdir, job=target, args=args, cpus=int(cpus))
    meta["mode"] = mode
    meta["from_job"] = job
    return meta


def run_blocking(
    settings: Settings,
    args: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    python_script: list[str] | None = None,
) -> dict:
    """Run abaqus synchronously (datacheck / python helpers) and capture output."""
    cmdline = build_command(settings, args, python_script=python_script)
    try:
        cp = subprocess.run(
            cmdline,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=toolchain_env(settings),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"abaqus exceeded {timeout}s: {cmdline}")
    except OSError as exc:
        raise ToolError(f"failed to run abaqus: {exc}") from exc
    stdout, stderr = cp.stdout or "", cp.stderr or ""
    return {
        "returncode": cp.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "failures": console_failures(stdout) + console_failures(stderr),
    }


def stop_job(settings: Settings, *, workdir: Path, job: str, timeout_s: int = 60) -> dict:
    """Ask Abaqus to terminate the job, then kill the process tree if it lingers."""
    result: dict = {"job": job, "before": process_state(workdir, job), "steps": []}
    try:
        cp = run_blocking(settings, [f"job={job}", "terminate"], timeout=timeout_s, cwd=workdir)
        result["steps"].append({"action": "abaqus terminate", "returncode": cp["returncode"], "failures": cp["failures"]})
    except ToolError as exc:
        result["steps"].append({"action": "abaqus terminate", "error": str(exc)})
    time.sleep(2)
    state = process_state(workdir, job)
    if state["pid_alive"] and state["pid"]:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(state["pid"]), "/T", "/F"], capture_output=True)
        else:
            subprocess.run(["kill", "-9", str(state["pid"])], capture_output=True)
        result["steps"].append({"action": "kill -9", "pid": state["pid"]})
    lck = workdir / f"{job}.lck"
    if lck.exists():
        try:
            lck.unlink()
            result["steps"].append({"action": "remove stale .lck"})
        except OSError:
            pass
    time.sleep(0.5)
    result["after"] = process_state(workdir, job)
    result["stopped"] = not result["after"]["pid_alive"] and not (workdir / f"{job}.lck").exists()
    return result
