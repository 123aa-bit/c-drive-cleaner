import os
import time
import subprocess
import safety
import psutil

# 白名单（由 main.py 注入）
_whitelist = set()


def set_whitelist(procs):
    global _whitelist
    _whitelist = set(p.lower() for p in procs)


def _is_whitelisted(name):
    return name.lower() in _whitelist


def clean_path(path_template, min_age_hours=24):
    path = os.path.expandvars(path_template)
    if not os.path.exists(path):
        return 0, 0, 0, {}

    total_freed = 0
    deleted_count = 0
    skipped_count = 0
    locked_files = []
    now = time.time()
    min_age_sec = min_age_hours * 3600

    for dirpath, _, filenames in os.walk(path, onerror=lambda x: None):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            safe, reason = safety.is_safe(fp)
            if not safe:
                skipped_count += 1
                continue
            try:
                if now - os.path.getmtime(fp) < min_age_sec:
                    skipped_count += 1
                    continue
                size = os.path.getsize(fp)
                os.remove(fp)
                total_freed += size
                deleted_count += 1
            except (OSError, PermissionError):
                skipped_count += 1
                locked_files.append(fp)

    if locked_files:
        blocking_info = find_locking_processes_batch(locked_files)
    else:
        blocking_info = {}

    return total_freed, deleted_count, skipped_count, blocking_info


def find_locking_processes_batch(filepaths):
    """批量查找占用文件的进程，返回 {文件路径: [{pid, name}, ...]}"""
    target = {}
    for fp in filepaths:
        try:
            target[os.path.normcase(os.path.abspath(fp))] = fp
        except Exception:
            pass

    result = {fp: [] for fp in filepaths}

    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc_name = proc.info['name'] or ""
            if _is_whitelisted(proc_name):
                continue  # 白名单进程直接跳过，不记录
            for f in proc.open_files():
                norm = os.path.normcase(os.path.abspath(f.path))
                if norm in target:
                    original = target[norm]
                    result[original].append({
                        "pid": proc.info['pid'],
                        "name": proc_name
                    })
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    return {k: v for k, v in result.items() if v}


def kill_process_by_pid(pid):
    """结束指定 PID 的进程"""
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        # 安全保护：拒绝杀系统关键进程
        dangerous = ['system', 'system idle process', 'csrss.exe', 'wininit.exe', 'services.exe', 'lsass.exe',
                     'smss.exe']
        if name.lower() in dangerous:
            return {"ok": False, "msg": f"{name} 是系统关键进程，禁止结束"}
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
        return {"ok": True, "msg": f"已结束 {name}"}
    except psutil.NoSuchProcess:
        return {"ok": False, "msg": "进程已经不存在"}
    except psutil.AccessDenied:
        return {"ok": False, "msg": "无权限结束该进程，请以管理员身份运行"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def run_system_command(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return 0, 0, 1, result.stderr[:200]
        return -1, 0, 0, ""
    except Exception as e:
        return 0, 0, 1, str(e)