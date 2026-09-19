"""Platform-specific Galaxy launch configuration; credentials stay in child envs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CREDENTIAL_KEYS = ("AD_USERNAME", "AD_PASSWORD", "AD_HOST", "AD_PORT")


def is_windows():
    return sys.platform == "win32"


def runtime_mode():
    mode = os.environ.get("KLINE_GALAXY_RUNTIME", "auto").strip().lower()
    if mode == "auto":
        return "native" if is_windows() else "docker"
    if mode not in ("native", "docker"):
        raise ValueError("KLINE_GALAXY_RUNTIME 只支持 auto、native、docker")
    if mode == "docker" and is_windows():
        raise ValueError("Windows 请使用 native；现有 Docker skill 入口面向 macOS/Linux")
    return mode


def runner_path():
    return Path(os.environ.get("KLINE_GALAXY_RUNNER", "~/.codex/skills/ad_api/scripts/run_in_docker.sh")).expanduser()


def process_options():
    # A worker never opens a console or inherits the desktop application's console.
    if is_windows():
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def stop_windows_process(run):
    # venv's python.exe is a redirector on Windows; terminating only that PID leaves
    # the real interpreter (and its SDK query) running. Target this request's tree.
    if run.poll() is None:
        subprocess.run(
            ["taskkill.exe", "/PID", str(run.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, **process_options(),
        )
    run.wait(timeout=5)


def native_runtime():
    settings_path = ROOT / ".runtime" / "galaxy-native.json"
    settings = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            if not isinstance(settings, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError("银河本机配置无效，请检查 .runtime/galaxy-native.json") from None
    python = os.environ.get("KLINE_GALAXY_PYTHON") or settings.get("python")
    if not python:
        if getattr(sys, "frozen", False):
            raise ValueError("打包版请通过 KLINE_GALAXY_PYTHON 指定已安装银河 SDK 的 Python")
        python = sys.executable
    if not isinstance(python, str):
        raise ValueError("银河 python 配置必须是 Python 可执行文件路径")
    executable = shutil.which(str(Path(python).expanduser()))
    if not executable:
        raise ValueError("未找到银河原生 Python，请设置 KLINE_GALAXY_PYTHON 或运行 scripts/setup_galaxy.ps1")
    env = os.environ.copy()
    configured_file = os.environ.get("KLINE_GALAXY_ENV_FILE") or settings.get("env_file")
    if configured_file and not isinstance(configured_file, str):
        raise ValueError("银河 env_file 配置必须是 .env 文件路径")
    default_file = ROOT / ".env" if (ROOT / ".env").is_file() else Path("~/.config/amazingdata/.env")
    env_path = Path(configured_file or default_file).expanduser()
    if configured_file and not env_path.is_file():
        raise ValueError("未找到银河凭据文件，请检查 KLINE_GALAXY_ENV_FILE 或本机 env_file 配置")
    if env_path.is_file():
        try:
            from dotenv import dotenv_values
        except ImportError:
            raise ValueError("读取银河凭据需要 python-dotenv，请安装 requirements-galaxy-native.txt") from None
        values = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False)
        # Reuse only authentication fields, never another project's cache or unrelated secrets.
        if not env.get("AD_USERNAME") and env.get("AD_USER"):
            env["AD_USERNAME"] = env["AD_USER"]
        for key in CREDENTIAL_KEYS:
            value = values.get(key) or (values.get("AD_USER") if key == "AD_USERNAME" else None)
            if not env.get(key) and value:
                env[key] = value
    if not env.get("AD_USERNAME") and env.get("AD_USER"):
        env["AD_USERNAME"] = env["AD_USER"]
    missing = [key for key in CREDENTIAL_KEYS if not env.get(key)]
    if missing:
        raise ValueError("银河原生配置缺少 " + ", ".join(missing) + "；请设置环境变量或 KLINE_GALAXY_ENV_FILE")
    try:
        if not 1 <= int(env["AD_PORT"]) <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("银河 AD_PORT 必须是 1–65535 的整数") from None
    env["AD_CACHE_DIR"] = str(Path(env.get("AD_CACHE_DIR") or ROOT / "data" / "galaxy_sdk_cache").expanduser().resolve())
    env["PYTHONIOENCODING"] = "utf-8"
    return executable, env


def launch_spec():
    mode = runtime_mode()
    if mode == "native":
        executable, env = native_runtime()
    else:
        runner = runner_path()
        if not runner.is_file():
            raise ValueError("未找到银河 ad-api skill 入口，请设置 KLINE_GALAXY_RUNNER")
        executable, env = str(runner), os.environ.copy()
        env.setdefault("AD_DOCKER_IMAGE", "kline-galaxy:1.1.9-tables")
        # GUI launches often resolve python3 to Apple's Xcode stub (exit 69).
        # Keep the skill entrypoint, but use this application's working interpreter.
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return mode, executable, env


def runtime_status():
    """Local readiness only: no login, network request or credentials in the response."""
    mode = "unknown"
    try:
        mode = runtime_mode()
        mode, executable, env = launch_spec()
        if mode == "native":
            with tempfile.TemporaryDirectory(prefix="kline-galaxy-check-") as tmp:
                output = Path(tmp) / "check.json"
                probe = subprocess.Popen(
                    [executable, str(ROOT / "scripts" / "galaxy_worker.py"), "--check", str(output)],
                    cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **process_options(),
                )
                try:
                    probe.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    if is_windows():
                        stop_windows_process(probe)
                    else:
                        probe.kill()
                        probe.wait(timeout=5)
                    raise
                if probe.returncode or not output.is_file():
                    raise ValueError("银河原生 Python 无法启动 SDK 检查")
                result = json.loads(output.read_text(encoding="utf-8"))
                if not result.get("ready"):
                    raise ValueError("银河原生 SDK 不可用，请安装官方 AmazingData、tgw 和 tables（含 HDF5 依赖）")
        if mode == "docker":
            return {"available": True, "runtime": mode, "status": "RUNNER_FOUND",
                    "description": "已找到 Docker skill 入口；容器、凭据与连接在补数时验证。"}
        return {"available": True, "runtime": mode, "status": "LOCAL_READY",
                "description": "本机运行配置已就绪；连接和数据权限在补数时验证。"}
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "银河本机运行环境检查失败或超时"
        return {"available": False, "runtime": mode, "status": "NOT_READY", "description": message}
