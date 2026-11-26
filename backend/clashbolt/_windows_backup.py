"""Create a backup artifact with a protected Windows access list before any write."""

import ctypes as ct
import msvcrt
import os
from ctypes import wintypes as wt
from pathlib import Path


def create_private_file(path: Path) -> int:
    class SecurityAttributes(ct.Structure):
        _fields_ = [("length", wt.DWORD), ("descriptor", ct.c_void_p), ("inherit", wt.BOOL)]

    advapi = ct.WinDLL("advapi32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    token_information = advapi.GetTokenInformation
    token_information.argtypes = [wt.HANDLE, wt.DWORD, ct.c_void_p, wt.DWORD, wt.LPDWORD]
    sid_string = advapi.ConvertSidToStringSidW
    sid_string.argtypes = [ct.c_void_p, ct.POINTER(wt.LPWSTR)]
    from_sddl = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    from_sddl.argtypes = [wt.LPCWSTR, wt.DWORD, ct.POINTER(ct.c_void_p), wt.LPDWORD]
    free = kernel.LocalFree
    free.argtypes = [wt.HLOCAL]
    free.restype = wt.HLOCAL
    create = kernel.CreateFileW
    create.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(SecurityAttributes),
        wt.DWORD, wt.DWORD, wt.HANDLE,
    ]
    create.restype = wt.HANDLE
    volume_info = kernel.GetVolumeInformationByHandleW
    volume_info.argtypes = [
        wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.LPDWORD, wt.LPDWORD,
        wt.LPDWORD, wt.LPWSTR, wt.DWORD,
    ]
    close = kernel.CloseHandle
    close.argtypes = [wt.HANDLE]

    process_token, token_user = wt.HANDLE(-4), 1  # GetCurrentProcessToken() pseudo-handle
    size = wt.DWORD()
    token_information(process_token, token_user, None, 0, ct.byref(size))
    if not size.value:
        raise ct.WinError(ct.get_last_error())
    identity = ct.create_string_buffer(size.value)
    if not token_information(process_token, token_user, identity, size, ct.byref(size)):
        raise ct.WinError(ct.get_last_error())
    # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is the SID pointer.
    sid = wt.LPWSTR()
    if not sid_string(ct.cast(identity, ct.POINTER(ct.c_void_p)).contents, ct.byref(sid)):
        raise ct.WinError(ct.get_last_error())
    descriptor = ct.c_void_p()
    try:
        # Protected DACL, one full-access entry for the creating account.
        if not from_sddl(f"D:P(A;;FA;;;{sid.value})", 1, ct.byref(descriptor), None):
            raise ct.WinError(ct.get_last_error())
    finally:
        free(sid)
    try:
        attributes = SecurityAttributes(ct.sizeof(SecurityAttributes), descriptor, False)
        generic_write, create_new, normal = 0x40000000, 1, 0x80
        handle = create(str(path), generic_write, 0, ct.byref(attributes), create_new, normal, None)
        error = ct.get_last_error()
    finally:
        free(descriptor)
    if handle == ct.c_void_p(-1).value:
        raise ct.WinError(error)
    try:
        flags = wt.DWORD()
        if not volume_info(handle, None, 0, None, None, ct.byref(flags), None, 0):
            raise ct.WinError(ct.get_last_error())
        persistent_acls = 0x00000008
        if not flags.value & persistent_acls:
            raise PermissionError("Backup storage must support persistent Windows ACLs")
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        path.unlink(missing_ok=True)
        raise
