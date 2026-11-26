"""Create a backup artifact with a protected Windows access list before any write."""

import ctypes as ct
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class _Win32Api:
    token_information: Callable[..., Any]
    sid_string: Callable[..., Any]
    from_sddl: Callable[..., Any]
    free: Callable[..., Any]
    create: Callable[..., Any]
    volume_info: Callable[..., Any]
    close: Callable[..., Any]
    open_osfhandle: Callable[[int, int], int]
    get_last_error: Callable[[], int]


def _win_error(code: int) -> OSError:
    if hasattr(ct, "WinError"):
        return ct.WinError(code)
    return OSError(code, f"Win32 error {code}")


def _load_win32_api() -> _Win32Api:
    import msvcrt

    dword = ct.c_uint32
    lpdword = ct.POINTER(dword)
    handle = ct.c_void_p
    lpcwstr = ct.c_wchar_p
    lpwstr = ct.c_wchar_p
    boolean = ct.c_int32

    advapi = ct.WinDLL("advapi32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)

    token_information = advapi.GetTokenInformation
    token_information.argtypes = [handle, dword, ct.c_void_p, dword, lpdword]
    token_information.restype = boolean

    sid_string = advapi.ConvertSidToStringSidW
    sid_string.argtypes = [ct.c_void_p, ct.POINTER(lpwstr)]
    sid_string.restype = boolean

    from_sddl = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    from_sddl.argtypes = [lpcwstr, dword, ct.POINTER(ct.c_void_p), lpdword]
    from_sddl.restype = boolean

    free = kernel.LocalFree
    free.argtypes = [ct.c_void_p]
    free.restype = ct.c_void_p

    create = kernel.CreateFileW
    create.argtypes = [
        lpcwstr, dword, dword, ct.c_void_p,
        dword, dword, handle,
    ]
    create.restype = handle

    volume_info = kernel.GetVolumeInformationByHandleW
    volume_info.argtypes = [
        handle, lpwstr, dword, lpdword, lpdword,
        lpdword, lpwstr, dword,
    ]
    volume_info.restype = boolean

    close = kernel.CloseHandle
    close.argtypes = [handle]
    close.restype = boolean

    return _Win32Api(
        token_information=token_information,
        sid_string=sid_string,
        from_sddl=from_sddl,
        free=free,
        create=create,
        volume_info=volume_info,
        close=close,
        open_osfhandle=msvcrt.open_osfhandle,
        get_last_error=ct.get_last_error,
    )


def _create_private_file(path: Path, api: _Win32Api) -> int:
    class SecurityAttributes(ct.Structure):
        _fields_ = [
            ("length", ct.c_uint32),
            ("descriptor", ct.c_void_p),
            ("inherit", ct.c_int32),
        ]

    process_token, token_user = ct.c_void_p(-4), 1  # GetCurrentProcessToken() pseudo-handle
    size = ct.c_uint32()
    api.token_information(process_token, token_user, None, 0, ct.byref(size))
    if not size.value:
        raise _win_error(api.get_last_error())
    identity = ct.create_string_buffer(size.value)
    if not api.token_information(process_token, token_user, identity, size, ct.byref(size)):
        raise _win_error(api.get_last_error())
    # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is the SID pointer.
    sid = ct.c_wchar_p()
    if not api.sid_string(ct.cast(identity, ct.POINTER(ct.c_void_p)).contents, ct.byref(sid)):
        raise _win_error(api.get_last_error())
    descriptor = ct.c_void_p()
    try:
        # Protected DACL, one full-access entry for the creating account.
        if not api.from_sddl(f"D:P(A;;FA;;;{sid.value})", 1, ct.byref(descriptor), None):
            raise _win_error(api.get_last_error())
    finally:
        api.free(sid)
    try:
        attributes = SecurityAttributes(ct.sizeof(SecurityAttributes), descriptor, False)
        generic_write, create_new, normal = 0x40000000, 1, 0x80
        handle = api.create(
            str(path), generic_write, 0, ct.byref(attributes), create_new, normal, None
        )
        error = api.get_last_error()
    finally:
        api.free(descriptor)
    if handle == ct.c_void_p(-1).value:
        raise _win_error(error)
    try:
        flags = ct.c_uint32()
        if not api.volume_info(handle, None, 0, None, None, ct.byref(flags), None, 0):
            raise _win_error(api.get_last_error())
        persistent_acls = 0x00000008
        if not flags.value & persistent_acls:
            raise PermissionError("Backup storage must support persistent Windows ACLs")
        flags_os = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        return api.open_osfhandle(handle, flags_os)
    except BaseException:
        api.close(handle)
        path.unlink(missing_ok=True)
        raise


def create_private_file(path: Path) -> int:
    return _create_private_file(path, _load_win32_api())
