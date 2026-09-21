import os

# 绝对禁区（写死在代码里，哪怕规则写错，这里也会拦截）
FORBIDDEN_PATHS = [
    "C:\\Windows\\System32",
    "C:\\Windows\\WinSxS",
    "C:\\Windows\\SysWOW64",
    "C:\\System Volume Information"
]

def is_safe(path):
    """返回 (是否安全, 拦截原因)"""
    p = os.path.abspath(path).lower()
    for forbidden in FORBIDDEN_PATHS:
        if p.startswith(forbidden.lower()):
            return False, f"位于系统保护区：{forbidden}"
    return True, ""