"""
auto_refresh 本地覆盖配置模板。

复制为 auto_refresh.local.py 后填入真实值。
auto_refresh.local.py 已被 .gitignore 忽略，不会提交到仓库。

任何 auto_refresh.py 中 CONFIG 的键都可以在这里覆盖，未列出的沿用默认值。
"""

CONFIG = {
    # 账号列表（local 模式必须）
    "accounts": [
        {"name": "Account-01"},
        {"name": "Account-02"},
    ],

    # 真实目标网站
    "target_url": "https://your-site.example.com/",
    "login_url": "https://your-site.example.com/login",

    # 其他常见覆盖示例：
    # "auth_token": "xxx",
    # "run_mode": "schedule",
    # "schedule_window": "09:00-23:00",
}
