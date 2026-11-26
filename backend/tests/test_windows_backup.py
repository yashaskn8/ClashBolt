"""Cross-platform unit tests for Windows private backup ACL creation."""

import ctypes as ct
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from clashbolt._windows_backup import (
    _create_private_file,
    _load_win32_api,
    _Win32Api,
    _win_error,
    create_private_file,
)


def _fake_api(
    *,
    token_size: int = 16,
    second_token_ok: bool = True,
    sid_ok: bool = True,
    sddl_ok: bool = True,
    handle_value: int | None = 42,
    vol_ok: bool = True,
    vol_flags: int = 0x00000008,
    open_ok: bool = True,
    target_path: Path | None = None,
):
    events = []
    freed = []
    closed = []
    sid_buf = ct.create_unicode_buffer("S-1-5-21-test")

    def token_information(_token, _cls, buf, _buf_len, ret_len):
        size_ptr = ct.cast(ret_len, ct.POINTER(ct.c_uint32))
        if buf is None:
            size_ptr[0] = token_size
            return False
        if not second_token_ok:
            return False
        ct.cast(buf, ct.POINTER(ct.c_void_p))[0] = 0x1234
        return True

    def sid_string(_sid_ptr, out_str):
        if not sid_ok:
            return False
        ct.cast(out_str, ct.POINTER(ct.c_void_p))[0] = ct.addressof(sid_buf)
        return True

    def from_sddl(sddl, _rev, out_desc, _out_size):
        events.append(("sddl", sddl))
        if not sddl_ok:
            return False
        ct.cast(out_desc, ct.POINTER(ct.c_void_p))[0] = 0x5678
        return True

    def free(ptr):
        freed.append(ptr.value if hasattr(ptr, "value") else ptr)
        return None

    def create(path_str, access, share, _sa, disp, _attrs, _tmpl):
        events.append(("create", path_str, access, share, disp))
        if handle_value == ct.c_void_p(-1).value:
            return ct.c_void_p(-1).value
        if target_path is not None:
            target_path.write_bytes(b"")
        return handle_value

    def volume_info(_handle, _vn, _vns, _vs, _mcl, flags_ptr, _fsn, _fsns):
        if not vol_ok:
            return False
        ct.cast(flags_ptr, ct.POINTER(ct.c_uint32))[0] = vol_flags
        return True

    def close(h):
        closed.append(h)
        return True

    def open_osfhandle(h, flags):
        events.append(("open", h, flags))
        if not open_ok:
            raise OSError("osfhandle failed")
        return 99

    api = _Win32Api(
        token_information=token_information,
        sid_string=sid_string,
        from_sddl=from_sddl,
        free=free,
        create=create,
        volume_info=volume_info,
        close=close,
        open_osfhandle=open_osfhandle,
        get_last_error=lambda: 5,
    )
    return api, events, freed, closed


def test_windows_private_file_happy_path(tmp_path):
    target = tmp_path / "private.dump"
    api, events, freed, closed = _fake_api(target_path=target)
    fd = _create_private_file(target, api)
    assert fd == 99
    assert ("sddl", "D:P(A;;FA;;;S-1-5-21-test)") in events
    assert len(freed) == 2
    assert closed == []
    assert target.exists()


def test_windows_private_file_failures_free_and_unlink(tmp_path):
    target = tmp_path / "partial.dump"

    api, _, _, _ = _fake_api(token_size=0)
    with pytest.raises(OSError):
        _create_private_file(target, api)

    api, _, _, _ = _fake_api(second_token_ok=False)
    with pytest.raises(OSError):
        _create_private_file(target, api)

    api, _, _, _ = _fake_api(sid_ok=False)
    with pytest.raises(OSError):
        _create_private_file(target, api)

    api, _, freed, _ = _fake_api(sddl_ok=False)
    with pytest.raises(OSError):
        _create_private_file(target, api)
    assert len(freed) == 1

    api, _, freed, _ = _fake_api(handle_value=ct.c_void_p(-1).value)
    with pytest.raises(OSError):
        _create_private_file(target, api)
    assert len(freed) == 2

    api, _, _, closed = _fake_api(vol_ok=False, target_path=target)
    with pytest.raises(OSError):
        _create_private_file(target, api)
    assert closed == [42]
    assert not target.exists()

    api, _, _, closed = _fake_api(vol_flags=0, target_path=target)
    with pytest.raises(PermissionError, match="persistent Windows ACLs"):
        _create_private_file(target, api)
    assert closed == [42]
    assert not target.exists()

    api, _, _, closed = _fake_api(open_ok=False, target_path=target)
    with pytest.raises(OSError, match="osfhandle failed"):
        _create_private_file(target, api)
    assert closed == [42]
    assert not target.exists()


def test_win_error_helper_works_cross_platform(monkeypatch):
    err = _win_error(5)
    assert isinstance(err, OSError)
    if os.name == "nt":
        monkeypatch.delattr(ct, "WinError")
        fallback = _win_error(5)
        assert isinstance(fallback, OSError)
        assert "Win32 error 5" in str(fallback)


def test_load_win32_api_and_create_private_file_entry(monkeypatch, tmp_path):
    class FakeProc:
        argtypes = None
        restype = None

    class FakeDll:
        def __init__(self, name, use_last_error=True):
            assert name in ("advapi32", "kernel32")
            assert use_last_error is True

        def __getattr__(self, _name):
            return FakeProc()

    monkeypatch.setattr(ct, "WinDLL", FakeDll, raising=False)
    monkeypatch.setattr(ct, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda _h, _f: 77))

    loaded = _load_win32_api()
    assert loaded.open_osfhandle(1, 0) == 77

    api, _, _, _ = _fake_api()
    monkeypatch.setattr("clashbolt._windows_backup._load_win32_api", lambda: api)
    assert create_private_file(tmp_path / "entry.dump") == 99
