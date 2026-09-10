# -*- coding: utf-8 -*-
"""Wi-Fi BSSID 접속 GUI (Python). Version v0.0.1"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from ctypes import wintypes
from datetime import datetime
from html import escape as xml_escape
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

APP_VERSION = "v0.0.1"
MAX_LOG_FILES = 5

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parent

LOG_DIR = ROOT_DIR / "logs"
HIDDEN_SSID: dict[str, str] = {}
LOG_FILE: Path | None = None


def read_version() -> str:
    p = ROOT_DIR / "ver.txt"
    try:
        v = p.read_text(encoding="utf-8").strip()
        if v:
            return v
    except Exception:
        pass
    return APP_VERSION


APP_VERSION = read_version()

try:
    import updater as gh_updater
    gh_updater.configure(
        repo_dir="Wi-Fi-Connect-Tool",
        app_py="Connect-Bssid-GUI.py",
        exe_name="Connect-Bssid-GUI.exe",
        source_files=(
            "Connect-Bssid-GUI.py",
            "Connect-Bssid-GUI.bat",
            "eap_connect.ps1",
            "ver.txt",
            "updater.py",
        ),
    )
except Exception:
    gh_updater = None


# ---------------------------------------------------------------------------
# netsh / helpers
# ---------------------------------------------------------------------------
def netsh(args: list[str]) -> str:
    try:
        r = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return str(e)


def netsh_lines(args: list[str]) -> list[str]:
    raw = netsh(args)
    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def location_permission_blocked(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(r"location permission|Location services|위치 서비스|위치 권한|ms-settings:privacy-location", text, re.I)
    )


def first_match(text: str, patterns: list[str]) -> str | None:
    if not text:
        return None
    for p in patterns:
        m = re.search(p, text)
        if m:
            if m.lastindex:
                return m.group(1)
            return m.group(0)
    return None


def format_band(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"(2\.4|5|6)\s*(GHz|ghz|GHZ)", s)
    if m:
        return f"{m.group(1)}GHz"
    if re.search(r"2\.4", s):
        return "2.4GHz"
    if re.search(r"\b6\b", s) and re.search(r"ghz|GHz|밴드|band", s, re.I):
        return "6GHz"
    if re.search(r"\b5\b", s) and re.search(r"ghz|GHz|밴드|band", s, re.I):
        return "5GHz"
    return s


def format_wifi_phy(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"802\.11\s*([a-z]+)", s, re.I)
    if m:
        return "802.11" + m.group(1).lower()
    m = re.search(r"\b(be|ax|ac|n|g|a|b|ad)\b", s, re.I)
    if m and re.search(r"802|wifi|wi-fi|무선|radio", s, re.I):
        return "802.11" + m.group(1).lower()
    return s


def format_mac(mac: str) -> str:
    hexed = re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()
    if len(hexed) != 12:
        return mac
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def normalize_mac(mac: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()


def mac_bytes(mac: str) -> bytes:
    h = normalize_mac(mac)
    if len(h) != 12:
        raise ValueError(f"BSSID 형식 오류: {mac}")
    return bytes(int(h[i : i + 2], 16) for i in range(0, 12, 2))


_PROFILE_CACHE: list[str] | None = None


def saved_profiles(refresh: bool = False) -> list[str]:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is not None and not refresh:
        return _PROFILE_CACHE
    names: list[str] = []
    for line in netsh_lines(["wlan", "show", "profiles"]):
        n = first_match(line, [r"프로필\s*:\s*(.+)$", r"Profile\s*:\s*(.+)$"])
        if not n:
            continue
        n = n.strip()
        if n and not re.search(r"인터페이스|Interface|없음|there are no", n, re.I):
            names.append(n)
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    _PROFILE_CACHE = out
    return out


def resolve_profile(ssid: str | None) -> str | None:
    if not ssid:
        return None
    allp = saved_profiles()
    for n in allp:
        if n == ssid:
            return n
    sl = ssid.lower()
    for n in allp:
        if n.lower() == sl:
            return n
    return None


def profile_passphrase(profile: str) -> str | None:
    raw = netsh(["wlan", "show", "profile", f"name={profile}", "key=clear"])
    write_file_log(f"show profile name='{profile}' key=clear (key redacted)", "DEBUG")
    key = first_match(raw, [r"(?im)(?:키\s*콘텐츠|보안\s*키\s*콘텐츠|Key\s*Content)\s*:\s*(.+)$"])
    if key:
        key = key.strip()
        if key and not re.search(r"없음|absent|none", key, re.I):
            return key
    if re.search(r"error|오류|denied|거부|권한", raw, re.I):
        raise RuntimeError("저장된 암호를 읽지 못했습니다. 관리자 권한으로 실행해 보세요.")
    return None


def current_wifi() -> dict:
    info = {"State": "", "Ssid": "", "Bssid": "", "Signal": "", "Channel": "", "Radio": "", "Profile": "", "Raw": ""}
    lines = netsh_lines(["wlan", "show", "interfaces"])
    info["Raw"] = "\r\n".join(lines)
    for line in lines:
        v = first_match(line, [r"(?i)상태\s*:\s*(.+)$", r"(?i)^\s*State\s*:\s*(.+)$"])
        if v:
            info["State"] = v.strip()
            continue
        if "BSSID" in line.upper():
            v = first_match(line, [r"(?i)(?:AP\s*)?BSSID\s*:\s*([0-9A-Fa-f:\-]{11,17})"])
            if v:
                info["Bssid"] = format_mac(v)
                continue
        v = first_match(line, [r"(?i)^\s*SSID\s*:\s*(.+)$"])
        if v:
            info["Ssid"] = v.strip()
            continue
        v = first_match(line, [r"(?i)신호\s*:\s*(.+)$", r"(?i)^\s*Signal\s*:\s*(.+)$"])
        if v:
            info["Signal"] = v.strip()
            continue
        v = first_match(line, [r"(?i)^\s*Rssi\s*:\s*(.+)$"])
        if v and not info["Signal"]:
            info["Signal"] = v.strip()
            continue
        v = first_match(line, [r"(?i)채널\s*:\s*(.+)$", r"(?i)^\s*Channel\s*:\s*(.+)$"])
        if v:
            info["Channel"] = v.strip()
            continue
        v = first_match(line, [r"(?i)라디오\s*유형\s*:\s*(.+)$", r"(?i)Radio type\s*:\s*(.+)$", r"(?i)송수신 장치 종류\s*:\s*(.+)$"])
        if v:
            info["Radio"] = v.strip()
            continue
        v = first_match(line, [r"(?i)프로필\s*:\s*(.+)$", r"(?i)^\s*Profile\s*:\s*(.+)$"])
        if v:
            info["Profile"] = v.strip()
    if not info["Bssid"]:
        v = first_match(info["Raw"], [r"(?i)BSSID\s*:?\s*([0-9A-Fa-f]{2}([:\-]?[0-9A-Fa-f]{2}){5})"])
        if v:
            info["Bssid"] = format_mac(v)
    return info


def is_connected(cur: dict) -> bool:
    s = (cur.get("State") or "").strip()
    if not s:
        return False
    if re.search(r"disconnect", s, re.I) or "연결되지" in s:
        return False
    if re.search(r"not\s*connected|disassociated", s, re.I):
        return False
    if re.search(r"(?i)\bconnected\b", s):
        return True
    if "연결됨" in s or s == "연결":
        return True
    return False


def is_authenticating(cur: dict) -> bool:
    s = (cur.get("State") or "").strip()
    if not s:
        return False
    return bool(re.search(r"authenticat", s, re.I) or "인증" in s)


def trigger_wlan_scan() -> int:
    if not wlanapi:
        return -1
    guid = _wlan_first_guid()
    negotiated = wintypes.DWORD()
    handle = ctypes.c_void_p()
    rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        return int(rc)
    try:
        wlanapi.WlanScan.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        wlanapi.WlanScan.restype = wintypes.DWORD
        ie = WLAN_RAW_DATA()
        ie.dwDataSize = 0
        ie.DataBlob[0] = 0
        return int(wlanapi.WlanScan(handle, ctypes.byref(guid), None, ctypes.byref(ie), None))
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def nearby_networks(do_scan: bool = True) -> list[dict]:
    saved_profiles(refresh=True)
    if do_scan:
        rc = trigger_wlan_scan()
        write_file_log(f"WlanScan active(probe) rc={rc}", "DEBUG")
        time.sleep(1.2)
    lines = netsh_lines(["wlan", "show", "networks", "mode=bssid"])
    out: list[dict] = []
    cur_ssid = auth = enc = None
    pending: dict | None = None

    def commit() -> None:
        nonlocal pending
        if not pending or not pending.get("Bssid"):
            pending = None
            return
        hidden = not (pending.get("Ssid") or "").strip()
        pending["Hidden"] = hidden
        pending["HasProfile"] = False if hidden else bool(resolve_profile(pending.get("Ssid")))
        out.append(pending)
        pending = None

    for line in lines:
        v = first_match(line, [r"^\s*SSID\s+\d+\s*:\s*(.*)$"])
        if v is not None:
            commit()
            cur_ssid = v.strip()
            auth = enc = None
            continue
        v = first_match(line, [r"(?i)^\s*Authentication\s*:\s*(.+)$", r"^\s*인증\s*:\s*(.+)$"])
        if v:
            auth = v.strip()
            continue
        v = first_match(line, [r"(?i)^\s*Encryption\s*:\s*(.+)$", r"^\s*암호화\s*:\s*(.+)$"])
        if v:
            enc = v.strip()
            continue
        v = first_match(line, [r"(?i)^\s*BSSID\s+\d+\s*:\s*([0-9a-fA-F:\-]+)"])
        if v:
            commit()
            pending = {
                "Ssid": cur_ssid or "",
                "Bssid": format_mac(v),
                "Signal": "",
                "Channel": "",
                "Radio": "",
                "Band": "",
                "Auth": auth or "",
                "Encrypt": enc or "",
            }
            continue
        if not pending:
            continue
        v = first_match(line, [r"(?i)^\s*Signal\s*:\s*(.+)$", r"^\s*신호\s*:\s*(.+)$"])
        if v:
            pending["Signal"] = v.strip()
            continue
        v = first_match(line, [r"(?i)^\s*Channel\s*:\s*(.+)$", r"^\s*채널\s*:\s*(.+)$"])
        if v:
            pending["Channel"] = v.strip()
            continue
    commit()

    def sig_num(n: dict) -> int:
        m = re.search(r"\d+", n.get("Signal") or "")
        return int(m.group(0)) if m else 0

    out.sort(key=lambda n: ((n.get("Ssid") or ""), -sig_num(n)))
    return out


def map_auth(auth: str, enc: str) -> dict:
    a = (auth or "").lower()
    e = (enc or "").lower()
    if re.search(r"open|열기|오픈", a):
        return {"Authentication": "open", "Encryption": "none", "NeedKey": False, "Enterprise": False}
    if "wpa3" in a and re.search(r"personal|개인|sae", a):
        return {"Authentication": "WPA3SAE", "Encryption": "AES", "NeedKey": True, "Enterprise": False}
    if "wpa2" in a and re.search(r"personal|개인", a):
        return {"Authentication": "WPA2PSK", "Encryption": "AES", "NeedKey": True, "Enterprise": False}
    if re.search(r"wpa-personal|wpa 개인", a) and not re.search(r"wpa2|wpa3", a):
        return {"Authentication": "WPAPSK", "Encryption": "AES" if re.search(r"aes|ccmp", e) else "TKIP", "NeedKey": True, "Enterprise": False}
    if "wpa3" in a and re.search(r"enterprise|엔터프라이즈|802\.1x", a):
        return {"Authentication": "WPA3ENT", "Encryption": "AES", "NeedKey": False, "Enterprise": True}
    if re.search(r"enterprise|엔터프라이즈|802\.1x", a):
        return {"Authentication": "WPA2", "Encryption": "AES", "NeedKey": False, "Enterprise": True}
    return {"Authentication": "WPA2PSK", "Encryption": "AES", "NeedKey": True, "Enterprise": False}


def profile_xml_personal(ssid: str, authentication: str, encryption: str, passphrase: str, open_net: bool, hidden: bool) -> str:
    name = xml_escape(ssid)
    nb = "true" if hidden else "false"
    if open_net:
        return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig><SSID><name>{name}</name></SSID><nonBroadcast>{nb}</nonBroadcast></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>auto</connectionMode>
  <MSM><security><authEncryption>
    <authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX>
  </authEncryption></security></MSM>
</WLANProfile>"""
    key = xml_escape(passphrase or "")
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig><SSID><name>{name}</name></SSID><nonBroadcast>{nb}</nonBroadcast></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>auto</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>{authentication}</authentication>
      <encryption>{encryption}</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{key}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>"""


def profile_xml_enterprise(ssid: str, authentication: str, hidden: bool) -> str:
    name = xml_escape(ssid)
    hx = "".join(f"{b:02X}" for b in ssid.encode("utf-8"))
    auth = authentication if authentication in ("WPA2", "WPA3ENT") else "WPA2"
    nb = "true" if hidden else "false"

    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
	<name>{name}</name>
	<SSIDConfig>
		<SSID>
			<hex>{hx}</hex>
			<name>{name}</name>
		</SSID>
		<nonBroadcast>{nb}</nonBroadcast>
	</SSIDConfig>
	<connectionType>ESS</connectionType>
	<connectionMode>auto</connectionMode>
	<autoSwitch>false</autoSwitch>
	<MSM>
		<security>
			<authEncryption>
				<authentication>{auth}</authentication>
				<encryption>AES</encryption>
				<useOneX>true</useOneX>
			</authEncryption>
			<PMKCacheMode>enabled</PMKCacheMode>
			<PMKCacheTTL>720</PMKCacheTTL>
			<PMKCacheSize>128</PMKCacheSize>
			<preAuthMode>disabled</preAuthMode>
			<OneX xmlns="http://www.microsoft.com/networking/OneX/v1">
				<cacheUserData>true</cacheUserData>
				<authMode>user</authMode>
				<EAPConfig>
					<EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
						<EapMethod>
							<Type xmlns="http://www.microsoft.com/provisioning/EapCommon">25</Type>
							<VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
							<VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
							<AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</AuthorId>
						</EapMethod>
						<Config xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
							<Eap xmlns="http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1">
								<Type>25</Type>
								<EapType xmlns="http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV1">
									<ServerValidation>
										<DisableUserPromptForServerValidation>true</DisableUserPromptForServerValidation>
										<ServerNames></ServerNames>
									</ServerValidation>
									<FastReconnect>true</FastReconnect>
									<InnerEapOptional>false</InnerEapOptional>
									<Eap xmlns="http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1">
										<Type>26</Type>
										<EapType xmlns="http://www.microsoft.com/provisioning/MsChapV2ConnectionPropertiesV1">
											<UseWinLogonCredentials>false</UseWinLogonCredentials>
										</EapType>
									</Eap>
									<EnableQuarantineChecks>false</EnableQuarantineChecks>
									<RequireCryptoBinding>false</RequireCryptoBinding>
								</EapType>
							</Eap>
						</Config>
					</EapHostConfig>
				</EAPConfig>
			</OneX>
		</security>
	</MSM>
</WLANProfile>"""


def split_eap_identity(user: str) -> tuple[str, str]:
    raw = (user or "").strip()
    if "\\" in raw:
        domain, name = raw.split("\\", 1)
        return name.strip(), domain.strip()
    return raw, ""


def eap_user_xml(user: str, password: str) -> str:
    name, domain = split_eap_identity(user)
    u = xml_escape(name)
    pw = xml_escape(password)
    dom = xml_escape(domain)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<EapHostUserCredentials xmlns="http://www.microsoft.com/provisioning/EapHostUserCredentials">
  <EapMethod>
    <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">25</Type>
    <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
    <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
    <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</AuthorId>
  </EapMethod>
  <Credentials>
    <Eap xmlns="http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1">
      <Type>25</Type>
      <EapType xmlns="http://www.microsoft.com/provisioning/MsPeapUserPropertiesV1">
        <RoutingIdentity></RoutingIdentity>
        <Eap xmlns="http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1">
          <Type>26</Type>
          <EapType xmlns="http://www.microsoft.com/provisioning/MsChapV2UserPropertiesV1">
            <Username>{u}</Username>
            <Password>{pw}</Password>
            <LogonDomain>{dom}</LogonDomain>
          </EapType>
        </Eap>
      </EapType>
    </Eap>
  </Credentials>
</EapHostUserCredentials>"""


def wifi_interface_names() -> list[str]:
    names: list[str] = []
    for line in netsh_lines(["wlan", "show", "interfaces"]):
        v = first_match(line, [r"(?i)^\s*이름\s*:\s*(.+)$", r"(?i)^\s*Name\s*:\s*(.+)$"])
        if v:
            n = v.strip()
            if n and n not in names:
                names.append(n)
    if not names:
        names = ["Wi-Fi"]
    return names


def netsh_failed(text: str) -> bool:
    if not text:
        return True
    return bool(re.search(r"error|오류|failed|실패|denied|거부|찾을 수 없|손상|corrupted|incorrect|올바르지", text, re.I))


def add_profile_file(xml: str, scopes: tuple[str, ...] = ("all", "current")) -> str:
    tmp = Path(tempfile.gettempdir()) / f"wlan_{uuid.uuid4().hex}.xml"
    tmp.write_text(xml, encoding="utf-8")
    try:
        attempts = []
        for iface in [None, *wifi_interface_names()]:
            for scope in scopes:
                args = ["wlan", "add", "profile", f"filename={tmp}", f"user={scope}"]
                if iface:
                    args.append(f"interface={iface}")
                out = netsh(args).strip()
                attempts.append(out)
                if out and not netsh_failed(out):
                    saved_profiles(refresh=True)
                    return out
        saved_profiles(refresh=True)
        return attempts[-1] if attempts else ""
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def set_eap_user_api(profile: str, xml: str) -> tuple[int, str]:
    if wlanapi is None:
        return -1, "wlanapi.dll 없음"
    try:
        wlanapi.WlanSetProfileEapXmlUserData.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
        ]
        wlanapi.WlanSetProfileEapXmlUserData.restype = wintypes.DWORD
    except Exception:
        pass
    guid = _wlan_first_guid()
    negotiated = wintypes.DWORD()
    handle = ctypes.c_void_p()
    rc_open = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc_open != 0:
        return int(rc_open), f"WlanOpenHandle {rc_open}"
    try:
        rc = int(wlanapi.WlanSetProfileEapXmlUserData(handle, ctypes.byref(guid), profile, 0, xml, None))
        if rc == 0:
            return 0, "WlanSetProfileEapXmlUserData 성공 (dwFlags=0)"
        return rc, f"WlanSetProfileEapXmlUserData 실패 (dwFlags=0, rc={rc})"
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def set_eap_user(profile: str, user: str, password: str) -> str:
    xml = eap_user_xml(user, password)
    api_rc, api_msg = set_eap_user_api(profile, xml)
    if api_rc == 0:
        return api_msg
    raise RuntimeError(f"EAP 계정 주입 실패: {api_msg}")


def apply_enterprise_profile_params(profile: str) -> str:
    out = netsh(["wlan", "set", "profileparameter", f"name={profile}", "authMode=userOnly", "cacheUserData=yes"]).strip()
    return out


def _delete_profile_quiet(ssid: str) -> None:
    prof = resolve_profile(ssid)
    if not prof:
        return
    netsh(["wlan", "delete", "profile", f"name={prof}"])
    saved_profiles(refresh=True)


# ==========================================================
# eap_connect.ps1 호출 함수 (자동 모드)
# ==========================================================
def call_eap_ps1(ssid: str, user: str, password: str, is_wpa3: bool, bssid: str = "") -> str:
    ps_path = ROOT_DIR / "eap_connect.ps1"
    if not ps_path.exists():
        raise RuntimeError(f"eap_connect.ps1 파일을 찾을 수 없습니다: {ps_path}")
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ps_path),
                "-SSID", ssid,
                "-Username", user,
                "-Password", password,
                "-IsWpa3", "1" if is_wpa3 else "0",
                "-Bssid", bssid
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
        output = proc.stdout + proc.stderr
        write_file_log(f"eap_connect.ps1 호출 결과 (BSSID='{bssid}'): {output}", "DEBUG")
        if "SUCCESS|" in output:
            return output.split("SUCCESS|", 1)[1].strip()
        return output
    except subprocess.TimeoutExpired:
        raise RuntimeError("eap_connect.ps1 실행 시간 초과 (30초)")
    except Exception as e:
        raise RuntimeError(f"eap_connect.ps1 실행 오류: {e}")


def recreate_enterprise_profile(ssid: str, net: dict, user: str, password: str, bssid: str = "") -> str:
    hidden = bool(net.get("Hidden"))
    auth = map_auth(net.get("Auth", ""), net.get("Encrypt", "")).get("Authentication") or "WPA2"
    _delete_profile_quiet(ssid)
    out = add_profile_file(profile_xml_enterprise(ssid, auth, hidden), scopes=("all",))
    if not resolve_profile(ssid):
        raise RuntimeError(f"프로필 생성 실패: {out}")
    used = auth
    try:
        eap_result = call_eap_ps1(ssid, user, password, (used == "WPA3ENT"), bssid)
        if "실패" in eap_result or "FAIL" in eap_result:
            raise RuntimeError(f"eap_connect.ps1 실패: {eap_result}")
        return f"프로필({used}): {out} / {eap_result}"
    except RuntimeError as e:
        raise RuntimeError(f"엔터프라이즈 프로필/EAP 계정 저장 실패: {out} / {e}")


def enable_nonbroadcast(profile: str, hidden: bool = True) -> str:
    val = "yes" if hidden else "no"
    return netsh(["wlan", "set", "profileparameter", f"name={profile}", f"nonBroadcast={val}"]).strip()


def add_wifi_profile(ssid: str, net: dict, passphrase: str, user: str) -> str:
    mp = map_auth(net.get("Auth", ""), net.get("Encrypt", ""))
    hidden = bool(net.get("Hidden"))
    if mp["Enterprise"]:
        if not user.strip() or not passphrase:
            raise RuntimeError("엔터프라이즈는 사용자 이름과 비밀번호가 필요합니다.")
        if resolve_profile(ssid):
            return update_wifi_credentials(ssid, net, passphrase, user)
        # [핵심 수정] Enterprise도 선택한 BSSID를 전달하도록 변경
        return recreate_enterprise_profile(ssid, net, user, passphrase, net.get("Bssid", ""))
    if mp["NeedKey"] and not passphrase:
        raise RuntimeError("비밀번호를 입력하세요.")
    xml = profile_xml_personal(ssid, mp["Authentication"], mp["Encryption"], passphrase, not mp["NeedKey"], hidden)
    out = add_profile_file(xml)
    if not resolve_profile(ssid):
        raise RuntimeError(f"프로필 생성 실패: {out}")
    nb = enable_nonbroadcast(ssid, bool(net.get("Hidden")))
    return f"{out} / nonBroadcast={'yes' if net.get('Hidden') else 'no'} {nb}".strip()


def update_wifi_credentials(ssid: str, net: dict, passphrase: str, user: str) -> str:
    mp = map_auth(net.get("Auth", ""), net.get("Encrypt", ""))
    if mp["Enterprise"]:
        if not user.strip() or not passphrase:
            raise RuntimeError("계정 수정은 사용자 ID와 비밀번호를 모두 입력해야 합니다.")
        try:
            eap = set_eap_user(ssid, user, passphrase)
            apply_enterprise_profile_params(ssid)
            return f"기존 프로필에 EAP계정만 갱신: {eap}"
        except RuntimeError as e:
            write_file_log(f"기존 프로필 EAP 갱신 실패 → 프로필 재생성: {e}", "WARN")
            # [핵심 수정] BSSID 전달
            return recreate_enterprise_profile(ssid, net, user, passphrase, net.get("Bssid", ""))
    if mp["NeedKey"]:
        if len(passphrase) < 8:
            raise RuntimeError("Passphrase는 8자리 이상이어야 합니다.")
        out = add_profile_file(profile_xml_personal(ssid, mp["Authentication"], mp["Encryption"], passphrase, False, bool(net.get("Hidden"))))
        if not resolve_profile(ssid):
            raise RuntimeError(f"프로필 갱신 실패: {out}")
        return out
    return "Open 네트워크는 자격 증명이 없습니다."


# ---------------------------------------------------------------------------
# wlanapi 설정 (클래스 정의 후에 배치)
# ---------------------------------------------------------------------------
wlanapi = ctypes.WinDLL("wlanapi.dll") if os.name == "nt" else None


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NDIS_OBJECT_HEADER(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Type", ctypes.c_ubyte), ("Revision", ctypes.c_ubyte), ("Size", ctypes.c_ushort)]


class DOT11_BSSID_LIST(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Header", NDIS_OBJECT_HEADER), ("uNumOfEntries", wintypes.DWORD), ("uTotalNumOfEntries", wintypes.DWORD), ("BSSID", ctypes.c_ubyte * 6)]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_RAW_DATA(ctypes.Structure):
    _fields_ = [("dwDataSize", wintypes.DWORD), ("DataBlob", ctypes.c_ubyte * 1)]


class WLAN_CONNECTION_PARAMETERS(ctypes.Structure):
    _fields_ = [("wlanConnectionMode", wintypes.DWORD), ("strProfile", wintypes.LPCWSTR), ("pDot11Ssid", ctypes.c_void_p), ("pDesiredBssidList", ctypes.c_void_p), ("dot11BssType", wintypes.DWORD), ("dwFlags", wintypes.DWORD)]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", ctypes.c_wchar * 256), ("isState", wintypes.DWORD)]


# 클래스 정의 후 wlanapi 설정
if wlanapi:
    wlanapi.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p)]
    wlanapi.WlanOpenHandle.restype = wintypes.DWORD
    wlanapi.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    wlanapi.WlanCloseHandle.restype = wintypes.DWORD
    wlanapi.WlanEnumInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    wlanapi.WlanEnumInterfaces.restype = wintypes.DWORD
    wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    wlanapi.WlanFreeMemory.restype = wintypes.DWORD
    wlanapi.WlanConnect.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p]
    wlanapi.WlanConnect.restype = wintypes.DWORD


def _wlan_first_guid() -> GUID:
    if not wlanapi:
        raise RuntimeError("wlanapi를 사용할 수 없습니다.")
    negotiated = wintypes.DWORD()
    handle = ctypes.c_void_p()
    rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        raise RuntimeError(f"WlanOpenHandle {rc}")
    p_list = ctypes.c_void_p()
    try:
        rc = wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(p_list))
        if rc != 0 or not p_list.value:
            raise RuntimeError(f"WlanEnumInterfaces {rc}")
        count = ctypes.cast(p_list, ctypes.POINTER(wintypes.DWORD)).contents.value
        if count == 0:
            raise RuntimeError("Wi-Fi 인터페이스 없음")
        info_addr = p_list.value + 8
        info = ctypes.cast(info_addr, ctypes.POINTER(WLAN_INTERFACE_INFO)).contents
        return info.InterfaceGuid
    finally:
        if p_list.value:
            wlanapi.WlanFreeMemory(p_list)
        wlanapi.WlanCloseHandle(handle, None)


def wlan_connect_profile(profile: str) -> int:
    guid = _wlan_first_guid()
    negotiated = wintypes.DWORD()
    handle = ctypes.c_void_p()
    rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        return rc
    try:
        cp = WLAN_CONNECTION_PARAMETERS()
        cp.wlanConnectionMode = 0
        cp.strProfile = profile
        cp.pDot11Ssid = None
        cp.pDesiredBssidList = None
        cp.dot11BssType = 1
        cp.dwFlags = 0x00000001  # WLAN_CONNECTION_HIDDEN_NETWORK
        return int(wlanapi.WlanConnect(handle, ctypes.byref(guid), ctypes.byref(cp), None))
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def wlan_connect_bssid(profile: str, bssid: str, ssid: str = "") -> int:
    guid = _wlan_first_guid()
    negotiated = wintypes.DWORD()
    handle = ctypes.c_void_p()
    rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
    if rc != 0:
        return rc
    try:
        bl = DOT11_BSSID_LIST()
        bl.Header.Type = 0x80
        bl.Header.Revision = 1
        bl.Header.Size = ctypes.sizeof(DOT11_BSSID_LIST)
        bl.uNumOfEntries = 1
        bl.uTotalNumOfEntries = 1
        raw = mac_bytes(bssid)
        for i, b in enumerate(raw):
            bl.BSSID[i] = b
        ssid_obj = DOT11_SSID()
        p_ssid = None
        if ssid:
            raw_s = ssid.encode("utf-8")[:32]
            ssid_obj.uSSIDLength = len(raw_s)
            for i, b in enumerate(raw_s):
                ssid_obj.ucSSID[i] = b
            p_ssid = ctypes.cast(ctypes.byref(ssid_obj), ctypes.c_void_p)
        cp = WLAN_CONNECTION_PARAMETERS()
        cp.wlanConnectionMode = 0
        cp.strProfile = profile
        cp.pDot11Ssid = p_ssid
        cp.pDesiredBssidList = ctypes.cast(ctypes.byref(bl), ctypes.c_void_p)
        cp.dot11BssType = 1
        cp.dwFlags = 0x00000001  # WLAN_CONNECTION_HIDDEN_NETWORK
        return int(wlanapi.WlanConnect(handle, ctypes.byref(guid), ctypes.byref(cp), None))
    finally:
        wlanapi.WlanCloseHandle(handle, None)


def wlan_disconnect() -> tuple[str, int]:
    out = netsh(["wlan", "disconnect"]).strip()
    rc = -1
    try:
        guid = _wlan_first_guid()
        negotiated = wintypes.DWORD()
        handle = ctypes.c_void_p()
        rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle))
        if rc == 0:
            try:
                rc = int(wlanapi.WlanDisconnect(handle, ctypes.byref(guid), None))
            finally:
                wlanapi.WlanCloseHandle(handle, None)
    except Exception:
        rc = -1
    return out, rc


def delete_profile(ssid: str) -> str:
    prof = resolve_profile(ssid)
    if not prof:
        raise RuntimeError(f"삭제할 프로필이 없습니다: {ssid}")
    notes: list[str] = []
    cur = current_wifi()
    using = (cur.get("Ssid") or "") == ssid or (cur.get("Profile") or "") == prof
    if using and (is_connected(cur) or is_authenticating(cur)):
        notes.append("접속 중 프로필 삭제 → Windows가 연결을 끊습니다")
    out = netsh(["wlan", "delete", "profile", f"name={prof}"]).strip()
    notes.append(out)
    saved_profiles(refresh=True)
    if resolve_profile(ssid):
        raise RuntimeError(f"프로필 삭제 실패: {' / '.join(notes)}")
    return " / ".join(x for x in notes if x)


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
def limit_logs() -> list[Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(LOG_DIR.glob("wifi-*.log"), key=lambda p: (p.stat().st_mtime, p.name))
    while len(files) > MAX_LOG_FILES:
        old = files.pop(0)
        try:
            old.unlink()
        except OSError:
            pass
    return files


def init_log() -> Path:
    global LOG_FILE
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE = LOG_DIR / f"wifi-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    header = (
        "============================================================\n"
        f"HSITX Wi-Fi 접속런처 {APP_VERSION}\n"
        f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"LogFile: {LOG_FILE}\n"
        "============================================================\n"
    )
    LOG_FILE.write_text(header, encoding="utf-8")
    limit_logs()
    return LOG_FILE


def write_file_log(msg: str, level: str = "INFO") -> None:
    if not LOG_FILE:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}][{level}] {msg}\n")
    except OSError:
        pass


def format_debug(cur: dict) -> str:
    return (
        f"state='{cur.get('State')}' connected={is_connected(cur)} "
        f"ssid='{cur.get('Ssid')}' bssid='{cur.get('Bssid')}' "
        f"signal='{cur.get('Signal')}' ch='{cur.get('Channel')}' "
        f"radio='{cur.get('Radio')}' profile='{cur.get('Profile')}'"
    )


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
CLR_SCAN_BG, CLR_SCAN_FG = "#ffffff", "#5a5a5a"
CLR_PROF_BG, CLR_PROF_FG = "#e8f2ff", "#004696"
CLR_CONN_BG, CLR_CONN_FG = "#c6ebc6", "#005a28"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"HSITX Wi-Fi 접속런처 {APP_VERSION}")
        self.geometry("980x680")
        self.minsize(860, 520)
        self.last_bssid = ""
        self.busy = False
        self._loc_warned = False
        self._scan_running = False
        self._all_nets: list[dict] = []
        self._last_cur: dict | None = None
        self._build()
        self.after(200, self._on_shown)
        self.after(500, self._silent_update_check)

    def _build(self) -> None:
        upd = tk.Frame(self)
        upd.pack(fill="x", padx=8, pady=(6, 0))
        self.btn_check_upd = tk.Button(upd, text="업데이트 확인", command=self.on_check_update)
        self.btn_check_upd.pack(side="left")
        self.lbl_upd = tk.Label(upd, text="", fg="#666666")
        self.lbl_upd.pack(side="left", padx=10)
        self.btn_apply_upd = tk.Button(
            upd,
            text="업데이트",
            command=self._do_update,
            bg="#d9534f",
            fg="white",
            activebackground="#c9302c",
            activeforeground="white",
        )

        self.lbl_now = tk.Label(self, text="현재 연결: 확인 중...", anchor="w", padx=10, pady=6)
        self.lbl_now.pack(fill="x")

        bar = tk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(4, 0))
        self.btn_scan = tk.Button(bar, text="다시 스캔", width=10, command=self.refresh_list)
        self.btn_scan.pack(side="left", padx=2)
        self.btn_connect = tk.Button(bar, text="선택 AP 접속", width=12, command=self.do_connect)
        self.btn_connect.pack(side="left", padx=2)
        self.btn_disc = tk.Button(bar, text="연결 끊기", width=10, command=self.do_disconnect)
        self.btn_disc.pack(side="left", padx=2)
        self.btn_del = tk.Button(bar, text="프로필 삭제", width=10, command=self.do_delete)
        self.btn_del.pack(side="left", padx=2)
        self.var_auto = tk.BooleanVar(value=False)
        self.chk_auto = tk.Checkbutton(bar, text="자동갱신", variable=self.var_auto, command=self._on_auto_changed)
        self.chk_auto.pack(side="left", padx=(10, 2))
        self.var_interval = tk.StringVar(value="8")
        self.cmb_interval = ttk.Combobox(bar, textvariable=self.var_interval, values=("3", "5", "8", "10", "15", "30", "60"), width=4, state="readonly")
        self.cmb_interval.pack(side="left")
        self.cmb_interval.bind("<<ComboboxSelected>>", lambda e: self._reschedule_tick())
        tk.Label(bar, text="초").pack(side="left", padx=(2, 8))
        self.var_debug = tk.BooleanVar(value=True)
        self.chk_debug = tk.Checkbutton(bar, text="디버그", variable=self.var_debug)
        self.chk_debug.pack(side="left")

        filt = tk.Frame(self)
        filt.pack(fill="x", padx=8, pady=4)
        tk.Label(filt, text="SSID").pack(side="left")
        self.var_ssid_pick = tk.StringVar(value="(전체)")
        self.cmb_ssid = ttk.Combobox(filt, textvariable=self.var_ssid_pick, width=22, state="readonly")
        self.cmb_ssid["values"] = ("(전체)",)
        self.cmb_ssid.pack(side="left", padx=4)
        self.cmb_ssid.bind("<<ComboboxSelected>>", lambda e: self._on_ssid_pick())
        self.btn_ssid_only = tk.Button(filt, text="선택 SSID만", command=self.filter_selected_ssid)
        self.btn_ssid_only.pack(side="left", padx=2)
        self.btn_ssid_all = tk.Button(filt, text="전체 보기", command=self.clear_ssid_filter)
        self.btn_ssid_all.pack(side="left", padx=2)
        tk.Label(filt, text="검색").pack(side="left", padx=(12, 2))
        self.var_search = tk.StringVar()
        self.txt_search = tk.Entry(filt, textvariable=self.var_search, width=18)
        self.txt_search.pack(side="left")
        self.var_search.trace_add("write", lambda *_: self.refill_tree())

        cred = tk.Frame(self)
        cred.pack(fill="x", padx=8, pady=(0, 2))
        # [수정] 접속 SSID 열 높이 고정 (30px)
        cred.config(height=30)
        cred.pack_propagate(False)

        self.lbl_ssid = tk.Label(cred, text="접속 SSID")
        self.lbl_ssid.pack(side="left")
        self.var_ssid = tk.StringVar()
        self.txt_ssid = tk.Entry(cred, textvariable=self.var_ssid, width=16)
        self.txt_ssid.pack(side="left", padx=4)
        self.btn_reveal = tk.Button(cred, text="저장된 암호확인", command=self.do_reveal)
        self.frm_user = tk.Frame(cred)
        tk.Label(self.frm_user, text="사용자 ID").pack(side="left")
        self.var_user = tk.StringVar()
        self.txt_user = tk.Entry(self.frm_user, textvariable=self.var_user, width=14)
        self.txt_user.pack(side="left", padx=4)
        self.lbl_pw = tk.Label(cred, text="암호")
        self.var_pw = tk.StringVar()
        self.txt_pw = tk.Entry(cred, textvariable=self.var_pw, width=16, show="*")
        self.var_show = tk.BooleanVar(value=False)
        self.chk_show = tk.Checkbutton(cred, text="표시", variable=self.var_show, command=self._toggle_pw)
        self.var_hidden = tk.BooleanVar(value=False)
        self.chk_hidden = tk.Checkbutton(cred, text="숨김SSID", variable=self.var_hidden)
        self.lbl_eap = tk.Label(cred, text="암호화")
        self.txt_eap = tk.Entry(cred, width=16)
        self.txt_eap.insert(0, "PEAP-MSCHAPv2")
        self.txt_eap.configure(state="disabled")
        self.cred_row = cred
        self.cred_widgets = [self.btn_reveal, self.frm_user, self.lbl_pw, self.txt_pw, self.chk_show, self.chk_hidden, self.lbl_eap, self.txt_eap]
        for w in self.cred_widgets:
            w.pack_forget()

        leg = tk.Frame(self)
        leg.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(leg, text="범례", fg="#696969").pack(side="left", padx=(0, 6))
        tk.Label(leg, text=" 검색된 SSID ", bg=CLR_SCAN_BG, fg=CLR_SCAN_FG, relief="solid", bd=1).pack(side="left", padx=4)
        tk.Label(leg, text=" 프로필 저장됨 ", bg=CLR_PROF_BG, fg=CLR_PROF_FG, relief="solid", bd=1).pack(side="left", padx=4)
        tk.Label(leg, text=" 접속 중 ", bg=CLR_CONN_BG, fg=CLR_CONN_FG, relief="solid", bd=1).pack(side="left", padx=4)
        self.lbl_mode = tk.Label(leg, text="AP를 선택하세요", fg="#696969", anchor="w")
        self.lbl_mode.pack(side="left", padx=12, fill="x", expand=True)

        cols = ("ssid", "bssid", "sig", "ch", "band", "radio", "auth", "prof", "now")
        listf = tk.Frame(self)
        listf.pack(fill="both", expand=True, padx=8)
        self.tree = ttk.Treeview(listf, columns=cols, show="headings", selectmode="browse")
        headers = [("ssid", "SSID", 200), ("bssid", "BSSID", 140), ("sig", "신호", 60), ("ch", "채널", 50), ("band", "Band", 70), ("radio", "무선 규격", 90), ("auth", "인증", 140), ("prof", "프로필", 60), ("now", "현재", 70)]
        self._sort_col = "ssid"
        self._sort_desc = False
        for key, title, w in headers:
            self.tree.heading(key, text=title, command=lambda c=key: self.sort_tree(c))
            self.tree.column(key, width=w, minwidth=50, anchor="w", stretch=True)
        vsb = ttk.Scrollbar(listf, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(listf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        listf.grid_rowconfigure(0, weight=1)
        listf.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("scan", background=CLR_SCAN_BG, foreground=CLR_SCAN_FG)
        self.tree.tag_configure("prof", background=CLR_PROF_BG, foreground=CLR_PROF_FG)
        self.tree.tag_configure("conn", background=CLR_CONN_BG, foreground=CLR_CONN_FG)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_cred_ui())
        self.tree.bind("<Double-1>", lambda e: self.do_connect())

        logf = tk.Frame(self)
        logf.pack(fill="x", padx=8, pady=4)
        tk.Button(logf, text="로그폴더보기", command=self.open_logs).pack(side="left")
        tk.Button(logf, text="최근로그 다운받기", command=self.save_logs).pack(side="left", padx=6)
        self.lbl_logpath = tk.Label(logf, text="로그: logs\\", fg="#696969")
        self.lbl_logpath.pack(side="left")

        logbox = tk.Frame(self)
        logbox.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.txt_log = tk.Text(logbox, height=12, bg="#1e1e1e", fg="#dcdcdc", state="disabled", wrap="none")
        log_ys = ttk.Scrollbar(logbox, orient="vertical", command=self.txt_log.yview)
        log_xs = ttk.Scrollbar(logbox, orient="horizontal", command=self.txt_log.xview)
        self.txt_log.configure(yscrollcommand=log_ys.set, xscrollcommand=log_xs.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        log_ys.grid(row=0, column=1, sticky="ns")
        log_xs.grid(row=1, column=0, sticky="ew")
        logbox.grid_rowconfigure(0, weight=1)
        logbox.grid_columnconfigure(0, weight=1)

        self.rows: dict[str, dict] = {}
        self._tick_id = None
        self._reschedule_tick()

    def ui_log(self, msg: str, level: str = "INFO") -> None:
        write_file_log(msg, level)
        if level == "DEBUG" and not self.var_debug.get():
            return
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}][{level}] {msg}\n"
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        self.update_idletasks()

    def _toggle_pw(self) -> None:
        self.txt_pw.configure(show="" if self.var_show.get() else "*")

    def _on_shown(self) -> None:
        p = init_log()
        self.lbl_logpath.configure(text=f"로그: {p}")
        self.ui_log(f"세션 로그 시작 {APP_VERSION}")
        self.check_location_permission(open_settings=True)
        self.refresh_list(force=True)

    def _silent_update_check(self) -> None:
        threading.Thread(target=self._check_update_worker, args=(False,), daemon=True).start()

    def on_check_update(self) -> None:
        self.lbl_upd.configure(text="업데이트 확인 중...")
        threading.Thread(target=self._check_update_worker, args=(True,), daemon=True).start()

    def _check_update_worker(self, prompt: bool) -> None:
        if gh_updater is None:
            self.after(0, lambda: self.lbl_upd.configure(text="updater.py 없음"))
            return
        frozen = bool(getattr(sys, "frozen", False))
        info = gh_updater.check_update(
            ROOT_DIR,
            frozen=frozen,
            current_version=APP_VERSION,
            exe_path=sys.executable if frozen else "",
        )
        self._update_info = info
        self.after(0, lambda: self._show_update(info, prompt))

    def _set_apply_visible(self, show: bool) -> None:
        if show:
            if not self.btn_apply_upd.winfo_ismapped():
                self.btn_apply_upd.pack(side="left", padx=6)
        else:
            self.btn_apply_upd.pack_forget()

    def _show_update(self, info: dict, prompt: bool) -> None:
        if not info.get("ok"):
            self.lbl_upd.configure(text=info.get("message") or "업데이트 확인 실패")
            self._set_apply_visible(False)
            if prompt:
                messagebox.showerror("업데이트", info.get("message") or "업데이트 확인 실패")
            return
        if info.get("available"):
            self.lbl_upd.configure(text=info.get("message") or "새 버전이 있습니다.")
            self._set_apply_visible(True)
        else:
            self.lbl_upd.configure(text="최신 버전입니다.")
            self._set_apply_visible(False)

    def _do_update(self) -> None:
        if gh_updater is None:
            messagebox.showerror("업데이트", "updater.py 가 없습니다.")
            return
        info = getattr(self, "_update_info", None) or {}
        frozen = bool(getattr(sys, "frozen", False))
        self.lbl_upd.configure(text="업데이트 받는 중...")
        self._set_apply_visible(False)

        def work() -> None:
            result = gh_updater.apply_update(
                ROOT_DIR,
                frozen=frozen,
                exe_path=sys.executable if frozen else "",
                info=info,
                expected_sha=info.get("remote") or "",
            )
            self.after(0, lambda: self._after_update(result, frozen))

        threading.Thread(target=work, daemon=True).start()

    def _after_update(self, result: dict, frozen: bool) -> None:
        if not result.get("ok"):
            self.lbl_upd.configure(text=result.get("message") or "업데이트 실패")
            self._set_apply_visible(True)
            messagebox.showerror("업데이트", result.get("message") or "업데이트 실패")
            return
        self.lbl_upd.configure(text=result.get("message") or "업데이트 완료")
        bat = result.get("replace_bat")
        if bat:
            try:
                gh_updater.launch_replace_bat(bat, ROOT_DIR)
            except Exception as exc:
                self.lbl_upd.configure(text=str(exc))
                return
            self.destroy()
            return
        messagebox.showinfo("업데이트", result.get("message") or "소스 업데이트 완료. 다시 실행하세요.")
        self.destroy()

    def check_location_permission(self, open_settings: bool = False) -> bool:
        raw = netsh(["wlan", "show", "interfaces"]) + "\n" + netsh(["wlan", "show", "networks", "mode=bssid"])
        if not location_permission_blocked(raw):
            return True
        self.ui_log("위치 서비스가 꺼져 있어 netsh Wi-Fi 스캔/BSSID 조회가 제한됩니다.", "WARN")
        if self._loc_warned and not open_settings:
            return False
        self._loc_warned = True
        messagebox.showwarning("위치 서비스 필요", "Windows가 Wi-Fi 검색과 BSSID 조회에 위치 권한을 요구합니다.\n\n설정 > 개인 정보 보호 및 보안 > 위치 에서\n위치 서비스를 켜야 이 프로그램을 정상적으로 사용할 수 있습니다.\n\n위치 설정 화면을 엽니다.")
        try:
            os.startfile("ms-settings:privacy-location")
        except Exception as e:
            self.ui_log(f"위치 설정 화면을 열지 못했습니다: {e}", "ERR")
        return False

    def _interval_ms(self) -> int:
        try:
            sec = int(self.var_interval.get())
        except Exception:
            sec = 8
        if sec not in (3, 5, 8, 10, 15, 30, 60):
            sec = 8
        return sec * 1000

    def _on_auto_changed(self) -> None:
        if self.var_auto.get():
            self.ui_log(f"자동갱신 ON ({self.var_interval.get()}초)")
            if not self.busy:
                self.refresh_list(force=True, reason="자동갱신")
        else:
            self.ui_log("자동갱신 OFF")
        self._reschedule_tick()

    def _reschedule_tick(self) -> None:
        if self._tick_id is not None:
            try:
                self.after_cancel(self._tick_id)
            except Exception:
                pass
        self._tick_id = self.after(self._interval_ms(), self._tick)

    def _tick(self) -> None:
        if self.var_auto.get() and not self.busy and not self._scan_running:
            self.refresh_list(force=True, reason="자동갱신")
        elif self.var_auto.get() and self._scan_running:
            self.ui_log("이전 스캔이 끝나지 않아 이번 자동갱신은 건너뜁니다")
        else:
            try:
                self.update_now()
            except Exception:
                pass
        self._tick_id = self.after(self._interval_ms(), self._tick)

    def selected_net(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.rows.get(sel[0])

    def effective_ssid(self, n: dict | None) -> str:
        typed = (self.var_ssid.get() or "").strip()
        if typed == "(숨김)":
            typed = ""
        hidden = bool(n and (n.get("Hidden") or not (n.get("Ssid") or "").strip()))
        if hidden:
            return typed
        if n and (n.get("Ssid") or "").strip():
            return n["Ssid"].strip()
        return typed

    def clear_creds(self) -> None:
        self.var_pw.set("")
        self.var_user.set("")
        self.var_show.set(False)
        self._toggle_pw()

    def hide_creds(self) -> None:
        for w in self.cred_widgets:
            w.pack_forget()

    def _pack_cred(self, w: tk.Widget, **kw) -> None:
        w.pack(side="left", padx=4, anchor="center", **kw)

    def update_now(self) -> dict:
        cur = current_wifi()
        if is_connected(cur):
            self.lbl_now.configure(text=f"현재 연결: SSID={cur['Ssid']}   BSSID={cur['Bssid']}   신호={cur['Signal']}   CH={cur['Channel']}   {cur['Radio']}", fg="#006400")
        else:
            st = (cur.get("State") or "").strip() or "알 수 없음"
            if re.search(r"하드웨어\s*켬|hardware\s*on", st, re.I):
                st = "하드웨어 켬, 접속된 SSID 없음"
            elif "접속된 SSID 없음" not in st:
                st = f"{st}, 접속된 SSID 없음"
            self.lbl_now.configure(text=f"현재 연결: {st}", fg="#696969")
        return cur

    def update_cred_ui(self) -> None:
        n = self.selected_net()
        self.hide_creds()
        if not n:
            self.lbl_mode.configure(text="AP를 선택하세요")
            self.txt_ssid.configure(state="normal")
            self.clear_creds()
            self.last_bssid = ""
            return
        hidden = bool(n.get("Hidden") or not (n.get("Ssid") or "").strip())
        key = n.get("Bssid") or ""
        changed = key != self.last_bssid
        self.last_bssid = key
        if changed:
            self.clear_creds()
            self.var_hidden.set(hidden)
        if hidden:
            self.txt_ssid.configure(state="normal", bg="#ffffe0")
            if changed:
                self.var_ssid.set("")
        else:
            self.var_ssid.set(n.get("Ssid") or "")
            self.txt_ssid.configure(state="readonly", readonlybackground="#dcdcdc")
        ssid = self.effective_ssid(n)
        mp = map_auth(n.get("Auth", ""), n.get("Encrypt", ""))
        has = bool(resolve_profile(ssid))
        tag = "숨김 SSID / " if hidden else ""

        if mp["Enterprise"]:
            auth_type_label = "Enterprise"
        elif mp["NeedKey"]:
            auth_type_label = "Personal"
        else:
            auth_type_label = "Open"

        self.lbl_mode.configure(text=f"{tag}{auth_type_label} / {'프로필 있음' if has else '프로필 없음'}")

        if mp["Enterprise"]:
            self._pack_cred(self.frm_user)
            self._pack_cred(self.lbl_pw)
            self._pack_cred(self.txt_pw)
            self._pack_cred(self.chk_show)
            self._pack_cred(self.chk_hidden)
            self._pack_cred(self.lbl_eap)
            self._pack_cred(self.txt_eap)
        elif mp["NeedKey"]:
            if has:
                self._pack_cred(self.btn_reveal)
            self._pack_cred(self.lbl_pw)
            self._pack_cred(self.txt_pw)
            self._pack_cred(self.chk_show)
            self._pack_cred(self.chk_hidden)
        else:
            self._pack_cred(self.chk_hidden)

    def _sort_key(self, n: dict, col: str):
        if col == "sig":
            m = re.search(r"\d+", n.get("Signal") or "")
            return (0, int(m.group(0)) if m else -1)
        if col == "ch":
            m = re.search(r"\d+", n.get("Channel") or "")
            return (0, int(m.group(0)) if m else -1)
        if col == "prof":
            return (0, 1 if n.get("HasProfile") else 0)
        if col == "now":
            return (0, 1)
        mapping = {"ssid": "Ssid", "bssid": "Bssid", "radio": "Radio", "band": "Band", "auth": "Auth"}
        val = (n.get(mapping.get(col, col), "") or "").casefold()
        return (0 if val else 1, val)

    def apply_sort(self) -> None:
        items = []
        col = self._sort_col
        for iid in self.tree.get_children(""):
            n = self.rows.get(iid, {})
            items.append((self._sort_key(n, col), iid))
        items.sort(reverse=self._sort_desc)
        for idx, (_, iid) in enumerate(items):
            self.tree.move(iid, "", idx)

    def sort_tree(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = col in ("sig", "now", "prof")
        self.apply_sort()

    def _ssid_name(self, n: dict) -> str:
        if n.get("Hidden") or not (n.get("Ssid") or "").strip():
            return "(숨김)"
        return n["Ssid"]

    def _visible_nets(self) -> list[dict]:
        pick = (self.var_ssid_pick.get() or "(전체)").strip()
        q = (self.var_search.get() or "").strip().casefold()
        out = []
        for n in self._all_nets:
            name = self._ssid_name(n)
            if pick and pick != "(전체)" and name != pick:
                continue
            blob = f"{name} {n.get('Bssid') or ''} {n.get('Auth') or ''}".casefold()
            if q and q not in blob:
                continue
            out.append(n)
        return out

    def _refresh_ssid_combo(self) -> None:
        names = []
        for n in self._all_nets:
            s = self._ssid_name(n)
            if s not in names:
                names.append(s)
        names.sort(key=lambda x: (x == "(숨김)", x.casefold()))
        values = ["(전체)", *names]
        cur = self.var_ssid_pick.get() or "(전체)"
        self.cmb_ssid["values"] = values
        if cur not in values:
            self.var_ssid_pick.set("(전체)")

    def _on_ssid_pick(self) -> None:
        pick = self.var_ssid_pick.get()
        self.ui_log(f"SSID 필터: {pick}")
        self.refill_tree()

    def filter_selected_ssid(self) -> None:
        n = self.selected_net()
        if not n:
            messagebox.showinfo("안내", "목록에서 SSID를 선택하세요.")
            return
        self.var_ssid_pick.set(self._ssid_name(n))
        self._on_ssid_pick()

    def clear_ssid_filter(self) -> None:
        self.var_ssid_pick.set("(전체)")
        self.var_search.set("")
        self.ui_log("SSID 필터 해제")
        self.refill_tree()

    def apply_link_to_tree(self, cur: dict | None = None) -> None:
        cur = cur or self._last_cur or {}
        self._last_cur = cur
        connected = is_connected(cur)
        cur_mac = normalize_mac(cur.get("Bssid") or "") if connected else ""
        for iid, n in list(self.rows.items()):
            vals = list(self.tree.item(iid, "values"))
            if len(vals) < 9:
                continue
            ssid = n.get("Ssid") or ""
            has = bool(resolve_profile(ssid)) if ssid else False
            n["HasProfile"] = has
            is_cur = bool(cur_mac and normalize_mac(n.get("Bssid") or "") == cur_mac)
            vals[7] = "있음" if has else "없음"
            vals[8] = "접속중" if is_cur else ""
            tag = "conn" if is_cur else ("prof" if has else "scan")
            self.tree.item(iid, values=vals, tags=(tag,))

    def refill_tree(self, prev: str | None = None) -> None:
        cur = self._last_cur or {}
        if prev is None:
            sel = self.tree.selection()
            if sel:
                prev = self.rows.get(sel[0], {}).get("Bssid")
        nets = self._visible_nets()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows.clear()
        keep = None
        for n in nets:
            is_cur = bool(is_connected(cur) and cur.get("Bssid") and normalize_mac(cur["Bssid"]) == normalize_mac(n.get("Bssid") or ""))
            ssid = self._ssid_name(n)
            tag = "conn" if is_cur else ("prof" if n.get("HasProfile") else "scan")
            iid = self.tree.insert("", "end", values=(ssid, n["Bssid"], n.get("Signal", ""), n.get("Channel", ""), format_band(n.get("Band", "")), format_wifi_phy(n.get("Radio", "")), n.get("Auth", ""), "있음" if n.get("HasProfile") else "없음", "접속중" if is_cur else ""), tags=(tag,))
            self.rows[iid] = n
            if prev and n["Bssid"] == prev:
                keep = iid
        if keep:
            self.tree.selection_set(keep)
            self.tree.see(keep)
        self.apply_sort()

    def refresh_list(self, force: bool = False, reason: str = "") -> None:
        if self._scan_running:
            return
        if self.busy and not force:
            return
        prev = None
        sel = self.tree.selection()
        if sel:
            prev = self.rows.get(sel[0], {}).get("Bssid")
        self._scan_running = True
        tag = reason or "수동"
        self.ui_log(f"스캔 중... ({tag})")

        def work() -> None:
            err = None
            cur = None
            nets: list[dict] = []
            try:
                cur = current_wifi()
                write_file_log("scan current: " + format_debug(cur), "DEBUG")
                if cur.get("Raw"):
                    write_file_log("netsh wlan show interfaces\r\n" + cur["Raw"], "DEBUG")
                nets = nearby_networks(do_scan=True)
                write_file_log(f"scan ap_count={len(nets)}", "DEBUG")
            except Exception as e:
                err = e
            self.after(0, lambda: self._apply_scan(cur, nets, prev, err))

        threading.Thread(target=work, daemon=True).start()

    def _apply_scan(self, cur: dict | None, nets: list[dict], prev: str | None, err: Exception | None) -> None:
        self._scan_running = False
        if err:
            self.ui_log(str(err), "ERR")
            return
        if cur is None:
            return
        try:
            if is_connected(cur):
                self.lbl_now.configure(text=f"현재 연결: SSID={cur['Ssid']}   BSSID={cur['Bssid']}   신호={cur['Signal']}   CH={cur['Channel']}   {cur['Radio']}", fg="#006400")
            else:
                self.update_now()
            if location_permission_blocked(cur.get("Raw") or ""):
                self.check_location_permission(open_settings=False)
            self._all_nets = nets
            self._last_cur = cur
            self._refresh_ssid_combo()
            self.refill_tree(prev)
            conn_iid = None
            for iid, nn in self.rows.items():
                if is_connected(cur) and cur.get("Bssid") and normalize_mac(cur["Bssid"]) == normalize_mac(nn.get("Bssid") or ""):
                    conn_iid = iid
                    break
            if conn_iid:
                self.tree.see(conn_iid)
            shown = len(self.rows)
            pick = self.var_ssid_pick.get() or "(전체)"
            extra = f", 표시 {shown} / 필터 {pick}" if shown != len(nets) or pick != "(전체)" else ""
            self.ui_log(f"AP {len(nets)}개 갱신 {datetime.now().strftime('%H:%M:%S')}{extra}")
            self.update_cred_ui()
        except Exception as e:
            self.ui_log(str(e), "ERR")

    def do_connect(self) -> None:
        n = self.selected_net()
        if not n:
            messagebox.showinfo("안내", "목록에서 BSSID를 선택하세요.")
            return
        ssid = self.effective_ssid(n)
        if not ssid or ssid == "(숨김)":
            messagebox.showinfo("안내", "숨김 AP입니다. 아래 SSID 칸에 실제 SSID를 입력한 뒤 접속하세요.")
            self.txt_ssid.focus_set()
            return
        n["Ssid"] = ssid
        n["Hidden"] = bool(self.var_hidden.get())
        if n["Hidden"]:
            HIDDEN_SSID[n["Bssid"]] = ssid
        mp = map_auth(n.get("Auth", ""), n.get("Encrypt", ""))
        pw = self.var_pw.get()
        user = self.var_user.get()
        self.ui_log(f"connect start ssid='{ssid}' bssid={n['Bssid']} hidden={n.get('Hidden')} auth={n.get('Auth')} enc={n.get('Encrypt')} user='{user.strip()}' pw_len={len(pw)} profile={resolve_profile(ssid) or '-'}", "DEBUG")
        self.ui_log("interfaces: " + format_debug(current_wifi()), "DEBUG")
        self.busy = True
        self.btn_connect.configure(state="disabled")
        try:
            prof = resolve_profile(ssid)
            if not prof:
                if mp["Enterprise"] and (not user.strip() or not pw):
                    raise RuntimeError("WPA2/WPA3-Enterprise는 사용자 ID와 비밀번호가 필요합니다.")
                if mp["NeedKey"] and len(pw) < 8:
                    raise RuntimeError("Personal Passphrase는 8자리 이상이어야 합니다.")
                if mp["Enterprise"]:
                    self.ui_log("저장된 프로필 없음 → PEAP-MSCHAPv2 프로필을 만들고 EAP 계정을 넣은 뒤 접속합니다")
                else:
                    self.ui_log("저장된 프로필 없음 → 입력한 Passphrase로 프로필을 만들고 최초 접속을 시도합니다")
                self.ui_log(add_wifi_profile(ssid, n, pw, user))
                prof = resolve_profile(ssid)
                if not prof:
                    raise RuntimeError("프로필을 만들지 못했습니다.")
                self.ui_log("프로필 생성 완료 → 지정 BSSID로 접속합니다")
            else:
                need_update = False
                if mp["Enterprise"]:
                    has_id = bool(user.strip())
                    has_pw = bool(pw)
                    if has_id ^ has_pw:
                        raise RuntimeError("계정을 바꾸려면 사용자 ID와 암호를 모두 입력하세요.")
                    if has_id and has_pw:
                        need_update = True
                    else:
                        self.ui_log(f"저장된 802.1X 프로필로 BSSID 접속: {prof} / {n['Bssid']}")
                elif mp["NeedKey"]:
                    if not pw:
                        self.ui_log(f"저장된 Personal 프로필로 BSSID 접속: {prof} / {n['Bssid']}")
                    elif len(pw) >= 8:
                        need_update = True
                    else:
                        raise RuntimeError("변경할 Personal Passphrase는 8자리 이상이어야 합니다.")
                if need_update:
                    if mp["NeedKey"]:
                        self.ui_log("입력한 Passphrase로 프로필을 갱신한 뒤 해당 BSSID에 접속합니다")
                    else:
                        self.ui_log("입력한 ID/암호로 프로필을 갱신한 뒤 해당 BSSID에 접속합니다")
                    self.ui_log(update_wifi_credentials(ssid, n, pw, user))
                    prof = resolve_profile(ssid)
                    if not prof:
                        raise RuntimeError("프로필 갱신 후 이름을 찾지 못했습니다.")

            try:
                enable_nonbroadcast(prof, bool(n.get("Hidden")))
            except Exception:
                pass
            self._conn_ssid = ssid
            self._conn_prof = prof
            self._conn_want = normalize_mac(n["Bssid"])
            self._conn_enterprise = bool(mp["Enterprise"])
            self._conn_bssid_locked = False
            self._conn_bssid_retried = False
            self._conn_ssid_fallback = False
            self._conn_i = 1
            self._conn_auth = 0
            self._conn_cancel = False
            self._conn_max = 40 if mp["Enterprise"] else 12
            self._conn_auth_max = 25 if mp["Enterprise"] else 5
            self.ui_log(f"BSSID 접속 요청: {ssid} / {n['Bssid']}  (profile={prof})")
            threading.Thread(target=self._issue_connect, args=(prof, ssid, n["Bssid"], bool(mp["Enterprise"])), daemon=True).start()
        except Exception as e:
            self.ui_log(str(e), "ERR")
            messagebox.showerror("접속 실패", str(e))
            self._finish_op()

    def _issue_connect(self, prof: str, ssid: str, bssid: str, enterprise: bool) -> None:
        ns = ""
        rc = -1
        err = None
        try:
            want = normalize_mac(bssid)
            cur = current_wifi()
            cur_mac = normalize_mac(cur.get("Bssid") or "")
            cur_ssid = cur.get("Ssid") or ""
            if is_connected(cur) and cur_ssid == ssid:
                self.after(0, lambda c=dict(cur): self._finish_ssid_autoselect(c))
                return
            if is_connected(cur) and cur_mac and cur_mac != want:
                self.after(0, lambda m=cur_mac: self.ui_log(f"다른 SSID에 연결 중({m}) → 끊고 {ssid} 로 접속"))
                wlan_disconnect()
                for _ in range(12):
                    time.sleep(0.25)
                    if not is_connected(current_wifi()):
                        break
            if enterprise:
                rc_b = wlan_connect_bssid(prof, bssid, ssid)
                rc = wlan_connect_profile(prof)
                ns = netsh(["wlan", "connect", f"name={prof}", f"ssid={ssid}"]).strip()
                self.after(0, lambda a=rc_b, b=rc: self.ui_log(f"802.1X 접속 profile={b} bssid_req={a} (저장된 계정)", "DEBUG"))
                if rc != 0 and rc_b == 0:
                    rc = 0
            else:
                rc = wlan_connect_bssid(prof, bssid, ssid)
                self.after(0, lambda a=rc: self.ui_log(f"WlanConnect BSSID rc={a}", "DEBUG"))
                if rc != 0:
                    ns = netsh(["wlan", "connect", f"name={prof}", f"ssid={ssid}"]).strip()
        except Exception as e:
            err = e
        self.after(0, lambda r=rc, s=ns, e=enterprise, x=err: self._after_connect_issued(r, s, e, x))

    def _after_connect_issued(self, rc: int, ns: str, enterprise: bool, err: Exception | None) -> None:
        if getattr(self, "_conn_cancel", False):
            return
        if err:
            self.ui_log(str(err), "ERR")
            messagebox.showerror("접속 실패", str(err))
            self._finish_op()
            return
        if ns:
            self.ui_log(f"netsh connect: {ns}")
        if rc in (87, 1168):
            self.ui_log(f"WlanConnect 초기 코드 {rc} (인증 완료 대기 중...)", "WARN")
            self.after(800, self._poll_connect_start)
            return
        if rc == 0:
            self.ui_log("접속 요청 수락", "OK")
        elif rc == 5:
            self.ui_log("권한 거부(5). 관리자 권한으로 실행해 보세요.", "ERR")
            messagebox.showerror("접속 실패", "권한 거부(5). 관리자 권한으로 실행해 보세요.")
            self._finish_op()
            return
        elif enterprise and ns and not netsh_failed(ns):
            self.ui_log(f"WlanConnect 코드 {rc} → netsh로 진행", "WARN")
        else:
            self.ui_log(f"WlanConnect 실패 코드 {rc}", "ERR")
            messagebox.showerror("접속 실패", f"WlanConnect 실패 코드 {rc}")
            self._finish_op()
            return
        self.after(300, self._poll_connect_start)

    def _finish_ssid_autoselect(self, cur: dict) -> None:
        self.lbl_now.configure(
            text=f"현재 연결: SSID={cur['Ssid']}   BSSID={cur['Bssid']}   신호={cur['Signal']}   CH={cur['Channel']}   {cur['Radio']}",
            fg="#006400",
        )
        self.apply_link_to_tree(cur)
        want = getattr(self, "_conn_want", "")
        self.ui_log(
            f"연결됨 SSID={cur.get('Ssid')} BSSID={cur.get('Bssid')} (요청 {want}). "
            "SSID접속은 Windows가 AP를 자동선택해서 연결합니다.",
            "OK",
        )
        self._finish_op()

    def _finish_op(self) -> None:
        self.busy = False
        self._conn_cancel = True
        self.btn_connect.configure(state="normal")

    def _cancel_connect_wait(self, reason: str) -> None:
        self._conn_cancel = True
        try:
            wlan_disconnect()
        except Exception:
            pass
        self.ui_log(reason, "WARN")
        self._finish_op()

    def _poll_connect_start(self) -> None:
        if getattr(self, "_conn_cancel", False):
            return
        threading.Thread(target=self._poll_connect_bg, daemon=True).start()

    def _poll_connect_bg(self) -> None:
        try:
            cur = current_wifi()
        except Exception as e:
            self.after(0, lambda: self._poll_connect_tick(None, e))
            return
        self.after(0, lambda c=cur: self._poll_connect_tick(c, None))

    def _poll_connect_tick(self, cur: dict | None, err: Exception | None) -> None:
        try:
            if getattr(self, "_conn_cancel", False):
                return
            if err:
                self.ui_log(str(err), "ERR")
                self._finish_op()
                return
            if not cur:
                self.after(800, self._poll_connect_start)
                return
            i = self._conn_i
            total = getattr(self, "_conn_max", 12)
            auth_max = getattr(self, "_conn_auth_max", 5)
            if i == 1 or i % 5 == 0 or is_connected(cur) or is_authenticating(cur):
                self.ui_log(f"대기 {i}/{total}  state={cur['State']} bssid={cur['Bssid']}")
            if (getattr(self, "_conn_enterprise", False) and i == 4 and not is_connected(cur) and not is_authenticating(cur) and not getattr(self, "_conn_ssid_fallback", False)):
                self._conn_ssid_fallback = True
                prof = getattr(self, "_conn_prof", self._conn_ssid)
                ssid = getattr(self, "_conn_ssid", "")
                self.ui_log("BSSID 요청 후 인증이 시작되지 않음 → 저장된 프로필로 SSID 접속")
                threading.Thread(target=lambda: (wlan_connect_profile(prof), netsh(["wlan", "connect", f"name={prof}", f"ssid={ssid}"])), daemon=True).start()
            same_bssid = normalize_mac(cur.get("Bssid") or "") == self._conn_want
            same_ssid = (cur.get("Ssid") or "") == getattr(self, "_conn_ssid", "")
            if is_connected(cur) and same_bssid:
                self.lbl_now.configure(text=f"현재 연결: SSID={cur['Ssid']}   BSSID={cur['Bssid']}   신호={cur['Signal']}   CH={cur['Channel']}   {cur['Radio']}", fg="#006400")
                self.apply_link_to_tree(cur)
                self.ui_log(f"연결됨  SSID={cur.get('Ssid')}  BSSID={cur.get('Bssid')}", "OK")
                self._finish_op()
                return
            if is_connected(cur) and same_ssid and not same_bssid:
                self.lbl_now.configure(text=f"현재 연결: SSID={cur['Ssid']}   BSSID={cur['Bssid']}   신호={cur['Signal']}   CH={cur['Channel']}   {cur['Radio']}", fg="#006400")
                self.apply_link_to_tree(cur)
                self.ui_log(f"연결됨 SSID={cur.get('Ssid')} BSSID={cur.get('Bssid')} (요청 {self._conn_want}). SSID접속은 Windows가 AP를 자동선택해서 연결합니다.", "OK")
                self._finish_op()
                return
            if is_authenticating(cur):
                self._conn_auth += 1
                kind = "802.1X/RADIUS" if getattr(self, "_conn_enterprise", False) else "PSK"
                self.ui_log(f"인증 시도 {self._conn_auth}/{auth_max}  ({kind} 협상 중)")
                if self._conn_auth >= auth_max:
                    self.ui_log("인증이 제한 시간 안에 완료되지 않음 → 연결을 끊습니다", "WARN")
                    threading.Thread(target=wlan_disconnect, daemon=True).start()
                    self.after(300, self._after_auth_fail)
                    return
            if i >= total:
                self.ui_log("요청은 갔지만 아직 연결이 확인되지 않음", "WARN")
                self._finish_op()
                return
            self._conn_i = i + 1
            self.after(800, self._poll_connect_start)
        except Exception as e:
            self.ui_log(str(e), "ERR")
            self._finish_op()

    def _after_auth_fail(self) -> None:
        self.update_now()
        self.refresh_list(force=True)
        if getattr(self, "_conn_enterprise", False):
            msg = "802.1X 인증이 끝나지 않았습니다. ID/암호뿐 아니라 EAP 계정 저장 실패, RADIUS 인증서, PEAP 외 방식(TTLS/TLS) 가능성도 확인하세요."
        else:
            msg = "암호를 확인하고 다시 접속하세요."
        self.ui_log(msg, "ERR")
        messagebox.showerror("접속 실패", msg)
        self._finish_op()

    def do_disconnect(self) -> None:
        cur = current_wifi()
        self.update_now()
        connecting = bool(self.busy) or is_authenticating(cur)
        if connecting:
            self._cancel_connect_wait(f"사용자 취소 (state={cur.get('State')}) → 접속 대기 중단")
            return
        if not is_connected(cur):
            self.ui_log(f"이미 끊긴 상태입니다 (state={cur.get('State')}) → 연결 끊기 생략")
            return
        self.busy = True
        try:
            self.ui_log(f"연결 끊기 요청: SSID={cur['Ssid']} BSSID={cur['Bssid']} state={cur['State']}")
            write_file_log("disconnect before " + format_debug(cur), "DEBUG")
            ns, rc = wlan_disconnect()
            self.ui_log(f"netsh: {ns}")
            write_file_log(f"WlanDisconnect rc={rc}", "DEBUG")
            if rc == 0:
                self.ui_log("WlanDisconnect 수락", "OK")
            elif rc > 0:
                self.ui_log(f"WlanDisconnect 코드 {rc}", "WARN")
            self._disc_i = 1
            self.after(300, self._poll_disconnect)
        except Exception as e:
            self.ui_log(str(e), "ERR")
            messagebox.showerror("끊기 실패", str(e))
            self._finish_op()

    def _poll_disconnect(self) -> None:
        try:
            i = self._disc_i
            cur = current_wifi()
            self.update_now()
            self.ui_log(f"대기 {i}/8  state={cur['State']}")
            if not is_connected(cur):
                self.apply_link_to_tree(cur)
                self.ui_log("무선 연결이 끊어졌습니다", "OK")
                self._finish_op()
                return
            if i >= 8:
                self.apply_link_to_tree(cur)
                self.ui_log("끊기 요청 후에도 연결된 상태로 보입니다", "WARN")
                self._finish_op()
                return
            self._disc_i = i + 1
            self.after(400, self._poll_disconnect)
        except Exception as e:
            self.ui_log(str(e), "ERR")
            self._finish_op()

    def do_delete(self) -> None:
        n = self.selected_net()
        if not n:
            messagebox.showinfo("안내", "목록에서 SSID/BSSID를 선택하세요.")
            return
        ssid = self.effective_ssid(n)
        if not ssid or ssid == "(숨김)":
            messagebox.showinfo("안내", "숨김 AP는 SSID를 입력한 뒤 해당 프로필을 삭제하세요.")
            return
        prof = resolve_profile(ssid)
        if not prof:
            messagebox.showinfo("안내", f"'{ssid}' 프로필이 없습니다.")
            return
        if not messagebox.askyesno("프로필 삭제", f"프로필 '{prof}' 을(를) 삭제할까요?"):
            return
        try:
            if self.busy:
                self._cancel_connect_wait("프로필 삭제 → 접속 대기 중단")
            self.ui_log(delete_profile(ssid), "OK")
            saved_profiles(refresh=True)
            cur = self.update_now()
            self.apply_link_to_tree(cur)
            self.update_cred_ui()
        except Exception as e:
            self.ui_log(str(e), "ERR")
            messagebox.showerror("삭제 실패", str(e))

    def do_reveal(self) -> None:
        n = self.selected_net()
        if not n:
            messagebox.showinfo("저장된 암호확인", "목록에서 Personal AP를 선택하세요.")
            return
        mp = map_auth(n.get("Auth", ""), n.get("Encrypt", ""))
        if mp["Enterprise"] or not mp["NeedKey"]:
            messagebox.showinfo("저장된 암호확인", "저장된 암호확인은 Personal 프로필만 지원합니다.")
            return
        ssid = self.effective_ssid(n)
        prof = resolve_profile(ssid)
        if not prof:
            messagebox.showinfo("저장된 암호확인", "확인할 프로필이 없습니다.")
            return
        try:
            key = profile_passphrase(prof)
            if not key:
                messagebox.showinfo("저장된 암호확인", f"저장된 Passphrase를 찾지 못했습니다. ({prof})")
                return
            self.var_pw.set(key)
            self.var_show.set(True)
            self._toggle_pw()
            self.ui_log(f"저장된 Passphrase를 표시했습니다. profile={prof}", "OK")
            write_file_log(f"passphrase reveal profile='{prof}' length={len(key)}", "DEBUG")
        except Exception as e:
            self.ui_log(str(e), "ERR")
            messagebox.showerror("저장된 암호확인", str(e))

    def open_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(LOG_DIR))
        self.ui_log(f"로그 폴더 열기: {LOG_DIR}")

    def save_logs(self) -> None:
        files = limit_logs()
        latest = LOG_FILE if LOG_FILE and LOG_FILE.exists() else (files[-1] if files else None)
        if not latest:
            messagebox.showinfo("최근로그", "저장할 로그가 없습니다.")
            return
        dest = filedialog.asksaveasfilename(title="최근 로그 1개 저장", defaultextension=".log", filetypes=[("로그 파일", "*.log"), ("모든 파일", "*.*")], initialfile=latest.name)
        if not dest:
            return
        Path(dest).write_bytes(latest.read_bytes())
        self.ui_log(f"최근 로그 1개를 저장했습니다 → {dest}", "OK")
        messagebox.showinfo("최근로그", f"최근 로그 1개를 저장했습니다.\n{dest}")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()