# -*- coding: utf-8 -*-
"""GitHub 업데이트. bat/Python 및 exe 파일의 정합성을 SHA-256 해시 기반으로 검증 및 업데이트."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

GITHUB_OWNER = "cockcut"
GITHUB_REPO = "Windows_Tool"
GITHUB_BRANCH = "main"
API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
API_REF = f"{API}/git/ref/heads/{GITHUB_BRANCH}"
API_RELEASES = f"{API}/releases/latest"
API_CONTENTS = f"{API}/contents"
RAW_BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
ZIP_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"
HEADERS = {"User-Agent": "HSITX-WiFi-Connect-Updater"}
SHA_FILE_NAME = ".update_sha"

REPO_DIR = "Wi-Fi-Connect-Tool"
APP_PY = "Connect-Bssid-GUI.py"
EXE_NAME = "Connect-Bssid-GUI.exe"
EXE_SHA_NAME = f"{EXE_NAME}.sha256"
RELEASE_DOWNLOAD = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download"

SKIP_DIR_NAMES = {".git", "__pycache__", "results", "upload", ".grok", "windows_devel"}
SKIP_FILE_NAMES = {
    SHA_FILE_NAME,
    "update_token.txt",
    "access_token.txt",
    "build_exe.bat",
    "cookies.txt",
    "_replace_exe.bat",
}


def configure(*, repo_dir: str, app_py: str, exe_name: str, source_files=None):
    global REPO_DIR, APP_PY, EXE_NAME
    REPO_DIR = repo_dir
    APP_PY = app_py
    EXE_NAME = exe_name


def sha_path(root: Path) -> Path:
    return Path(root) / SHA_FILE_NAME


def read_local_sha(root: Path) -> str:
    try:
        return sha_path(root).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def write_local_sha(root: Path, sha: str) -> None:
    sha_path(root).write_text((sha or "").strip() + "\n", encoding="utf-8")


def file_sha256(data: bytes) -> str:
    """순수 데이터의 SHA-256 해시값을 반환합니다."""
    return hashlib.sha256(data).hexdigest()


class _Resp:
    def __init__(self, status_code: int, content: bytes, text: str):
        self.status_code = status_code
        self.content = content or b""
        self.text = text or ""
        self.ok = 200 <= status_code < 300

    def json(self):
        import json
        return json.loads(self.text or "{}")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _get(url: str, timeout: int = 20, stream: bool = False):
    if requests is not None:
        try:
            return requests.get(url, headers=HEADERS, timeout=timeout, stream=stream)
        except requests.exceptions.SSLError:
            return requests.get(url, headers=HEADERS, timeout=timeout, stream=stream, verify=False)
    import ssl
    import urllib.request
    req = urllib.request.Request(url, headers=HEADERS)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read()
            return _Resp(getattr(resp, "status", 200), data, data.decode("utf-8", "replace"))
    except Exception as exc:
        err = str(exc)
        code = 404 if "404" in err else 403 if "403" in err else 0
        if code:
            return _Resp(code, b"", "")
        raise


def fetch_remote_sha() -> str:
    r = _get(API_REF, timeout=15)
    if r.status_code == 404:
        raise RuntimeError("저장소를 찾을 수 없습니다.")
    if r.status_code == 403:
        raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부.")
    r.raise_for_status()
    sha = ((r.json() or {}).get("object") or {}).get("sha") or ""
    if not sha:
        raise RuntimeError("GitHub 응답에 SHA가 없습니다.")
    return sha


def app_py_sha256_matches(root: Path) -> bool:
    """원격 저장소의 app_py와 로컬 파일의 SHA-256 해시를 비교합니다."""
    local = Path(root) / APP_PY
    if not local.is_file():
        return False

    raw_url = f"{RAW_BASE_URL}/{REPO_DIR}/{APP_PY}"
    r = _get(raw_url, timeout=15)
    if r.status_code != 200 or not r.content:
        return False

    remote_sha256 = file_sha256(r.content)
    try:
        local_sha256 = file_sha256(local.read_bytes())
        return local_sha256 == remote_sha256
    except Exception:
        return False


def _norm_ver(s: str) -> str:
    t = (s or "").strip()
    if t.lower().startswith("v") and len(t) > 1 and t[1].isdigit():
        t = t[1:]
    return t.lower()


def parse_sha256_text(text: str) -> str:
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0].strip().lower().replace("sha256:", "")
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            return token
    return ""


def remote_exe_urls() -> tuple[str, str]:
    base = RELEASE_DOWNLOAD.rstrip("/")
    return f"{base}/{EXE_SHA_NAME}", f"{base}/{EXE_NAME}"


def fetch_remote_exe_sha256() -> str:
    sha_url, _ = remote_exe_urls()
    r = _get(sha_url, timeout=20)
    if r.status_code != 200 or not (r.text or r.content):
        raise RuntimeError(f"릴리스에서 {EXE_SHA_NAME} 을 받지 못했습니다. (HTTP {r.status_code})")
    text = r.text if getattr(r, "text", "") else (r.content or b"").decode("utf-8", "replace")
    digest = parse_sha256_text(text)
    if not digest:
        raise RuntimeError(f"{EXE_SHA_NAME} 형식이 올바르지 않습니다.")
    return digest


def find_remote_exe() -> dict:
    r = _get(API_RELEASES, timeout=15)
    if r.status_code == 200:
        data = r.json() or {}
        picked = None
        for a in data.get("assets") or []:
            name = a.get("name") or ""
            if not name.lower().endswith(".exe"):
                continue
            picked = a
            if name == EXE_NAME:
                break
        if picked and picked.get("browser_download_url"):
            return {
                "id": f"rel:{data.get('tag_name') or data.get('id')}:{picked.get('id')}",
                "name": picked.get("name") or EXE_NAME,
                "url": picked.get("browser_download_url"),
                "size": picked.get("size") or 0,
                "tag": str(data.get("tag_name") or ""),
                "digest": picked.get("digest") or "",
                "git_sha": "",
            }
    elif r.status_code not in (404,):
        if r.status_code == 403:
            raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부.")
        r.raise_for_status()

    last_err = "GitHub Releases와 저장소에서 exe를 찾지 못했습니다."
    for rel in (
        f"{REPO_DIR}/dist/{EXE_NAME}",
        f"{REPO_DIR}/{EXE_NAME}",
        f"windows/dist/{EXE_NAME}",
        f"release/{EXE_NAME}",
    ):
        cr = _get(f"{API_CONTENTS}/{rel}?ref={GITHUB_BRANCH}", timeout=15)
        if cr.status_code == 404:
            continue
        if cr.status_code == 403:
            raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부.")
        cr.raise_for_status()
        info = cr.json() or {}
        dl = info.get("download_url")
        if not dl:
            last_err = f"{rel} 다운로드 URL이 없습니다."
            continue
        return {
            "id": f"file:{info.get('sha')}",
            "name": info.get("name") or EXE_NAME,
            "url": dl,
            "size": info.get("size") or 0,
            "tag": "",
            "digest": "",
            "git_sha": info.get("sha") or "",
        }
    raise RuntimeError(last_err)


def local_file_hashes(path: Path) -> dict:
    data = Path(path).read_bytes()
    return {
        "sha256": file_sha256(data),
        "size": len(data),
    }


def remote_exe_hash_matches(exe_path: Path, remote_info: dict) -> bool:
    path = Path(exe_path)
    if not path.is_file():
        return False
    local = local_file_hashes(path)

    digest = (remote_info.get("digest") or "").lower().replace("sha256:", "").strip()
    if digest:
        return digest == local["sha256"]

    url = remote_info.get("url") or remote_info.get("exe_url")
    if not url:
        return False
    r = _get(url, timeout=180)
    if r.status_code != 200 or not r.content:
        return False
    return file_sha256(r.content) == local["sha256"]


def check_update(root: Path, frozen: bool = False, current_version: str = "", exe_path: str = "") -> dict:
    try:
        if frozen:
            sha_url, exe_url = remote_exe_urls()
            remote = fetch_remote_exe_sha256()
            exe = Path(exe_path) if exe_path else (Path(root) / EXE_NAME)
            local = ""
            if exe.is_file():
                local = file_sha256(exe.read_bytes())
            extra = {
                "exe_url": exe_url,
                "url": exe_url,
                "sha_url": sha_url,
                "exe_name": EXE_NAME,
                "digest": remote,
                "id": remote,
            }
            available = bool(remote) and bool(local) and remote != local
            if not exe.is_file():
                available = bool(remote)
            return {
                "ok": True,
                "available": available,
                "local": local,
                "remote": remote,
                "frozen": True,
                "message": "새 exe가 있습니다." if available else "최신 버전입니다.",
                **extra,
            }
        else:
            rsrc = _get(f"{API_CONTENTS}/{REPO_DIR}/{APP_PY}?ref={GITHUB_BRANCH}", timeout=15)
            if rsrc.status_code != 200:
                return {
                    "ok": True,
                    "available": False,
                    "local": read_local_sha(root),
                    "remote": "",
                    "frozen": frozen,
                    "message": f"저장소에 프로그램 소스({APP_PY})가 없습니다.",
                }
            remote = fetch_remote_sha()
            extra = {}
    except Exception as exc:
        return {
            "ok": False,
            "available": False,
            "local": read_local_sha(root),
            "remote": "",
            "frozen": frozen,
            "message": f"업데이트 확인 실패: {exc}",
        }

    local = read_local_sha(root)
    if (not local) and remote:
        if app_py_sha256_matches(root):
            write_local_sha(root, remote)
            local = remote

    available = bool(remote) and remote != local
    msg = "새 버전이 있습니다." if available else "최신 버전입니다."
    out = {
        "ok": True,
        "available": available,
        "local": local,
        "remote": remote,
        "frozen": frozen,
        "message": msg,
    }
    out.update(extra)
    return out


def _should_skip(rel: Path) -> bool:
    if any(p in SKIP_DIR_NAMES for p in rel.parts):
        return True
    if rel.name in SKIP_FILE_NAMES:
        return True
    if rel.suffix.lower() in {".pyc", ".pyo", ".exe", ".bl7"}:
        return True
    return False


def apply_source_update(root: Path, expected_sha: str = "") -> dict:
    root = Path(root)
    r = _get(ZIP_URL, timeout=90, stream=True)
    r.raise_for_status()
    raw = r.content
    if not raw:
        return {"ok": False, "message": "다운로드한 zip이 비어 있습니다."}

    tmp = root / ".update_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(tmp)
    tops = [p for p in tmp.iterdir() if p.is_dir()]
    src_root = tops[0] if len(tops) == 1 else tmp
    product = src_root / REPO_DIR
    if product.is_dir():
        src_root = product

    copied = 0
    for src in src_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(src_root)
        if _should_skip(rel):
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    shutil.rmtree(tmp, ignore_errors=True)
    sha = expected_sha or fetch_remote_sha()
    write_local_sha(root, sha)
    if copied == 0:
        return {"ok": False, "message": "덮어쓸 소스 파일이 없습니다."}
    return {"ok": True, "message": f"소스 업데이트 완료 ({copied}개 파일). 프로그램을 다시 실행하세요.", "sha": sha}


def apply_exe_update(root: Path, exe_path: str, info: dict | None = None) -> dict:
    root = Path(root)
    exe_path = Path(exe_path)
    info = info or {}
    sha_url, default_exe_url = remote_exe_urls()
    url = info.get("url") or info.get("exe_url") or default_exe_url
    expect = (info.get("digest") or info.get("remote") or "").lower().replace("sha256:", "").strip()
    if not expect:
        expect = fetch_remote_exe_sha256()
    r = _get(url, timeout=180, stream=True)
    r.raise_for_status()
    data = r.content
    if not data or data[:2] != b"MZ":
        return {"ok": False, "message": "받은 파일이 Windows exe가 아닙니다."}
    got = file_sha256(data)
    if expect and got != expect:
        return {"ok": False, "message": f"받은 exe SHA-256이 {EXE_SHA_NAME} 과 다릅니다."}
    new_path = exe_path.with_suffix(exe_path.suffix + ".new")
    new_path.write_bytes(data)
    bat = root / "_replace_exe.bat"
    bat.write_text(
        "\r\n".join(
            [
                "@echo off",
                "cd /d \"%~dp0\"",
                "timeout /t 2 /nobreak >nul",
                ":RETRY",
                f'del /f /q "{exe_path.name}"',
                f'if exist "{exe_path.name}" (timeout /t 1 /nobreak >nul & goto RETRY)',
                f'move /y "{new_path.name}" "{exe_path.name}"',
                f'start "" "%cd%\\{exe_path.name}"',
                'del "%~f0"',
                "",
            ]
        ),
        encoding="ascii",
    )
    return {
        "ok": True,
        "message": "새 exe를 받았습니다. 종료 후 자동으로 교체·재실행됩니다.",
        "replace_bat": str(bat),
        "sha": expect or got,
    }


def apply_update(root: Path, frozen: bool = False, exe_path: str = "", info: dict | None = None, expected_sha: str = "") -> dict:
    try:
        if frozen:
            if not exe_path:
                return {"ok": False, "message": "실행 중인 exe 경로를 알 수 없습니다."}
            return apply_exe_update(root, exe_path, info)
        return apply_source_update(root, expected_sha or ((info or {}).get("remote") or ""))
    except Exception as exc:
        return {"ok": False, "message": f"업데이트 실패: {exc}"}


def launch_replace_bat(bat: str, cwd: Path):
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("_PYI") or k in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(k, None)
    subprocess.Popen(["cmd", "/c", bat], cwd=str(cwd), env=env)