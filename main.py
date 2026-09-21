import webview
import scanner
import cleaner
import json
import sys
import ctypes
import os
import subprocess
import send2trash
import time
import psutil
import platform
import datetime
import threading
import hashlib

try:
    import pystray
    from PIL import Image as PILImage

    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False


def get_base_dirs():
    if getattr(sys, 'frozen', False):
        res_dir = sys._MEIPASS
        appdata = os.environ.get('APPDATA', os.path.expanduser('~'))
        data_dir = os.path.join(appdata, 'C盘清理大师')
        os.makedirs(data_dir, exist_ok=True)
    else:
        res_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = res_dir
    return res_dir, data_dir


RES_DIR, DATA_DIR = get_base_dirs()
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

_disk_scan = {"running": False, "current": "", "results": [], "done": False, "error": "", "root": ""}


def _get_dir_size_fast(path):
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except (OSError, PermissionError):
                        pass
        except (OSError, PermissionError):
            pass
    return total


def _scan_disk_worker(path):
    global _disk_scan
    try:
        results = []
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except (OSError, PermissionError) as e:
            _disk_scan["error"] = f"无法访问：{e}"
            _disk_scan["done"] = True
            _disk_scan["running"] = False
            return
        for entry in entries:
            _disk_scan["current"] = entry.path
            try:
                if entry.is_dir(follow_symlinks=False):
                    size = _get_dir_size_fast(entry.path)
                    results.append({"name": entry.name, "path": entry.path, "size": size, "isDir": True})
                elif entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    results.append({"name": entry.name, "path": entry.path, "size": size, "isDir": False})
            except (OSError, PermissionError):
                pass
        results.sort(key=lambda x: x["size"], reverse=True)
        _disk_scan["results"] = results[:80]
        _disk_scan["done"] = True
    except Exception as e:
        _disk_scan["error"] = str(e)
        _disk_scan["done"] = True
    finally:
        _disk_scan["running"] = False
        _disk_scan["current"] = ""


# ============ 重复文件 ============
_dup_scan = {"running": False, "current": "", "phase": "", "progress": 0, "done": False, "error": "", "groups": [],
             "scanned": 0, "total": 0}


def _file_md5(fp, chunk_size=65536):
    h = hashlib.md5()
    with open(fp, 'rb') as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def _dup_scan_worker(path, min_size_kb):
    global _dup_scan
    try:
        min_bytes = max(1, min_size_kb) * 1024
        _dup_scan["phase"] = "收集文件"
        size_map = {}
        count = 0
        for dirpath, _, filenames in os.walk(path, onerror=lambda x: None):
            _dup_scan["current"] = dirpath
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    st = os.stat(fp)
                    if st.st_size < min_bytes:
                        continue
                    size_map.setdefault(st.st_size, []).append(fp)
                    count += 1
                except (OSError, PermissionError):
                    pass
        _dup_scan["scanned"] = count
        candidate_groups = [paths for paths in size_map.values() if len(paths) >= 2]
        total_candidates = sum(len(g) for g in candidate_groups)
        _dup_scan["total"] = total_candidates
        if total_candidates == 0:
            _dup_scan["groups"] = []
            _dup_scan["done"] = True
            return
        _dup_scan["phase"] = "计算哈希"
        hash_groups = {}
        processed = 0
        for paths in candidate_groups:
            size = os.path.getsize(paths[0])
            for fp in paths:
                processed += 1
                if processed % 5 == 0 or processed == total_candidates:
                    _dup_scan["progress"] = int(processed / max(total_candidates, 1) * 100)
                _dup_scan["current"] = fp
                try:
                    h = _file_md5(fp)
                    hash_groups.setdefault((size, h), []).append(fp)
                except (OSError, PermissionError):
                    pass
        _dup_scan["phase"] = "整理结果"
        groups = []
        for (size, h), paths in hash_groups.items():
            if len(paths) >= 2:
                groups.append({"size": size, "count": len(paths), "waste": size * (len(paths) - 1), "paths": paths})
        groups.sort(key=lambda x: x["waste"], reverse=True)
        _dup_scan["groups"] = groups[:300]
        _dup_scan["done"] = True
    except Exception as e:
        _dup_scan["error"] = str(e)
        _dup_scan["done"] = True
    finally:
        _dup_scan["running"] = False
        _dup_scan["current"] = ""
        _dup_scan["phase"] = ""


# ============ 空文件夹 ============
_empty_scan = {"running": False, "current": "", "done": False, "error": "", "folders": [], "scanned": 0}


def _empty_scan_worker(path):
    global _empty_scan
    try:
        empty_set = set()
        for dirpath, dirnames, filenames in os.walk(path, topdown=False, onerror=lambda x: None):
            if dirpath == path:
                continue
            _empty_scan["current"] = dirpath
            _empty_scan["scanned"] += 1
            has_file = len(filenames) > 0
            has_nonempty_subdir = False
            for d in dirnames:
                sub = os.path.join(dirpath, d)
                if sub not in empty_set:
                    has_nonempty_subdir = True
                    break
            if not has_file and not has_nonempty_subdir:
                empty_set.add(dirpath)
        _empty_scan["folders"] = list(empty_set)
        _empty_scan["done"] = True
    except Exception as e:
        _empty_scan["error"] = str(e)
        _empty_scan["done"] = True
    finally:
        _empty_scan["running"] = False
        _empty_scan["current"] = ""


# ============ 卸载残留 ============
_residue_scan = {"running": False, "current": "", "done": False, "error": "", "results": [], "scanned": 0}


def _read_installed_software():
    installed_names = set()
    installed_paths = []
    try:
        import winreg
    except ImportError:
        return installed_names, installed_paths
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hkey, subkey in keys:
        try:
            with winreg.OpenKey(hkey, subkey) as k:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(k, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, sub_name) as sk:
                            display_name = ""
                            install_loc = ""
                            try:
                                display_name = winreg.QueryValueEx(sk, "DisplayName")[0]
                            except Exception:
                                pass
                            try:
                                install_loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                            except Exception:
                                pass
                            if display_name:
                                installed_names.add(str(display_name).lower().strip())
                            if install_loc:
                                p = str(install_loc).lower().strip().rstrip("\\/")
                                if p:
                                    installed_paths.append(p)
                    except Exception:
                        pass
        except Exception:
            pass
    return installed_names, installed_paths


def _has_exe_in_root(path, limit=80):
    try:
        count = 0
        for f in os.listdir(path):
            count += 1
            if count > limit:
                break
            if f.lower().endswith(".exe"):
                return True
    except Exception:
        pass
    return False


def _residue_scan_worker():
    global _residue_scan
    try:
        _residue_scan["current"] = "读取已安装软件列表..."
        installed_names, installed_paths = _read_installed_software()
        scan_dirs = []
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        for d in [pf, pf86, local, roaming]:
            if d and os.path.isdir(d):
                scan_dirs.append(d)
        results = []
        for base in scan_dirs:
            _residue_scan["current"] = f"扫描：{base}"
            try:
                for name in os.listdir(base):
                    path = os.path.join(base, name)
                    if not os.path.isdir(path):
                        continue
                    if name.startswith(".") or name in ("Microsoft", "Windows", "Packages", "Temp"):
                        continue
                    _residue_scan["scanned"] += 1
                    _residue_scan["current"] = f"检查：{name}"
                    path_lower = path.lower()
                    name_lower = name.lower().strip()
                    is_installed = False
                    for p in installed_paths:
                        if p == path_lower or p.startswith(path_lower + "\\") or path_lower.startswith(p + "\\"):
                            is_installed = True
                            break
                    if not is_installed:
                        for n in installed_names:
                            if n == name_lower or n.replace(" ", "") == name_lower.replace(" ", ""):
                                is_installed = True
                                break
                            if len(name_lower) >= 5 and name_lower in n:
                                is_installed = True
                                break
                    if is_installed:
                        continue
                    try:
                        size = _get_dir_size_fast(path)
                        mtime = os.path.getmtime(path)
                        days_old = int((time.time() - mtime) / 86400)
                        has_exe = _has_exe_in_root(path)
                        if not has_exe and days_old >= 30 and size < 5 * 1024 ** 3:
                            if size > 200 * 1024 ** 2:
                                risk = "medium"
                            else:
                                risk = "low"
                            results.append({
                                "path": path, "name": name, "size": size,
                                "days_old": days_old,
                                "mtime_str": time.strftime("%Y-%m-%d", time.localtime(mtime)),
                                "risk": risk,
                            })
                    except Exception:
                        pass
            except Exception:
                pass
        results.sort(key=lambda x: x["size"], reverse=True)
        _residue_scan["results"] = results[:100]
        _residue_scan["done"] = True
    except Exception as e:
        _residue_scan["error"] = str(e)
        _residue_scan["done"] = True
    finally:
        _residue_scan["running"] = False
        _residue_scan["current"] = ""


# ============ 文件粉碎机 ============
_shred_state = {
    "running": False,
    "cancel": False,
    "current": "",
    "total_files": 0,
    "done_files": 0,
    "total_bytes": 0,
    "done_bytes": 0,
    "passes": 3,
    "done": False,
    "error": "",
    "failed": [],
    "success": 0,
}

# 绝对禁止粉碎的路径
SHRED_FORBIDDEN_PATHS = [
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\System Volume Information",
    r"C:\$Recycle.Bin",
    r"C:\Recovery",
    r"C:\Boot",
    r"C:\EFI",
]

SHRED_FORBIDDEN_FILES = {
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "ntuser.dat", "bootmgr", "bootnxt", "bcd", "ntldr",
}


def _is_shred_safe(path):
    """检查文件是否可以粉碎，返回 (是否安全, 原因)"""
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False, "路径无效"

    # 1. 路径黑名单（不能碰系统目录）
    for forbidden in SHRED_FORBIDDEN_PATHS:
        fp = os.path.normcase(os.path.abspath(forbidden))
        if p == fp or p.startswith(fp + os.sep):
            return False, f"位于系统保护区：{forbidden}"

    # 2. 文件名黑名单
    name = os.path.basename(p).lower()
    if name in SHRED_FORBIDDEN_FILES:
        return False, f"系统关键文件：{name}"

    # 3. 不允许盘根目录
    drive, tail = os.path.splitdrive(p)
    if tail in ("\\", "/", ""):
        return False, "不允许粉碎磁盘根目录"

    return True, ""


def _shred_one_file(path, passes, progress_cb):
    """粉碎单个文件（覆写 passes 次后删除）"""
    try:
        size = os.path.getsize(path)
    except Exception:
        return False, 0

    if size == 0:
        # 空文件直接删
        try:
            os.remove(path)
            return True, 0
        except Exception:
            return False, 0

    try:
        chunk_size = 1024 * 1024  # 1MB
        with open(path, 'r+b') as f:
            for pass_num in range(passes):
                if _shred_state["cancel"]:
                    return False, 0
                f.seek(0)
                remaining = size
                while remaining > 0:
                    if _shred_state["cancel"]:
                        return False, 0
                    w = min(chunk_size, remaining)
                    f.write(os.urandom(w))
                    remaining -= w
                    if progress_cb:
                        progress_cb(w)
                f.flush()
                os.fsync(f.fileno())
        os.remove(path)
        return True, size
    except Exception:
        # 失败也尝试删除
        try:
            os.remove(path)
        except Exception:
            pass
        return False, size


def _shred_worker(paths, passes):
    global _shred_state
    try:
        _shred_state["passes"] = passes

        # 计算总大小
        total_size = 0
        valid_paths = []
        for p in paths:
            safe, reason = _is_shred_safe(p)
            if not safe:
                _shred_state["failed"].append(f"{os.path.basename(p)}: {reason}")
                continue
            if os.path.isfile(p):
                try:
                    total_size += os.path.getsize(p)
                    valid_paths.append(p)
                except Exception:
                    _shred_state["failed"].append(f"{os.path.basename(p)}: 无法读取")
            elif os.path.isdir(p):
                # 递归收集文件
                for dirpath, _, filenames in os.walk(p, onerror=lambda x: None):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        safe2, reason2 = _is_shred_safe(fp)
                        if not safe2:
                            continue
                        try:
                            total_size += os.path.getsize(fp)
                            valid_paths.append(fp)
                        except Exception:
                            pass

        _shred_state["total_files"] = len(valid_paths)
        _shred_state["total_bytes"] = total_size

        if len(valid_paths) == 0:
            _shred_state["done"] = True
            return

        # 逐个粉碎
        for p in valid_paths:
            if _shred_state["cancel"]:
                break
            _shred_state["current"] = p

            def _cb(written, _p=p):
                _shred_state["done_bytes"] += written

            ok, sz = _shred_one_file(p, passes, _cb)
            if ok:
                _shred_state["success"] += 1
            else:
                if not _shred_state["cancel"]:
                    _shred_state["failed"].append(f"{os.path.basename(p)}: 粉碎失败")
            _shred_state["done_files"] += 1

        _shred_state["done"] = True
    except Exception as e:
        _shred_state["error"] = str(e)
        _shred_state["done"] = True
    finally:
        _shred_state["running"] = False
        _shred_state["current"] = ""


PROCESS_FRIENDLY_NAMES = {
    "chrome.exe": "Google Chrome 浏览器", "msedge.exe": "Microsoft Edge 浏览器",
    "firefox.exe": "Mozilla Firefox 浏览器", "wechat.exe": "微信", "wechatappex.exe": "微信（小程序）",
    "qq.exe": "QQ", "tim.exe": "TIM", "dingtalk.exe": "钉钉", "wework.exe": "企业微信",
    "feishu.exe": "飞书", "douyin.exe": "抖音", "cloudmusic.exe": "网易云音乐",
    "qqmusic.exe": "QQ 音乐", "code.exe": "Visual Studio Code", "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA", "explorer.exe": "Windows 资源管理器", "svchost.exe": "Windows 服务主机",
    "system": "Windows 系统内核", "dwm.exe": "桌面窗口管理器", "msmpeng.exe": "Windows Defender 杀毒",
    "smss.exe": "会话管理器（系统关键）", "csrss.exe": "客户端服务（系统关键）",
    "wininit.exe": "Windows 初始化（系统关键）", "services.exe": "服务控制（系统关键）",
    "lsass.exe": "本地安全认证（系统关键）", "winlogon.exe": "Windows 登录（系统关键）",
    "steam.exe": "Steam 游戏平台", "wegame.exe": "WeGame 游戏平台",
    "sogouinput.exe": "搜狗输入法", "qqpinyin.exe": "QQ 拼音输入法",
    "baidunetdisk.exe": "百度网盘", "xunlei.exe": "迅雷", "wps.exe": "WPS Office",
    "winword.exe": "Microsoft Word", "excel.exe": "Microsoft Excel", "notepad.exe": "记事本",
    "taskmgr.exe": "任务管理器", "cmd.exe": "命令提示符", "powershell.exe": "PowerShell",
    "regedit.exe": "注册表编辑器", "conhost.exe": "控制台窗口主机",
}

SYSTEM_CRITICAL_PROCS = {"system", "system idle process", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
                         "smss.exe", "winlogon.exe", "registry", "memory compression"}


def load_settings():
    default = {
        "whitelist": ["explorer.exe", "system", "svchost.exe", "msmpeng.exe", "csrss.exe"],
        "auto_clean": {"enabled": False, "interval_hours": 24, "start_hour": 2, "end_hour": 5, "last_clean": 0},
        "shutdown_deadline": 0,
        "window": {"width": 960, "height": 780},
        "theme": "dark",
        "sound": True,
        "minimize_to_tray": True
    }
    if not os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)
        return default
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        ac = s.get("auto_clean", {})
        if "preferred_hour" in ac and "start_hour" not in ac:
            ph = ac.pop("preferred_hour", 3)
            ac["start_hour"] = max(0, ph - 2)
            ac["end_hour"] = min(23, ph + 3)
            s["auto_clean"] = ac
        for k, v in default["auto_clean"].items():
            s.setdefault("auto_clean", {}).setdefault(k, v)
        s.setdefault("whitelist", default["whitelist"])
        s.setdefault("shutdown_deadline", 0)
        s.setdefault("window", default["window"])
        s.setdefault("theme", "dark")
        s.setdefault("sound", True)
        s.setdefault("minimize_to_tray", True)
        return s
    except Exception:
        return default


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"records": []}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"records": []}


def save_history(h):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def format_size(bytes_val):
    try:
        bytes_val = float(bytes_val)
    except Exception:
        return "0 B"
    if bytes_val < 1024:
        return f"{int(bytes_val)} B"
    for unit in ['KB', 'MB', 'GB', 'TB']:
        bytes_val /= 1024
        if bytes_val < 1024:
            return f"{bytes_val:.2f} {unit}"
    return f"{bytes_val:.2f} PB"


def get_cpu_name():
    if platform.system() == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
            winreg.CloseKey(key)
            return name.strip()
        except Exception:
            pass
    return platform.processor() or "未知处理器"


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def restart_as_admin(rule_ids):
    ids_str = ",".join(rule_ids) if rule_ids else ""
    if getattr(sys, 'frozen', False):
        executable = sys.executable
        params = f'--clean-ids="{ids_str}"' if ids_str else ""
    else:
        executable = sys.executable
        script_path = os.path.abspath(sys.argv[0])
        params = f'"{script_path}" --clean-ids="{ids_str}"' if ids_str else f'"{script_path}"'
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    except Exception as e:
        print("提权失败:", e)
    finally:
        os._exit(0)


def in_time_window(current_hour, start_hour, end_hour):
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


CHAT_FORBIDDEN_KEYWORDS = ["\\msg\\", "\\msgattach\\", "\\db\\", "\\database\\", "\\config\\"]
CHAT_FORBIDDEN_EXTS = {".db", ".sqlite", ".sqlite3", ".db-shm", ".db-wal", ".dat"}


def _is_chat_file_safe(path):
    lower = path.lower().replace("/", "\\")
    for kw in CHAT_FORBIDDEN_KEYWORDS:
        if kw in lower:
            return False
    ext = os.path.splitext(lower)[1]
    if ext in CHAT_FORBIDDEN_EXTS:
        return False
    return True


WECHAT_SUBDIRS = {"Image": "图片", "Video": "视频", "File": "文件", "Voice": "语音", "Cache": "缓存",
                  "Favorite": "收藏", "Fav": "收藏"}
QQ_SUBDIRS = {"Image": "图片", "Video": "视频", "FileRecv": "接收的文件", "Audio": "语音", "Photo": "照片",
              "FileShare": "分享的文件", "Thumb": "缩略图", "Screen": "截图"}


def detect_chat_apps():
    result = {"wechat": [], "qq": []}
    home = os.path.expanduser("~")
    search_roots = set()
    search_roots.add(os.path.join(home, "Documents"))
    search_roots.add(os.path.join(home, "文档"))
    for drive in ["C:", "D:", "E:", "F:", "G:"]:
        search_roots.add(f"{drive}\\Documents")
        search_roots.add(f"{drive}\\文档")
        search_roots.add(f"{drive}\\")
    for root in search_roots:
        for folder_name in ["WeChat Files", "xwechat_files"]:
            p = os.path.join(root, folder_name)
            if not os.path.isdir(p):
                continue
            try:
                for sub in os.listdir(p):
                    sub_p = os.path.join(p, sub)
                    if not os.path.isdir(sub_p) or sub.lower() == "all users":
                        continue
                    if os.path.isdir(os.path.join(sub_p, "FileStorage")):
                        result["wechat"].append({"account": sub, "path": sub_p, "version": "3.x"})
                    elif os.path.isdir(os.path.join(sub_p, "msg")):
                        result["wechat"].append({"account": sub, "path": sub_p, "version": "4.x"})
            except (OSError, PermissionError):
                pass
    for root in search_roots:
        p = os.path.join(root, "Tencent Files")
        if not os.path.isdir(p):
            continue
        try:
            for sub in os.listdir(p):
                sub_p = os.path.join(p, sub)
                if os.path.isdir(sub_p) and sub.isdigit():
                    result["qq"].append({"account": sub, "path": sub_p, "version": "QQ"})
        except (OSError, PermissionError):
            pass
    return result


def scan_chat_dir(account_path, app_type):
    results = []
    if app_type == "wechat":
        candidates = [os.path.join(account_path, "FileStorage"), os.path.join(account_path, "msg", "attach")]
        subdir_map = WECHAT_SUBDIRS
    else:
        candidates = [account_path]
        subdir_map = QQ_SUBDIRS
    for base in candidates:
        if not os.path.isdir(base):
            continue
        try:
            for sub_name in os.listdir(base):
                sub_path = os.path.join(base, sub_name)
                if not os.path.isdir(sub_path) or sub_name not in subdir_map:
                    continue
                size = _get_dir_size_fast(sub_path)
                if size > 0:
                    results.append(
                        {"category": sub_name, "label": subdir_map[sub_name], "path": sub_path, "size": size})
        except (OSError, PermissionError):
            pass
    merged = {}
    for r in results:
        key = r["category"]
        if key in merged:
            merged[key]["size"] += r["size"]
            merged[key]["paths"].append(r["path"])
        else:
            merged[key] = {"category": r["category"], "label": r["label"], "path": r["path"], "paths": [r["path"]],
                           "size": r["size"]}
    return list(merged.values())


def collect_chat_files(paths, min_age_days):
    cutoff = time.time() - min_age_days * 86400
    files = []
    total_size = 0
    skipped_danger = 0
    for base in paths:
        if not os.path.isdir(base):
            continue
        for dirpath, _, filenames in os.walk(base, onerror=lambda x: None):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not _is_chat_file_safe(fp):
                    skipped_danger += 1
                    continue
                try:
                    st = os.stat(fp)
                    if st.st_mtime > cutoff:
                        continue
                    files.append(fp)
                    total_size += st.st_size
                except (OSError, PermissionError):
                    pass
    return files, total_size, skipped_danger


def play_windows_sound(kind):
    try:
        import winsound
        if kind == "success":
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif kind == "error":
            winsound.MessageBeep(winsound.MB_ICONHAND)
        elif kind == "notify":
            winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass


# ============ 托盘 ============
_main_window = None
_tray_icon = None
_quitting = False
_tray_thread = None


def _create_tray_image():
    try:
        icon_path = os.path.join(RES_DIR, "icon.ico")
        if os.path.exists(icon_path):
            return PILImage.open(icon_path)
    except Exception:
        pass
    try:
        icon_path = os.path.join(DATA_DIR, "icon.ico")
        if os.path.exists(icon_path):
            return PILImage.open(icon_path)
    except Exception:
        pass
    return PILImage.new("RGBA", (64, 64), (79, 124, 255, 255))


def _tray_show(icon, item):
    try:
        if _main_window:
            _main_window.show()
            _main_window.restore()
    except Exception:
        pass


def _tray_clean(icon, item):
    try:
        if _main_window:
            _main_window.show()
            _main_window.restore()
            js = "document.querySelector('.tab[data-page=\"cleanPage\"]').click(); setTimeout(() => { if (typeof doScan === 'function') doScan(); }, 200);"
            _main_window.evaluate_js(js)
    except Exception:
        pass


def _tray_quit(icon, item):
    global _quitting
    _quitting = True
    try:
        icon.stop()
    except Exception:
        pass
    try:
        if _main_window:
            _main_window.destroy()
    except Exception:
        pass
    time.sleep(0.2)
    os._exit(0)


def _run_tray():
    global _tray_icon
    if not HAS_TRAY:
        print("[托盘] pystray 未安装，跳过")
        return
    try:
        img = _create_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", _tray_show, default=True),
            pystray.MenuItem("一键清理", _tray_clean),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _tray_quit),
        )
        _tray_icon = pystray.Icon("CleanerMaster", img, "C盘清理大师", menu)
        _tray_icon.run()
    except Exception as e:
        print(f"[托盘] 启动失败: {e}")


def _start_tray():
    global _tray_thread
    if not HAS_TRAY:
        return
    _tray_thread = threading.Thread(target=_run_tray, daemon=True)
    _tray_thread.start()


class Api:
    def __init__(self):
        self.pending_ids = []
        for arg in sys.argv:
            if arg.startswith("--clean-ids="):
                ids_str = arg.split("=", 1)[1].strip('"')
                if ids_str:
                    self.pending_ids = ids_str.split(",")
        s = load_settings()
        cleaner.set_whitelist(s.get("whitelist", []))

    def get_initial_state(self):
        s = load_settings()
        return {
            "theme": s.get("theme", "dark"),
            "sound": s.get("sound", True),
            "has_tray": HAS_TRAY,
            "minimize_to_tray": s.get("minimize_to_tray", True),
        }

    def play_sound(self, kind):
        s = load_settings()
        if s.get("sound", True):
            threading.Thread(target=play_windows_sound, args=(kind,), daemon=True).start()
        return {"ok": True}

    def save_theme(self, theme):
        s = load_settings()
        s["theme"] = theme
        save_settings(s)
        return {"ok": True}

    def save_sound(self, enabled):
        s = load_settings()
        s["sound"] = bool(enabled)
        save_settings(s)
        return {"ok": True}

    def save_minimize_to_tray(self, enabled):
        s = load_settings()
        s["minimize_to_tray"] = bool(enabled)
        save_settings(s)
        return {"ok": True}

    def hide_to_tray(self):
        try:
            if _main_window:
                _main_window.hide()
        except Exception:
            pass
        return {"ok": True}

    def quit_app(self):
        global _quitting
        _quitting = True
        try:
            if _tray_icon:
                _tray_icon.stop()
        except Exception:
            pass
        try:
            if _main_window:
                _main_window.destroy()
        except Exception:
            pass
        time.sleep(0.2)
        os._exit(0)

    def get_pending_clean(self):
        return self.pending_ids

    def get_scan_progress(self):
        return scanner.get_scan_status()

    def scan(self):
        rules_path = os.path.join(RES_DIR, "rules.json")
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                rules = json.load(f)["rules"]
        except Exception:
            return []
        results = []
        for r in rules:
            try:
                size, count = scanner.scan_path(r["paths"][0], r.get("minAgeHours", 24), r["name"])
                results.append({"id": r["id"], "name": r["name"], "size": size, "count": count,
                                "requiresAdmin": r.get("requiresAdmin", False)})
            except Exception:
                results.append({"id": r["id"], "name": r["name"] + " (扫描出错)", "size": 0, "count": 0,
                                "requiresAdmin": r.get("requiresAdmin", False)})
        return results

    def scan_large_files(self):
        return scanner.scan_large_files("C:\\", 50)

    def open_file_location(self, filepath):
        try:
            norm_path = os.path.normpath(filepath)
            if os.path.exists(norm_path):
                subprocess.Popen(['explorer', '/select,', norm_path])
                return True
            return False
        except Exception:
            return False

    def open_folder(self, folder):
        try:
            norm = os.path.normpath(folder)
            if os.path.isdir(norm):
                os.startfile(norm)
                return True
            elif os.path.isfile(norm):
                subprocess.Popen(['explorer', '/select,', norm])
                return True
            return False
        except Exception:
            return False

    def pick_folder(self):
        try:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
                "$f.Description = '选择要扫描的目录';"
                "$f.ShowNewFolderButton = $false;"
                "$r = $f.ShowDialog();"
                "if ($r -eq 'OK') { Write-Output $f.SelectedPath }"
            )
            result = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                                    capture_output=True, text=True, timeout=180)
            path = (result.stdout or "").strip()
            if path and os.path.isdir(path):
                return {"ok": True, "path": path}
            return {"ok": False, "msg": "用户取消"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pick_files_for_shred(self):
        """弹文件选择框（支持多选）"""
        try:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f = New-Object System.Windows.Forms.OpenFileDialog;"
                "$f.Multiselect = $true;"
                "$f.Title = '选择要粉碎的文件（可多选）';"
                "$r = $f.ShowDialog();"
                "if ($r -eq 'OK') { $f.FileNames -join '|' }"
            )
            result = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                                    capture_output=True, text=True, timeout=180)
            out = (result.stdout or "").strip()
            if out:
                files = [f for f in out.split("|") if f and os.path.isfile(f)]
                return {"ok": True, "files": files}
            return {"ok": False, "msg": "用户取消"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def calc_shred_size(self, paths):
        """计算待粉碎的总大小和文件数"""
        total_size = 0
        total_files = 0
        rejected = []
        for p in paths:
            safe, reason = _is_shred_safe(p)
            if not safe:
                rejected.append({"path": p, "reason": reason})
                continue
            if os.path.isfile(p):
                try:
                    total_size += os.path.getsize(p)
                    total_files += 1
                except Exception:
                    pass
            elif os.path.isdir(p):
                for dirpath, _, filenames in os.walk(p, onerror=lambda x: None):
                    for f in filenames:
                        fp = os.path.join(dirpath, f)
                        safe2, _ = _is_shred_safe(fp)
                        if not safe2:
                            continue
                        try:
                            total_size += os.path.getsize(fp)
                            total_files += 1
                        except Exception:
                            pass
        return {"size": total_size, "count": total_files, "rejected": rejected}

    def start_shred(self, paths, passes=3):
        global _shred_state
        if _shred_state.get("running", False):
            return {"ok": False, "msg": "正在粉碎中，请稍候"}
        if not paths:
            return {"ok": False, "msg": "没有要粉碎的文件"}
        try:
            passes = max(1, min(7, int(passes)))
        except Exception:
            passes = 3

        _shred_state = {
            "running": True, "cancel": False, "current": "",
            "total_files": 0, "done_files": 0,
            "total_bytes": 0, "done_bytes": 0,
            "passes": passes, "done": False, "error": "",
            "failed": [], "success": 0,
        }
        t = threading.Thread(target=_shred_worker, args=(list(paths), passes), daemon=True)
        t.start()
        return {"ok": True}

    def get_shred_status(self):
        return dict(_shred_state)

    def cancel_shred(self):
        _shred_state["cancel"] = True
        return {"ok": True}

    def delete_large_files(self, file_paths):
        total_freed = 0
        deleted_count = 0
        errors = []
        blocked = []
        for path in file_paths:
            try:
                size = os.path.getsize(path)
                send2trash.send2trash(path)
                total_freed += size
                deleted_count += 1
            except Exception as e:
                blockers = cleaner.find_locking_processes_batch([path])
                if blockers and path in blockers:
                    blocked.append({"path": path, "processes": blockers[path]})
                else:
                    errors.append(f"{os.path.basename(path)}: 无法删除")
        return {"freed": total_freed, "deleted": deleted_count, "errors": errors, "blocked": blocked}

    def kill_process(self, pid):
        return cleaner.kill_process_by_pid(pid)

    def clean(self, rule_ids):
        rules_path = os.path.join(RES_DIR, "rules.json")
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)["rules"]
        needs_admin = False
        for r in rules:
            if r["id"] in rule_ids and r.get("requiresAdmin", False):
                if not is_admin():
                    needs_admin = True
                    break
        if needs_admin:
            return {"needsAdmin": True, "rule_ids": rule_ids}
        total_freed = 0
        details = []
        for r in rules:
            if r["id"] in rule_ids:
                if "command" in r:
                    freed, deleted, skipped, err = cleaner.run_system_command(r["command"])
                    details.append(
                        {"id": r["id"], "name": r["name"], "freed": freed, "deleted": deleted, "skipped": skipped,
                         "error": err, "blocked": {}})
                    if freed > 0:
                        total_freed += freed
                else:
                    freed, deleted, skipped, blocked = cleaner.clean_path(r["paths"][0], r.get("minAgeHours", 24))
                    total_freed += freed
                    details.append(
                        {"id": r["id"], "name": r["name"], "freed": freed, "deleted": deleted, "skipped": skipped,
                         "error": "", "blocked": blocked})
        s = load_settings()
        s["auto_clean"]["last_clean"] = time.time()
        save_settings(s)
        try:
            h = load_history()
            h["records"].append({"time": time.time(), "freed": total_freed,
                                 "count": sum(1 for d in details if d.get("deleted", 0) > 0)})
            h["records"] = h["records"][-100:]
            save_history(h)
        except Exception:
            pass
        return {"totalFreed": total_freed, "details": details}

    def restart_as_admin(self, rule_ids):
        restart_as_admin(rule_ids)

    def get_settings(self):
        s = load_settings()
        ac = s.get("auto_clean", {})
        return {
            "whitelist": s.get("whitelist", []),
            "auto_clean": ac,
            "is_admin": is_admin(),
            "theme": s.get("theme", "dark"),
            "sound": s.get("sound", True),
            "has_tray": HAS_TRAY,
            "minimize_to_tray": s.get("minimize_to_tray", True),
            "last_clean_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(ac.get("last_clean", 0))) if ac.get(
                "last_clean", 0) > 0 else "从未清理"
        }

    def get_running_processes(self):
        procs = {}
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = proc.info['name'] or ""
                if not name:
                    continue
                name_lower = name.lower()
                if name_lower in procs:
                    continue
                procs[name_lower] = {"name": name, "display": PROCESS_FRIENDLY_NAMES.get(name_lower, name),
                                     "is_system": name_lower in SYSTEM_CRITICAL_PROCS}
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        result = list(procs.values())
        result.sort(key=lambda x: x["display"])
        return result

    def get_friendly_name(self, proc_name):
        return PROCESS_FRIENDLY_NAMES.get(proc_name.lower(), proc_name)

    def save_whitelist(self, whitelist):
        s = load_settings()
        s["whitelist"] = [w.strip() for w in whitelist if w.strip()]
        save_settings(s)
        cleaner.set_whitelist(s["whitelist"])
        return {"ok": True}

    def save_auto_clean(self, enabled, interval_hours, start_hour, end_hour):
        s = load_settings()
        s["auto_clean"]["enabled"] = bool(enabled)
        s["auto_clean"]["interval_hours"] = int(interval_hours)
        s["auto_clean"]["start_hour"] = int(start_hour)
        s["auto_clean"]["end_hour"] = int(end_hour)
        save_settings(s)
        return {"ok": True}

    def check_auto_clean(self):
        s = load_settings()
        ac = s.get("auto_clean", {})
        if not ac.get("enabled", False):
            return {"should_clean": False}
        last = ac.get("last_clean", 0)
        interval_sec = ac.get("interval_hours", 24) * 3600
        now = time.time()
        current_hour = time.localtime(now).tm_hour
        interval_ok = (now - last) >= interval_sec
        hour_ok = in_time_window(current_hour, ac.get("start_hour", 2), ac.get("end_hour", 5))
        if interval_ok and hour_ok:
            return {"should_clean": True,
                    "last_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last > 0 else "从未"}
        return {"should_clean": False}

    def get_system_info(self):
        cpu_percent = 0.0
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
        except Exception:
            pass
        mem_total_gb = mem_used_gb = 0
        mem_percent = 0
        try:
            mem = psutil.virtual_memory()
            mem_total_gb = round(mem.total / (1024 ** 3), 1)
            mem_used_gb = round(mem.used / (1024 ** 3), 1)
            mem_percent = mem.percent
        except Exception:
            pass
        disk_total_gb = disk_used_gb = 0
        disk_percent = 0
        try:
            disk = psutil.disk_usage("C:\\")
            disk_total_gb = round(disk.total / (1024 ** 3), 1)
            disk_used_gb = round(disk.used / (1024 ** 3), 1)
            disk_percent = disk.percent
        except Exception:
            pass
        cpu_cores = 0
        try:
            cpu_cores = psutil.cpu_count(logical=True) or 0
        except Exception:
            pass
        cpu_name = "未知处理器"
        try:
            cpu_name = get_cpu_name()
        except Exception:
            pass
        os_info = "Windows"
        try:
            os_info = f"Windows {platform.release()}"
        except Exception:
            pass
        battery_info = {"present": False, "percent": 0, "plugged": False}
        try:
            bat = psutil.sensors_battery()
            if bat:
                battery_info = {"present": True, "percent": round(bat.percent), "plugged": bool(bat.power_plugged)}
        except Exception:
            pass
        return {"os": os_info, "cpu": cpu_name, "cpu_cores": cpu_cores, "cpu_percent": cpu_percent,
                "mem_total_gb": mem_total_gb, "mem_used_gb": mem_used_gb, "mem_percent": mem_percent,
                "disk_total_gb": disk_total_gb, "disk_used_gb": disk_used_gb, "disk_percent": disk_percent,
                "battery": battery_info}

    def get_clean_history(self):
        try:
            h = load_history()
            records = h.get("records", [])
            now = datetime.datetime.now()
            month_start = datetime.datetime(now.year, now.month, 1).timestamp()
            month_freed = sum(r.get("freed", 0) for r in records if r.get("time", 0) >= month_start)
            total_freed = sum(r.get("freed", 0) for r in records)
            recent = sorted(records, key=lambda x: x.get("time", 0), reverse=True)[:10]
            recent_display = [{"time_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("time", 0))),
                               "freed_str": format_size(r.get("freed", 0)), "count": r.get("count", 0)} for r in recent]
            return {"month_freed_str": format_size(month_freed), "total_freed_str": format_size(total_freed),
                    "total_count": len(records), "recent": recent_display}
        except Exception:
            return {"month_freed_str": "0 B", "total_freed_str": "0 B", "total_count": 0, "recent": []}

    def set_shutdown_timer(self, minutes):
        try:
            minutes = int(minutes)
        except Exception:
            return {"ok": False, "msg": "时间格式错误"}
        if minutes < 1:
            return {"ok": False, "msg": "至少 1 分钟"}
        if minutes > 1440:
            return {"ok": False, "msg": "最多 24 小时"}
        secs = minutes * 60
        try:
            subprocess.run("shutdown /a", shell=True, capture_output=True)
            result = subprocess.run(f"shutdown /s /t {secs}", shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                return {"ok": False, "msg": result.stderr or "设置失败，可能需要管理员权限"}
            s = load_settings()
            s["shutdown_deadline"] = time.time() + secs
            save_settings(s)
            return {"ok": True, "msg": f"将在 {minutes} 分钟后关机"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def cancel_shutdown(self):
        try:
            subprocess.run("shutdown /a", shell=True, capture_output=True)
            s = load_settings()
            s["shutdown_deadline"] = 0
            save_settings(s)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_shutdown_status(self):
        s = load_settings()
        deadline = s.get("shutdown_deadline", 0)
        if deadline <= 0:
            return {"active": False, "remaining": 0}
        remaining = int(deadline - time.time())
        if remaining <= 0:
            s["shutdown_deadline"] = 0
            save_settings(s)
            return {"active": False, "remaining": 0}
        return {"active": True, "remaining": remaining}

    def start_disk_scan(self, path="C:\\"):
        global _disk_scan
        if _disk_scan.get("running", False):
            return {"ok": False, "msg": "正在扫描中，请稍候"}
        _disk_scan = {"running": True, "current": path, "results": [], "done": False, "error": "", "root": path}
        t = threading.Thread(target=_scan_disk_worker, args=(path,), daemon=True)
        t.start()
        return {"ok": True}

    def get_disk_scan_status(self):
        return dict(_disk_scan)

    def detect_chat_apps(self):
        try:
            return detect_chat_apps()
        except Exception as e:
            return {"wechat": [], "qq": [], "error": str(e)}

    def scan_chat_account(self, account_path, app_type):
        try:
            return scan_chat_dir(account_path, app_type)
        except Exception:
            return []

    def clean_chat_category(self, paths, min_age_days, dry_run=False):
        try:
            min_age_days = int(min_age_days)
        except Exception:
            min_age_days = 30
        files, total_size, skipped_danger = collect_chat_files(paths, min_age_days)
        if dry_run:
            return {"count": len(files), "size": total_size, "skippedDanger": skipped_danger}
        freed = 0
        deleted = 0
        errors = 0
        for f in files:
            try:
                sz = os.path.getsize(f)
                send2trash.send2trash(f)
                freed += sz
                deleted += 1
            except Exception:
                errors += 1
        if freed > 0:
            try:
                h = load_history()
                h["records"].append({"time": time.time(), "freed": freed, "count": deleted})
                h["records"] = h["records"][-100:]
                save_history(h)
            except Exception:
                pass
        return {"freed": freed, "deleted": deleted, "errors": errors, "skippedDanger": skipped_danger}

    def start_dup_scan(self, path, min_size_kb=1024):
        global _dup_scan
        if _dup_scan.get("running", False):
            return {"ok": False, "msg": "正在扫描中"}
        _dup_scan = {"running": True, "current": path, "phase": "准备", "progress": 0, "done": False, "error": "",
                     "groups": [], "scanned": 0, "total": 0}
        t = threading.Thread(target=_dup_scan_worker, args=(path, int(min_size_kb)), daemon=True)
        t.start()
        return {"ok": True}

    def get_dup_scan_status(self):
        return dict(_dup_scan)

    def delete_dup_files(self, paths):
        freed = 0
        deleted = 0
        errors = 0
        for fp in paths:
            try:
                sz = os.path.getsize(fp)
                send2trash.send2trash(fp)
                freed += sz
                deleted += 1
            except Exception:
                errors += 1
        if freed > 0:
            try:
                h = load_history()
                h["records"].append({"time": time.time(), "freed": freed, "count": deleted})
                h["records"] = h["records"][-100:]
                save_history(h)
            except Exception:
                pass
        return {"freed": freed, "deleted": deleted, "errors": errors}

    def start_empty_scan(self, path):
        global _empty_scan
        if _empty_scan.get("running", False):
            return {"ok": False, "msg": "正在扫描中"}
        _empty_scan = {"running": True, "current": path, "done": False, "error": "", "folders": [], "scanned": 0}
        t = threading.Thread(target=_empty_scan_worker, args=(path,), daemon=True)
        t.start()
        return {"ok": True}

    def get_empty_scan_status(self):
        return dict(_empty_scan)

    def delete_empty_folders(self, folders):
        deleted = 0
        errors = 0
        for d in folders:
            try:
                os.rmdir(d)
                deleted += 1
            except Exception:
                errors += 1
        return {"deleted": deleted, "errors": errors}

    def start_residue_scan(self):
        global _residue_scan
        if _residue_scan.get("running", False):
            return {"ok": False, "msg": "正在扫描中"}
        _residue_scan = {"running": True, "current": "准备中...", "done": False, "error": "", "results": [],
                         "scanned": 0}
        t = threading.Thread(target=_residue_scan_worker, daemon=True)
        t.start()
        return {"ok": True}

    def get_residue_scan_status(self):
        return dict(_residue_scan)

    def delete_residue_folders(self, folders):
        freed = 0
        deleted = 0
        errors = 0
        for d in folders:
            try:
                size = _get_dir_size_fast(d)
                send2trash.send2trash(d)
                freed += size
                deleted += 1
            except Exception:
                errors += 1
        if freed > 0:
            try:
                h = load_history()
                h["records"].append({"time": time.time(), "freed": freed, "count": deleted})
                h["records"] = h["records"][-100:]
                save_history(h)
            except Exception:
                pass
        return {"freed": freed, "deleted": deleted, "errors": errors}


api = Api()

_s = load_settings()
_win = _s.get("window", {"width": 960, "height": 780})
_w = max(720, int(_win.get("width", 960)))
_h = max(560, int(_win.get("height", 780)))

_main_window = webview.create_window(
    "C 盘清理大师",
    os.path.join(RES_DIR, "index.html"),
    js_api=api,
    width=_w, height=_h,
    min_size=(720, 560)
)


def _on_closing():
    global _quitting
    if _quitting:
        return True
    s = load_settings()
    if s.get("minimize_to_tray", True) and HAS_TRAY:
        try:
            _main_window.hide()
        except Exception:
            pass
        return False
    try:
        s.setdefault("window", {})
        s["window"]["width"] = int(_main_window.width)
        s["window"]["height"] = int(_main_window.height)
        save_settings(s)
    except Exception:
        pass
    return True


_main_window.events.closing += _on_closing

_start_tray()

webview.start()