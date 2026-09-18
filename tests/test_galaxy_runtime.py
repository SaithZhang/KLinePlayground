"""Runtime boundary tests: no broker connections, synthetic credentials only."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from backend import galaxy_data, galaxy_runtime


@pytest.fixture
def local_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(galaxy_runtime, "ROOT", tmp_path)
    monkeypatch.setattr(galaxy_data, "ROOT", tmp_path)
    for name in tuple(os.environ):
        if name.startswith(("KLINE_GALAXY_", "AD_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KLINE_GALAXY_RUNTIME", "native")
    credentials = tmp_path / "认证 文件.env"
    credentials.write_text("AD_USERNAME='test-user'\nAD_PASSWORD='test$literal#password'\n"
                           "AD_HOST=example.invalid\nAD_PORT=8600\n"
                           "AD_CACHE_DIR=other-project-cache\nUNRELATED_SECRET=do-not-load\n", encoding="utf-8")
    monkeypatch.setenv("KLINE_GALAXY_ENV_FILE", str(credentials))
    return tmp_path


@pytest.mark.parametrize("windows,expected", [(True, "native"), (False, "docker")])
def test_platform_default(local_runtime, monkeypatch, windows, expected):
    monkeypatch.delenv("KLINE_GALAXY_RUNTIME")
    monkeypatch.setattr(galaxy_runtime, "is_windows", lambda: windows)
    assert galaxy_runtime.runtime_mode() == expected


def test_windows_docker_fails_with_native_guidance(local_runtime, monkeypatch):
    monkeypatch.setattr(galaxy_runtime, "is_windows", lambda: True)
    monkeypatch.setenv("KLINE_GALAXY_RUNTIME", "docker")
    with pytest.raises(ValueError, match="Windows.*native"):
        galaxy_runtime.launch_spec()


def test_native_env_is_private_filtered_and_not_global(local_runtime, monkeypatch):
    monkeypatch.setenv("AD_USERNAME", "process-user")
    before = dict(os.environ)
    python, env = galaxy_runtime.native_runtime()
    assert Path(python).samefile(sys.executable)
    assert env["AD_USERNAME"] == "process-user"
    assert env["AD_PASSWORD"] == "test$literal#password"
    assert "UNRELATED_SECRET" not in env
    assert env["AD_CACHE_DIR"] == str(local_runtime / "data" / "galaxy_sdk_cache")
    assert dict(os.environ) == before


def test_local_json_bom_and_environment_overrides(local_runtime, monkeypatch):
    config = local_runtime / ".runtime" / "galaxy-native.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"python": "nonexistent-python", "env_file": "missing.env"}), encoding="utf-8-sig")
    monkeypatch.setenv("KLINE_GALAXY_PYTHON", sys.executable)
    assert galaxy_runtime.native_runtime()[1]["AD_USERNAME"] == "test-user"


def test_project_env_default_and_username_alias(local_runtime, monkeypatch):
    monkeypatch.delenv("KLINE_GALAXY_ENV_FILE")
    (local_runtime / ".env").write_text("AD_USER=alias\nAD_PASSWORD=placeholder\nAD_HOST=example.invalid\nAD_PORT=8600\n", encoding="utf-8")
    assert galaxy_runtime.native_runtime()[1]["AD_USERNAME"] == "alias"


def test_missing_credentials_report_names_without_values(local_runtime):
    (local_runtime / "认证 文件.env").write_text("AD_PASSWORD=synthetic-secret\n", encoding="utf-8")
    status = galaxy_runtime.runtime_status()
    assert not status["available"]
    assert "AD_USERNAME" in status["description"]
    assert "synthetic-secret" not in json.dumps(status)


def test_bad_port_is_redacted(local_runtime, monkeypatch):
    monkeypatch.setenv("AD_PORT", "synthetic-sensitive-invalid-port")
    status = galaxy_runtime.runtime_status()
    assert not status["available"]
    assert "AD_PORT" in status["description"]
    assert "synthetic-sensitive" not in json.dumps(status)


def test_sdk_check_does_not_claim_unavailable_runtime_is_ready(local_runtime, monkeypatch):
    def probe(command, **kwargs):
        assert command[-2] == "--check"
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        Path(command[-1]).write_text('{"ready": false}', encoding="utf-8")
        return SimpleNamespace(returncode=0, wait=lambda timeout: 0)
    monkeypatch.setattr(galaxy_runtime.subprocess, "Popen", probe)
    assert galaxy_runtime.runtime_status()["status"] == "NOT_READY"


def test_native_child_round_trip_with_spaces_unicode_and_utf8(local_runtime, monkeypatch):
    # A real child exercises command quoting, file closure, UTF-8 and temp cleanup on either OS.
    project = local_runtime / "中文 project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(galaxy_data, "ROOT", project)
    (scripts / "galaxy_worker.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "request=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "assert os.environ['AD_USERNAME']=='test-user'\n"
        "result={'calendar':['20260917'], 'periods':{}, 'note':'原生成功', 'code':request['code']}\n"
        "Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = galaxy_data.query_galaxy("603938.SH", pd.Timestamp("2026-09-17"), pd.Timestamp("2026-09-17"), ["daily"])
    assert result["note"] == "原生成功"
    assert result["code"] == "603938.SH"
    assert not list((project / "data" / "galaxy_requests").iterdir())
    assert json.loads((project / "data" / "galaxy_calendar.json").read_text(encoding="utf-8"))["source"] == "galaxy"


@pytest.mark.parametrize("partial", [False, True])
def test_windows_timeout_terminates_only_worker_and_keeps_checkpoint(local_runtime, monkeypatch, partial):
    monkeypatch.setattr(galaxy_runtime, "is_windows", lambda: True)
    # Permit simulating Windows process options on macOS/Linux CI.
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = []

    class Process:
        pid = 999991
        returncode = None
        def __init__(self, command, **kwargs):
            assert kwargs["creationflags"] == 0x08000000
            assert "start_new_session" not in kwargs
            assert command[0] == sys.executable
            if partial:
                Path(command[-1]).write_text(json.dumps({"periods": {"daily": {"rows": []}}}), encoding="utf-8")
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("worker", timeout)

    def taskkill(command, **kwargs):
        assert command == ["taskkill.exe", "/PID", "999991", "/T", "/F"]
        assert kwargs["creationflags"] == 0x08000000
        calls.append("tree_stopped")
        Process.returncode = 1
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(galaxy_data.subprocess, "Popen", Process)
    monkeypatch.setattr(galaxy_data.subprocess, "run", taskkill)
    ticks = iter([0, 181])
    monkeypatch.setattr(galaxy_data.time, "monotonic", lambda: next(ticks))
    args = ("603938.SH", pd.Timestamp("2026-09-17"), pd.Timestamp("2026-09-17"), ["daily", "15m"])
    if partial:
        result = galaxy_data.query_galaxy(*args)
        assert result["periods"]["15m"]["error"] == "GALAXY_TIMEOUT_RUNTIME"
        assert "daily" in result["periods"]
    else:
        with pytest.raises(ValueError, match="超时"):
            galaxy_data.query_galaxy(*args)
    assert calls == ["tree_stopped"]
    assert not list((local_runtime / "data" / "galaxy_requests").iterdir())


def test_mac_retains_runner_image_and_process_group(local_runtime, monkeypatch):
    monkeypatch.setenv("KLINE_GALAXY_RUNTIME", "auto")
    monkeypatch.setattr(galaxy_runtime, "is_windows", lambda: False)
    runner = local_runtime / "run_in_docker.sh"
    runner.touch()
    monkeypatch.setenv("KLINE_GALAXY_RUNNER", str(runner))
    mode, executable, env = galaxy_runtime.launch_spec()
    assert (mode, executable) == ("docker", str(runner))
    assert env["AD_DOCKER_IMAGE"] == "kline-galaxy:1.1.9-tables"
    assert "AD_PASSWORD" not in env  # Mac credentials are still owned by the skill runner.
    assert galaxy_runtime.process_options() == {"start_new_session": True}
    assert galaxy_runtime.runtime_status()["status"] == "RUNNER_FOUND"


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows venv redirector lifecycle")
def test_real_windows_cleanup_reaps_venv_interpreter(local_runtime):
    import ctypes
    import time
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    pid_file = local_runtime / "child.pid"
    code = "import os,time; from pathlib import Path; Path({!r}).write_text(str(os.getpid())); time.sleep(60)".format(str(pid_file))
    run = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, **galaxy_runtime.process_options())
    handle = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.is_file()
        interpreter_pid = int(pid_file.read_text())
        handle = kernel.OpenProcess(0x00100000, False, interpreter_pid)  # SYNCHRONIZE
        assert handle
        galaxy_runtime.stop_windows_process(run)
        assert run.poll() is not None
        assert kernel.WaitForSingleObject(handle, 5000) == 0  # Actual interpreter exited too.
    finally:
        if run.poll() is None:
            galaxy_runtime.stop_windows_process(run)
        if handle:
            kernel.CloseHandle(handle)
