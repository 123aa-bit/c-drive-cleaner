import os
import time

# 全局扫描进度
_scan_status = {"running": False, "current": "", "rule": ""}


def get_scan_status():
    return dict(_scan_status)


def scan_path(path_template, min_age_hours=24, rule_name=""):
    global _scan_status
    path = os.path.expandvars(path_template)
    if not os.path.exists(path):
        return 0, 0

    _scan_status["running"] = True
    _scan_status["rule"] = rule_name
    _scan_status["current"] = path

    total_size = 0
    file_count = 0
    now = time.time()
    min_age_seconds = min_age_hours * 3600

    for dirpath, _, filenames in os.walk(path, onerror=lambda x: None):
        _scan_status["current"] = dirpath
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                mtime = os.path.getmtime(fp)
                if now - mtime < min_age_seconds:
                    continue
                total_size += os.path.getsize(fp)
                file_count += 1
            except (OSError, PermissionError):
                pass

    _scan_status["running"] = False
    return total_size, file_count


def get_file_warning(fp, size):
    fn = os.path.basename(fp).lower()
    ext = os.path.splitext(fn)[1]
    path_lower = fp.lower()

    if fn in ['pagefile.sys', 'hiberfil.sys',
              'swapfile.sys'] or '\\windows\\' in path_lower or '\\system32\\' in path_lower:
        return {"label": "系统核心，禁止删除", "color": "danger", "can_delete": False}

    personal_exts = ['.mp4', '.avi', '.mkv', '.mov', '.flv', '.zip', '.rar', '.7z', '.tar', '.gz', '.jpg', '.png',
                     '.gif', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.pdf', '.txt']
    if ext in personal_exts:
        return {"label": "个人数据（视频/压缩包等），请确认", "color": "warning", "can_delete": True}

    cache_keywords = ['cache', 'temp', 'log', '.tmp', '.log', 'dump']
    if any(k in path_lower for k in cache_keywords):
        return {"label": "缓存/日志，可安全清理", "color": "safe", "can_delete": True}

    return {"label": "未知文件，请确认内容", "color": "unknown", "can_delete": True}


def scan_large_files(drive="C:\\", top_n=50):
    large_files = []
    exclude_dirs = ["Windows", "Program Files", "Program Files (x86)", "ProgramData", "$Recycle.Bin"]

    for dirpath, dirnames, filenames in os.walk(drive, onerror=lambda x: None):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                size = os.path.getsize(fp)
                if size > 50 * 1024 * 1024:
                    warning = get_file_warning(fp, size)
                    large_files.append({"path": fp, "size": size, "warning": warning})
            except (OSError, PermissionError):
                pass

    large_files.sort(key=lambda x: x["size"], reverse=True)
    return large_files[:top_n]