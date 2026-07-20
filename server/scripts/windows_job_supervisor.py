from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004
ERROR_INVALID_PARAMETER = 87
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
STARTF_USESTDHANDLES = 0x00000100
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WINDOWS_TO_DOTNET_TICKS = 504_911_232_000_000_000


class IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", JobObjectBasicLimitInformation),
        ("io_info", IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("this_period_total_user_time", ctypes.c_int64),
        ("this_period_total_kernel_time", ctypes.c_int64),
        ("total_page_fault_count", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("total_terminated_processes", wintypes.DWORD),
    ]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD),
        ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD),
        ("y_size", wintypes.DWORD),
        ("x_count_chars", wintypes.DWORD),
        ("y_count_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD),
        ("reserved2_size", wintypes.WORD),
        ("reserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("process_id", wintypes.DWORD),
        ("thread_id", wintypes.DWORD),
    ]


def _raise_last_winerror(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed", None, error)


def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfo),
        ctypes.POINTER(ProcessInformation),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _filetime_ticks(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _process_identity(kernel32: Any, handle: int) -> tuple[int, str]:
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        _raise_last_winerror("GetProcessTimes")

    capacity = wintypes.DWORD(32_768)
    image = ctypes.create_unicode_buffer(capacity.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(capacity)):
        _raise_last_winerror("QueryFullProcessImageNameW")
    return _filetime_ticks(created) + WINDOWS_TO_DOTNET_TICKS, image.value


def _same_executable(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.realpath(actual)) == os.path.normcase(os.path.realpath(expected))


def _wait_for_process(kernel32: Any, handle: int, timeout_ms: int) -> str:
    result = int(kernel32.WaitForSingleObject(handle, timeout_ms))
    if result == WAIT_OBJECT_0:
        return "exited"
    if result == WAIT_TIMEOUT:
        return "timeout"
    _raise_last_winerror("WaitForSingleObject")
    raise AssertionError("unreachable")


def terminate_verified_process(
    process_id: int,
    expected_start_time_utc_ticks: int,
    expected_executable: str,
    *,
    start_time_tolerance_ticks: int,
    timeout_ms: int,
) -> dict[str, Any]:
    """Validate and terminate through one process handle to avoid PID reuse races."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE,
        False,
        process_id,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_INVALID_PARAMETER:
            return {"state": "missing", "process_id": process_id}
        return {"state": "error", "process_id": process_id, "winerror": error}
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            _raise_last_winerror("GetExitCodeProcess")
        if exit_code.value != STILL_ACTIVE:
            return {"state": "missing", "process_id": process_id}
        actual_ticks, actual_executable = _process_identity(kernel32, handle)
        start_time_matches = (
            abs(actual_ticks - expected_start_time_utc_ticks) <= start_time_tolerance_ticks
        )
        if not start_time_matches or not _same_executable(actual_executable, expected_executable):
            return {"state": "mismatch", "process_id": process_id}
        if not kernel32.TerminateProcess(handle, 1):
            error = ctypes.get_last_error()
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != STILL_ACTIVE:
                return {"state": "missing", "process_id": process_id}
            return {"state": "error", "process_id": process_id, "winerror": error}
        wait_state = _wait_for_process(kernel32, handle, timeout_ms)
        return {"state": "terminated" if wait_state == "exited" else "timeout", "process_id": process_id}
    finally:
        kernel32.CloseHandle(handle)


def _terminate_suspended_process(kernel32: Any, process: int, timeout_ms: int = 5_000) -> None:
    if not kernel32.TerminateProcess(process, 1):
        _raise_last_winerror("TerminateProcess")
    if _wait_for_process(kernel32, process, timeout_ms) != "exited":
        raise TimeoutError("suspended child did not exit after TerminateProcess")


def _write_handshake(path: Path, process_id: int) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"process_id": process_id, "job_managed": True}),
        encoding="utf-8",
    )
    temporary.replace(path)


def _open_log(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_BINARY, 0o600)
    os.set_inheritable(descriptor, True)
    return descriptor


def _active_process_count(kernel32: Any, job: int) -> int:
    accounting = JobObjectBasicAccountingInformation()
    returned_length = wintypes.DWORD()
    if not kernel32.QueryInformationJobObject(
        job,
        JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        ctypes.byref(returned_length),
    ):
        _raise_last_winerror("QueryInformationJobObject")
    return int(accounting.active_processes)


def supervise(spec: dict[str, Any]) -> int:
    import msvcrt

    executable = str(Path(spec["executable"]).resolve())
    arguments = [str(value) for value in spec["arguments"]]
    working_directory = str(Path(spec["working_directory"]).resolve())
    handshake = Path(spec["handshake_file"]).resolve()
    stdout_fd = _open_log(Path(spec["stdout_file"]).resolve())
    stderr_fd = _open_log(Path(spec["stderr_file"]).resolve())
    stdin_fd = os.open("NUL", os.O_RDONLY | os.O_BINARY)
    os.set_inheritable(stdin_fd, True)

    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        _raise_last_winerror("CreateJobObjectW")
    process = ProcessInformation()
    assigned = False
    resumed = False
    cleanup_error: BaseException | None = None
    try:
        limits = JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            _raise_last_winerror("SetInformationJobObject")

        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.flags = STARTF_USESTDHANDLES
        startup.stdin = msvcrt.get_osfhandle(stdin_fd)
        startup.stdout = msvcrt.get_osfhandle(stdout_fd)
        startup.stderr = msvcrt.get_osfhandle(stderr_fd)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable, *arguments]))
        if not kernel32.CreateProcessW(
            executable,
            command_line,
            None,
            None,
            True,
            CREATE_SUSPENDED | CREATE_NO_WINDOW,
            None,
            working_directory,
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            _raise_last_winerror("CreateProcessW")
        if not kernel32.AssignProcessToJobObject(job, process.process):
            _raise_last_winerror("AssignProcessToJobObject")
        assigned = True
        if kernel32.ResumeThread(process.thread) == 0xFFFFFFFF:
            _raise_last_winerror("ResumeThread")
        resumed = True
        _write_handshake(handshake, int(process.process_id))

        while _active_process_count(kernel32, job) > 0:
            time.sleep(0.2)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process.process, ctypes.byref(exit_code)):
            _raise_last_winerror("GetExitCodeProcess")
        return 0 if exit_code.value == 0 else 1
    finally:
        for descriptor in (stdin_fd, stdout_fd, stderr_fd):
            os.close(descriptor)
        if process.thread:
            kernel32.CloseHandle(process.thread)
        if process.process:
            if not resumed:
                try:
                    _terminate_suspended_process(kernel32, process.process)
                except BaseException as exc:
                    cleanup_error = exc
            kernel32.CloseHandle(process.process)
        if job:
            kernel32.CloseHandle(job)
        if not assigned:
            handshake.unlink(missing_ok=True)
        if cleanup_error is not None:
            raise cleanup_error


def main() -> int:
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--spec", type=Path)
    operation.add_argument("--terminate-pid", type=int)
    parser.add_argument("--start-time-utc-ticks", type=int)
    parser.add_argument("--executable")
    parser.add_argument("--start-time-tolerance-ticks", type=int, default=0)
    parser.add_argument("--timeout-ms", type=int, default=5_000)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("windows_job_supervisor.py only supports Windows")
    if args.terminate_pid is not None:
        if args.start_time_utc_ticks is None or args.executable is None:
            parser.error("--terminate-pid requires --start-time-utc-ticks and --executable")
        result = terminate_verified_process(
            args.terminate_pid,
            args.start_time_utc_ticks,
            args.executable,
            start_time_tolerance_ticks=args.start_time_tolerance_ticks,
            timeout_ms=args.timeout_ms,
        )
        print(json.dumps(result, separators=(",", ":")))
        # The JSON state is the control contract. A non-zero native status causes
        # PowerShell to discard that structured result under ErrorActionPreference=Stop.
        return 0
    assert args.spec is not None
    spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
    return supervise(spec)


if __name__ == "__main__":
    raise SystemExit(main())
