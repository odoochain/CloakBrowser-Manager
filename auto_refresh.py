"""
CloakBrowser 批量自动刷新脚本

支持两种运行模式：
  1. manager 模式（默认）：连接 CloakBrowser Manager (Docker)，通过 CDP API 控制
  2. local 模式（--local）：直接在 Windows 本地用 Playwright 启动浏览器，无需 Docker

使用方法：
  # Manager 模式（需要先 docker compose up -d）
  python auto_refresh.py

  # 本地模式（无需 Docker，直接在 Windows 运行）
  python auto_refresh.py --local

  # 首次使用 local 模式时，加 --setup-logins 会打开浏览器供手动登录
  python auto_refresh.py --local --setup-logins

功能：
- 批量管理多个浏览器 Profile
- 并发刷新目标页面获取积分
- 随机化刷新间隔，模拟真实用户行为
- 登录状态检测与自动处理
- Cookie 备份与恢复
- 完整的日志记录和错误处理
"""

import argparse
import asyncio
import json
import random
import signal
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ============================================================================
# 配置区域 - 根据你的实际情况修改
# ============================================================================

CONFIG = {
    # ── Manager 模式配置 ──────────────────────────────────────────────
    # CloakBrowser Manager 地址
    "base_url": "http://localhost:8080",
    # 如果设置了 AUTH_TOKEN，填在这里；否则留空字符串
    "auth_token": "",

    # ── 本地模式配置 ──────────────────────────────────────────────────
    # 本地 Profile 数据目录（每个账号一个子目录，保存 cookies/session）
    "local_profile_dir": "local_profiles",
    # 本地模式下使用的浏览器通道（留空使用 Playwright 自带 Chromium）
    # 可选: "chrome", "msedge", "" (Chromium)
    "local_browser_channel": "",
    # 本地模式是否使用 headless（无头模式）
    # 首次登录时建议设 False 以便手动操作；日常运行可设 True
    "local_headless": False,
    # 本地模式每个浏览器的视口宽度/高度
    "local_viewport_width": 1920,
    "local_viewport_height": 1080,

    # ── 通用配置 ──────────────────────────────────────────────────────
    # 账号列表（local 模式必须填写；manager 模式留空则使用所有已创建的 profile）
    # 每个账号对应一个独立的浏览器 Profile
    "accounts": [
        # {"name": "Account-01"},
        # {"name": "Account-02"},
    ],

    # 目标网站 URL（刷新获取积分的页面）
    "target_url": "https://example.com/dashboard",
    # 目标网站的登录页面 URL（用于检测登录状态）
    "login_url": "https://example.com/login",
    # GitHub 登录页面 URL
    "github_login_url": "https://github.com/login",

    # 可选：签到按钮的 CSS 选择器（留空则不点击）
    "claim_button_selector": "",  # 例如 "button.claim-btn"
    # 可选：积分显示元素的 CSS 选择器（留空则不读取）
    "points_selector": "",  # 例如 ".points-value"

    # 刷新间隔（秒），每次刷新会在此范围内随机取值
    "refresh_interval_min": 300,   # 5 分钟
    "refresh_interval_max": 600,   # 10 分钟

    # 启动 profile 后等待浏览器就绪的时间（秒）
    "launch_wait_time": 15,
    # 每次刷新后等待页面加载的时间（秒）
    "page_load_wait": 3,
    # 最大重试次数
    "max_retries": 3,
    # 重试等待时间（秒）
    "retry_wait": 10,

    # 日志级别: DEBUG, INFO, WARNING, ERROR
    "log_level": "INFO",
    # 日志文件路径（留空则只输出到控制台）
    "log_file": "auto_refresh.log",
    # 截图目录（留空则不截图）
    "screenshot_dir": "",
    # 登录失败截图目录
    "login_fail_screenshot_dir": "login_failures",

    # 只刷新指定的 profile 名称（留空则刷新所有）
    "only_profiles": [],
    # 跳过指定的 profile 名称
    "skip_profiles": [],

    # Cookie 备份目录
    "cookie_backup_dir": "cookies",
    # 连续失败次数阈值，达到后暂停该账号
    "failure_threshold": 3,
    # 是否在检测到登录过期时自动停止该账号
    "auto_stop_on_login_expire": True,

    # ── 调度模式 ──────────────────────────────────────────────────────
    # 运行模式: "loop" | "once" | "schedule"
    #   loop     — 无限循环刷新（默认，配合 refresh_interval_min/max）
    #   once     — 只刷新一轮就退出
    #   schedule — 在指定时间段内循环刷新，时间段外休眠
    "run_mode": "loop",
    # schedule 模式的时间窗口，格式 "HH:MM-HH:MM"（24小时制）
    # 例如 "09:00-23:00" 表示每天 9:00 到 23:00 之间运行
    "schedule_window": "09:00-23:00",
    # 最大运行轮数（0 = 无限）。设置后不管什么模式，跑完 N 轮就退出
    "max_rounds": 0,
    # schedule 模式下，休眠期间的检查间隔（秒）
    "schedule_sleep_check_interval": 300,
}

# ============================================================================
# 日志配置
# ============================================================================

def setup_logging():
    """配置日志"""
    level = getattr(logging, CONFIG["log_level"].upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    if CONFIG["log_file"]:
        Path(CONFIG["log_file"]).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(CONFIG["log_file"], encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    return logging.getLogger("auto-refresh")

logger = setup_logging()

# ============================================================================
# 全局状态
# ============================================================================

shutdown_event = asyncio.Event()

stats = {
    "total_refreshes": 0,
    "successful_refreshes": 0,
    "failed_refreshes": 0,
    "login_expired_count": 0,
    "start_time": None,
}

account_status = {}  # {name: {"consecutive_failures": int, "last_error": str, "login_expired": bool}}

# ============================================================================
# 登录状态检测器
# ============================================================================

class LoginDetector:
    """检测登录状态"""

    GITHUB_LOGIN_INDICATORS = [
        "github.com/login",
        "github.com/session",
        "Sign in to GitHub",
    ]

    TARGET_LOGIN_INDICATORS = [
        "/login",
        "/signin",
        "/auth",
        "Sign in",
        "Log in",
        "登录",
    ]

    def __init__(self):
        self.github_login_url = CONFIG["github_login_url"]
        self.target_login_url = CONFIG["login_url"]

    async def check_github_login(self, page: Page) -> bool:
        """检查 GitHub 是否已登录"""
        try:
            current_url = page.url
            if any(ind in current_url for ind in self.GITHUB_LOGIN_INDICATORS):
                return False
            login_form = await page.query_selector(
                'input[name="login"], input[name="user[login]"], input[name="username"]'
            )
            if login_form:
                return False
            sign_in_btn = await page.query_selector(
                'a[href="/login"], button:has-text("Sign in")'
            )
            if sign_in_btn:
                if await sign_in_btn.is_visible():
                    return False
            avatar = await page.query_selector(
                'img[alt*="@github"], .avatar-user, .Header-link--profile'
            )
            if avatar:
                return True
            return True
        except Exception as e:
            logger.warning(f"检查 GitHub 登录状态时出错: {e}")
            return True

    async def check_target_login(self, page: Page) -> bool:
        """检查目标网站是否已登录"""
        try:
            current_url = page.url
            if self.target_login_url and self.target_login_url in current_url:
                return False
            for ind in self.TARGET_LOGIN_INDICATORS:
                if ind in current_url.lower():
                    return False
            login_form = await page.query_selector(
                'form[action*="login"], form[action*="signin"]'
            )
            if login_form:
                return False
            login_required = await page.query_selector(
                ':has-text("Please log in"), :has-text("请登录"), :has-text("Login required")'
            )
            if login_required:
                return False
            return True
        except Exception as e:
            logger.warning(f"检查目标网站登录状态时出错: {e}")
            return True

    async def check_all_logins(self, page: Page) -> tuple[bool, bool]:
        """返回 (github_logged_in, target_logged_in)"""
        return await self.check_github_login(page), await self.check_target_login(page)


# ============================================================================
# Cookie 管理器
# ============================================================================

class CookieManager:
    """管理 Cookie 备份和恢复"""

    def __init__(self):
        self.backup_dir = Path(CONFIG["cookie_backup_dir"])
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def get_cookie_path(self, account_name: str) -> Path:
        safe_name = account_name.replace(" ", "_").replace("/", "_")
        return self.backup_dir / f"{safe_name}_cookies.json"

    async def save_cookies(self, account_name: str, context) -> bool:
        try:
            cookies = await context.cookies()
            cookie_path = self.get_cookie_path(account_name)
            with open(cookie_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            logger.info(f"[{account_name}] Cookies 已保存 ({len(cookies)} 条)")
            return True
        except Exception as e:
            logger.error(f"[{account_name}] 保存 Cookies 失败: {e}")
            return False

    async def load_cookies(self, account_name: str, context) -> bool:
        try:
            cookie_path = self.get_cookie_path(account_name)
            if not cookie_path.exists():
                return False
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if not cookies:
                return False
            await context.add_cookies(cookies)
            logger.info(f"[{account_name}] 已加载 {len(cookies)} 个 Cookies")
            return True
        except Exception as e:
            logger.error(f"[{account_name}] 加载 Cookies 失败: {e}")
            return False


# ============================================================================
# Manager 模式 API 客户端
# ============================================================================

class CloakBrowserAPI:
    """CloakBrowser Manager API 客户端（Manager 模式）"""

    def __init__(self, base_url: str, auth_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers = {}
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self.headers, **kwargs)
            resp.raise_for_status()
            return resp

    async def list_profiles(self) -> list[dict]:
        resp = await self._request("GET", "/api/profiles")
        return resp.json()

    async def launch_profile(self, profile_id: str) -> dict:
        resp = await self._request("POST", f"/api/profiles/{profile_id}/launch")
        return resp.json()

    async def stop_profile(self, profile_id: str) -> dict:
        resp = await self._request("POST", f"/api/profiles/{profile_id}/stop")
        return resp.json()

    async def get_status(self) -> dict:
        resp = await self._request("GET", "/api/status")
        return resp.json()

    async def launch_all_profiles(self, wait: bool = True) -> list[dict]:
        profiles = await self.list_profiles()
        launched = []
        for p in profiles:
            if p["status"] == "stopped":
                if CONFIG["skip_profiles"] and p["name"] in CONFIG["skip_profiles"]:
                    continue
                if CONFIG["only_profiles"] and p["name"] not in CONFIG["only_profiles"]:
                    continue
                try:
                    await self.launch_profile(p["id"])
                    logger.info(f"✓ 启动: {p['name']}")
                    launched.append(p)
                    await asyncio.sleep(2)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 409:
                        launched.append(p)
                    else:
                        logger.error(f"✗ 启动失败 {p['name']}: {e}")
                except Exception as e:
                    logger.error(f"✗ 启动失败 {p['name']}: {e}")
        if wait and launched:
            logger.info(f"等待 {CONFIG['launch_wait_time']}s 让浏览器就绪...")
            await asyncio.sleep(CONFIG["launch_wait_time"])
        return launched


# ============================================================================
# 本地模式浏览器管理器
# ============================================================================

class LocalBrowserManager:
    """
    本地模式：直接用 Playwright 管理浏览器，不依赖 Docker/Manager。

    每个账号对应一个独立的 user_data_dir（持久化目录），
    cookies/session 会自动保存到该目录中。
    """

    def __init__(self):
        self.profile_dir = Path(CONFIG["local_profile_dir"])
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.running: dict[str, BrowserContext] = {}  # account_name -> context
        self._playwright = None

    @property
    def playwright(self):
        return self._playwright

    def get_profile_path(self, account_name: str) -> Path:
        safe_name = account_name.replace(" ", "_").replace("/", "_")
        return self.profile_dir / safe_name

    def list_accounts(self) -> list[dict]:
        """列出所有本地 profile（目录存在的）"""
        accounts = []
        for d in sorted(self.profile_dir.iterdir()):
            if d.is_dir():
                accounts.append({"name": d.name, "id": d.name})
        return accounts

    async def start(self):
        """启动 Playwright"""
        self._playwright = await async_playwright().start()
        logger.info("本地浏览器管理器已启动")

    async def stop(self):
        """关闭所有浏览器并停止 Playwright"""
        for name in list(self.running.keys()):
            await self.close_account(name)
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("本地浏览器管理器已停止")

    async def launch_account(self, account_name: str, headless: bool = None) -> BrowserContext:
        """
        启动一个账号的浏览器（持久化 context）。

        首次启动会创建新的 user_data_dir。
        后续启动会复用已有的 session/cookies。
        """
        if account_name in self.running:
            logger.debug(f"[{account_name}] 浏览器已在运行")
            return self.running[account_name]

        user_data_dir = self.get_profile_path(account_name)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_opts = {
            "headless": headless if headless is not None else CONFIG["local_headless"],
            "viewport": {
                "width": CONFIG["local_viewport_width"],
                "height": CONFIG["local_viewport_height"],
            },
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }

        # 使用指定浏览器通道（chrome / msedge）
        channel = CONFIG.get("local_browser_channel", "")
        if channel:
            launch_opts["channel"] = channel

        context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            **launch_opts,
        )
        self.running[account_name] = context
        logger.info(f"[{account_name}] 浏览器已启动 (headless={launch_opts['headless']})")
        return context

    async def close_account(self, account_name: str):
        """关闭一个账号的浏览器"""
        context = self.running.pop(account_name, None)
        if context:
            try:
                await context.close()
                logger.info(f"[{account_name}] 浏览器已关闭")
            except Exception as e:
                logger.warning(f"[{account_name}] 关闭浏览器出错: {e}")

    async def close_all(self):
        """关闭所有浏览器"""
        for name in list(self.running.keys()):
            await self.close_account(name)

    async def setup_logins(self, account_names: list[str]):
        """
        交互式登录模式：逐个打开浏览器，让用户手动登录 GitHub + 目标网站。
        登录完成后按回车继续下一个。
        """
        logger.info("=" * 60)
        logger.info("交互式登录模式")
        logger.info("=" * 60)
        logger.info(f"将为 {len(account_names)} 个账号逐个打开浏览器")
        logger.info("每个账号需要：1. 登录 GitHub  2. 用 GitHub 登录目标网站")
        logger.info("登录完成后回到终端按回车继续下一个")
        logger.info("=" * 60)

        for i, name in enumerate(account_names, 1):
            logger.info(f"\n--- [{i}/{len(account_names)}] {name} ---")

            # 以非 headless 模式打开
            context = await self.launch_account(name, headless=False)
            page = context.pages[0] if context.pages else await context.new_page()

            # 打开 GitHub 登录页
            await page.goto(CONFIG["github_login_url"])
            logger.info(f"[{name}] 已打开 GitHub 登录页，请在浏览器中完成登录")
            logger.info(f"[{name}] GitHub 登录后，请继续用 GitHub OAuth 登录目标网站:")
            logger.info(f"       {CONFIG['target_url']}")

            # 等待用户按回车
            await asyncio.get_event_loop().run_in_executor(None, input, "登录完成后按回车继续...")

            # 保存当前页面 URL 用于日志
            logger.info(f"[{name}] 当前页面: {page.url}")

            # 关闭浏览器（session 会自动保存到 user_data_dir）
            await self.close_account(name)
            logger.info(f"[{name}] ✓ 登录已保存")

        logger.info("=" * 60)
        logger.info("所有账号登录完成！")
        logger.info("现在可以用 --local 模式运行自动刷新了")
        logger.info("=" * 60)


# ============================================================================
# 刷新任务
# ============================================================================

async def save_failure_screenshot(account_name: str, page: Optional[Page], reason: str):
    """保存登录失败的截图"""
    if not CONFIG["login_fail_screenshot_dir"] or not page:
        return
    try:
        screenshot_dir = Path(CONFIG["login_fail_screenshot_dir"])
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{account_name}_{reason}_{timestamp}.png"
        screenshot_path = screenshot_dir / filename
        await page.screenshot(path=str(screenshot_path))
        logger.info(f"[{account_name}] 失败截图已保存: {screenshot_path}")
    except Exception as e:
        logger.warning(f"[{account_name}] 保存失败截图出错: {e}")


def init_account_status(account_name: str):
    """初始化账号状态追踪"""
    if account_name not in account_status:
        account_status[account_name] = {
            "consecutive_failures": 0,
            "last_error": None,
            "login_expired": False,
        }


# ── Manager 模式刷新 ──────────────────────────────────────────────────

async def refresh_account_manager(
    playwright,
    api: CloakBrowserAPI,
    profile: dict,
    login_detector: LoginDetector,
    cookie_manager: CookieManager,
    retry_count: int = 0,
):
    """Manager 模式：通过 CDP 连接 CloakBrowser Manager 的浏览器"""
    profile_id = profile["id"]
    account_name = profile["name"]
    browser: Optional[Browser] = None

    init_account_status(account_name)
    if account_status[account_name]["login_expired"]:
        return

    try:
        cdp_url = f"{api.base_url}/api/profiles/{profile_id}/cdp"
        browser = await playwright.chromium.connect_over_cdp(cdp_url)

        contexts = browser.contexts
        if not contexts:
            context = await browser.new_context()
            page = await context.new_page()
        else:
            context = contexts[0]
            pages = context.pages
            page = pages[0] if pages else await context.new_page()

        # 检查登录状态
        logger.info(f"[{account_name}] 检查登录状态...")
        github_ok, target_ok = await login_detector.check_all_logins(page)

        if not github_ok:
            logger.warning(f"[{account_name}] GitHub 登录已过期")
            if await cookie_manager.load_cookies(account_name, context):
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(3)
                github_ok, target_ok = await login_detector.check_all_logins(page)
            if not github_ok:
                logger.error(f"[{account_name}] ✗ GitHub 登录恢复失败，需要手动重新登录")
                await save_failure_screenshot(account_name, page, "github_login_expired")
                account_status[account_name]["login_expired"] = True
                account_status[account_name]["last_error"] = "GitHub 登录过期"
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    try:
                        await api.stop_profile(profile_id)
                    except:
                        pass
                return

        if not target_ok:
            logger.warning(f"[{account_name}] 目标网站登录已过期")
            if await cookie_manager.load_cookies(account_name, context):
                await page.goto(CONFIG["target_url"], wait_until="networkidle")
                await asyncio.sleep(3)
                _, target_ok = await login_detector.check_all_logins(page)
            if not target_ok:
                logger.error(f"[{account_name}] ✗ 目标网站登录恢复失败")
                await save_failure_screenshot(account_name, page, "target_login_expired")
                account_status[account_name]["login_expired"] = True
                account_status[account_name]["last_error"] = "目标网站登录过期"
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    try:
                        await api.stop_profile(profile_id)
                    except:
                        pass
                return

        # 执行刷新
        logger.info(f"[{account_name}] 正在刷新...")
        await page.goto(CONFIG["target_url"], wait_until="networkidle", timeout=30000)
        await asyncio.sleep(CONFIG["page_load_wait"])

        _, target_ok = await login_detector.check_all_logins(page)
        if not target_ok:
            logger.error(f"[{account_name}] 刷新后被重定向到登录页")
            await save_failure_screenshot(account_name, page, "redirected_after_refresh")
            account_status[account_name]["consecutive_failures"] += 1
            stats["failed_refreshes"] += 1
            if account_status[account_name]["consecutive_failures"] >= CONFIG["failure_threshold"]:
                account_status[account_name]["login_expired"] = True
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    try:
                        await api.stop_profile(profile_id)
                    except:
                        pass
            return

        await _do_refresh_actions(account_name, page)
        await cookie_manager.save_cookies(account_name, context)
        logger.info(f"[{account_name}] ✓ 刷新成功")
        stats["successful_refreshes"] += 1
        account_status[account_name]["consecutive_failures"] = 0

    except Exception as e:
        logger.error(f"[{account_name}] ✗ 刷新失败: {e}")
        stats["failed_refreshes"] += 1
        account_status[account_name]["consecutive_failures"] += 1
        account_status[account_name]["last_error"] = str(e)
        if account_status[account_name]["consecutive_failures"] >= CONFIG["failure_threshold"]:
            logger.warning(f"[{account_name}] 连续失败 {account_status[account_name]['consecutive_failures']} 次，标记为异常")
        if retry_count < CONFIG["max_retries"]:
            logger.info(f"[{account_name}] 尝试重试 ({retry_count + 1}/{CONFIG['max_retries']})")
            await asyncio.sleep(CONFIG["retry_wait"])
            await refresh_account_manager(playwright, api, profile, login_detector, cookie_manager, retry_count + 1)
    finally:
        if browser:
            try:
                await browser.close()
            except:
                pass
    stats["total_refreshes"] += 1


# ── 本地模式刷新 ──────────────────────────────────────────────────────

async def refresh_account_local(
    local_mgr: LocalBrowserManager,
    account: dict,
    login_detector: LoginDetector,
    cookie_manager: CookieManager,
    retry_count: int = 0,
):
    """本地模式：直接操作本地 Playwright 浏览器"""
    account_name = account["name"]

    init_account_status(account_name)
    if account_status[account_name]["login_expired"]:
        return

    context: Optional[BrowserContext] = None
    page: Optional[Page] = None

    try:
        # 启动（或复用）该账号的浏览器
        context = await local_mgr.launch_account(account_name)
        page = context.pages[0] if context.pages else await context.new_page()

        # 检查登录状态
        logger.info(f"[{account_name}] 检查登录状态...")
        github_ok, target_ok = await login_detector.check_all_logins(page)

        if not github_ok:
            logger.warning(f"[{account_name}] GitHub 登录已过期")
            if await cookie_manager.load_cookies(account_name, context):
                await page.reload(wait_until="networkidle")
                await asyncio.sleep(3)
                github_ok, target_ok = await login_detector.check_all_logins(page)
            if not github_ok:
                logger.error(f"[{account_name}] ✗ GitHub 登录恢复失败，需要手动重新登录")
                await save_failure_screenshot(account_name, page, "github_login_expired")
                account_status[account_name]["login_expired"] = True
                account_status[account_name]["last_error"] = "GitHub 登录过期"
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    await local_mgr.close_account(account_name)
                return

        if not target_ok:
            logger.warning(f"[{account_name}] 目标网站登录已过期")
            if await cookie_manager.load_cookies(account_name, context):
                await page.goto(CONFIG["target_url"], wait_until="networkidle")
                await asyncio.sleep(3)
                _, target_ok = await login_detector.check_all_logins(page)
            if not target_ok:
                logger.error(f"[{account_name}] ✗ 目标网站登录恢复失败")
                await save_failure_screenshot(account_name, page, "target_login_expired")
                account_status[account_name]["login_expired"] = True
                account_status[account_name]["last_error"] = "目标网站登录过期"
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    await local_mgr.close_account(account_name)
                return

        # 执行刷新
        logger.info(f"[{account_name}] 正在刷新...")
        await page.goto(CONFIG["target_url"], wait_until="networkidle", timeout=30000)
        await asyncio.sleep(CONFIG["page_load_wait"])

        _, target_ok = await login_detector.check_all_logins(page)
        if not target_ok:
            logger.error(f"[{account_name}] 刷新后被重定向到登录页")
            await save_failure_screenshot(account_name, page, "redirected_after_refresh")
            account_status[account_name]["consecutive_failures"] += 1
            stats["failed_refreshes"] += 1
            if account_status[account_name]["consecutive_failures"] >= CONFIG["failure_threshold"]:
                account_status[account_name]["login_expired"] = True
                stats["login_expired_count"] += 1
                if CONFIG["auto_stop_on_login_expire"]:
                    await local_mgr.close_account(account_name)
            return

        await _do_refresh_actions(account_name, page)
        # 本地模式下 cookies 自动持久化在 user_data_dir 中，但额外备份一份
        await cookie_manager.save_cookies(account_name, context)
        logger.info(f"[{account_name}] ✓ 刷新成功")
        stats["successful_refreshes"] += 1
        account_status[account_name]["consecutive_failures"] = 0

    except Exception as e:
        logger.error(f"[{account_name}] ✗ 刷新失败: {e}")
        stats["failed_refreshes"] += 1
        account_status[account_name]["consecutive_failures"] += 1
        account_status[account_name]["last_error"] = str(e)
        if account_status[account_name]["consecutive_failures"] >= CONFIG["failure_threshold"]:
            logger.warning(f"[{account_name}] 连续失败 {account_status[account_name]['consecutive_failures']} 次，标记为异常")
        if retry_count < CONFIG["max_retries"]:
            logger.info(f"[{account_name}] 尝试重试 ({retry_count + 1}/{CONFIG['max_retries']})")
            await asyncio.sleep(CONFIG["retry_wait"])
            await refresh_account_local(local_mgr, account, login_detector, cookie_manager, retry_count + 1)
    finally:
        # 本地模式下不关闭浏览器（保持 session 活跃），只在登录过期时关闭
        pass

    stats["total_refreshes"] += 1


async def _do_refresh_actions(account_name: str, page: Page):
    """刷新后的通用操作：读积分、点签到、截图"""

    # 读取积分
    if CONFIG["points_selector"]:
        try:
            points_el = await page.query_selector(CONFIG["points_selector"])
            if points_el:
                points = await points_el.text_content()
                logger.info(f"[{account_name}] 当前积分: {points}")
        except Exception as e:
            logger.warning(f"[{account_name}] 读取积分失败: {e}")

    # 点击签到按钮
    if CONFIG["claim_button_selector"]:
        try:
            claim_btn = await page.query_selector(CONFIG["claim_button_selector"])
            if claim_btn:
                if await claim_btn.is_visible():
                    await claim_btn.click()
                    await asyncio.sleep(2)
                    logger.info(f"[{account_name}] 已点击签到")
                else:
                    logger.debug(f"[{account_name}] 签到按钮不可见，可能已签到")
            else:
                logger.debug(f"[{account_name}] 未找到签到按钮")
        except Exception as e:
            logger.warning(f"[{account_name}] 点击签到失败: {e}")

    # 截图
    if CONFIG["screenshot_dir"]:
        try:
            screenshot_dir = Path(CONFIG["screenshot_dir"])
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = screenshot_dir / f"{account_name}_{timestamp}.png"
            await page.screenshot(path=str(screenshot_path))
            logger.debug(f"[{account_name}] 截图保存: {screenshot_path}")
        except Exception as e:
            logger.warning(f"[{account_name}] 截图失败: {e}")


# ============================================================================
# 调度辅助
# ============================================================================

def parse_schedule_window(window: str) -> tuple[int, int]:
    """
    解析时间窗口字符串 "HH:MM-HH:MM" → (start_minutes, end_minutes)。
    返回一天中的分钟数（0-1439）。
    """
    try:
        start_str, end_str = window.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        start = sh * 60 + sm
        end = eh * 60 + em
        if start < 0 or start > 1439 or end < 0 or end > 1439:
            raise ValueError("时间超出范围")
        return start, end
    except Exception as e:
        raise ValueError(
            f'无效的时间窗口 "{window}"，请使用 "HH:MM-HH:MM" 格式，例如 "09:00-23:00": {e}'
        )


def now_in_minutes() -> int:
    """当前时间换算成一天中的分钟数 (0-1439)"""
    now = datetime.now()
    return now.hour * 60 + now.minute


def is_in_schedule_window() -> bool:
    """检查当前时间是否在配置的时间窗口内"""
    window = CONFIG["schedule_window"]
    if not window:
        return True
    start, end = parse_schedule_window(window)
    now = now_in_minutes()

    if start <= end:
        return start <= now < end
    else:
        # 跨午夜，比如 "22:00-06:00"
        return now >= start or now < end


def seconds_until_window_open() -> int:
    """距离时间窗口打开还有多少秒"""
    window = CONFIG["schedule_window"]
    if not window:
        return 0
    start, _ = parse_schedule_window(window)
    now = now_in_minutes()
    if now < start:
        return (start - now) * 60
    else:
        # 今天已过，等明天
        return (start + 1440 - now) * 60


# ============================================================================
# 刷新主循环
# ============================================================================

async def _wait_or_break(seconds: int):
    """分段等待，期间响应 shutdown 信号。返回 True 表示被中断。"""
    for _ in range(seconds):
        if shutdown_event.is_set():
            return True
        await asyncio.sleep(1)
    return False


async def _run_one_round_manager(
    playwright, api: CloakBrowserAPI, profiles, login_detector, cookie_manager,
):
    """执行一轮 Manager 模式刷新"""
    active = [
        p for p in profiles
        if not account_status.get(p["name"], {}).get("login_expired", False)
    ]
    if not active:
        logger.warning("所有账号都已标记为登录过期")
        return False

    shuffled = active.copy()
    random.shuffle(shuffled)
    logger.info(f"=== 开始新一轮刷新 ({len(shuffled)} 个账号，{len(profiles) - len(shuffled)} 个已暂停) ===")

    tasks = []
    for profile in shuffled:
        if shutdown_event.is_set():
            break
        tasks.append(asyncio.create_task(
            refresh_account_manager(playwright, api, profile, login_detector, cookie_manager)
        ))
        await asyncio.sleep(random.uniform(0.5, 2.0))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    expired = sum(1 for s in account_status.values() if s.get("login_expired"))
    if expired > 0:
        logger.warning(f"⚠ {expired} 个账号需要手动重新登录")

    return True


async def refresh_loop_manager(
    playwright,
    api: CloakBrowserAPI,
    profiles: list[dict],
    login_detector: LoginDetector,
    cookie_manager: CookieManager,
):
    """Manager 模式刷新主循环（支持 once / max_rounds / schedule 调度）"""
    run_mode = CONFIG["run_mode"]
    max_rounds = CONFIG["max_rounds"]
    round_count = 0

    while not shutdown_event.is_set():
        # ── schedule 模式：检查是否在时间窗口内 ──
        if run_mode == "schedule":
            if not is_in_schedule_window():
                sleep_secs = seconds_until_window_open()
                logger.info(
                    f"当前时间不在运行窗口 "
                    f"({CONFIG['schedule_window']})，休眠 {sleep_secs // 60} 分钟后自动恢复"
                )
                # 分段休眠，每隔一段时间检查一次
                check_int = CONFIG["schedule_sleep_check_interval"]
                while not shutdown_event.is_set() and not is_in_schedule_window():
                    wait = min(check_int, sleep_secs)
                    if await _wait_or_break(wait):
                        break
                    sleep_secs -= wait
                if shutdown_event.is_set():
                    break
                logger.info("进入运行时间窗口，开始刷新")
            else:
                logger.info(f"在运行窗口内 ({CONFIG['schedule_window']})")

        # ── 执行一轮刷新 ──
        ran = await _run_one_round_manager(
            playwright, api, profiles, login_detector, cookie_manager
        )

        round_count += 1
        logger.info(f"已完成第 {round_count} 轮刷新")

        # ── once 模式：刷新一轮就退出 ──
        if run_mode == "once":
            logger.info("--once 模式：刷新一轮完成，退出")
            break

        # ── max_rounds：达到最大轮数退出 ──
        if max_rounds > 0 and round_count >= max_rounds:
            logger.info(f"已达最大轮数 {max_rounds}，退出")
            break

        # ── 所有账号都过期了 ──
        if not ran:
            break

        if shutdown_event.is_set():
            break

        # ── 等待下一轮 ──
        wait_time = random.randint(CONFIG["refresh_interval_min"], CONFIG["refresh_interval_max"])
        logger.info(f"下一轮刷新将在 {wait_time}s ({wait_time // 60}m {wait_time % 60}s) 后开始")
        await _wait_or_break(wait_time)


async def _run_one_round_local(
    local_mgr: LocalBrowserManager, accounts, login_detector, cookie_manager,
):
    """执行一轮本地模式刷新"""
    active = [
        a for a in accounts
        if not account_status.get(a["name"], {}).get("login_expired", False)
    ]
    if not active:
        logger.warning("所有账号都已标记为登录过期")
        return False

    shuffled = active.copy()
    random.shuffle(shuffled)
    logger.info(f"=== 开始新一轮刷新 ({len(shuffled)} 个账号，{len(accounts) - len(shuffled)} 个已暂停) ===")

    tasks = []
    for account in shuffled:
        if shutdown_event.is_set():
            break
        tasks.append(asyncio.create_task(
            refresh_account_local(local_mgr, account, login_detector, cookie_manager)
        ))
        await asyncio.sleep(random.uniform(0.5, 2.0))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    expired = sum(1 for s in account_status.values() if s.get("login_expired"))
    if expired > 0:
        logger.warning(f"⚠ {expired} 个账号需要手动重新登录")

    return True


async def refresh_loop_local(
    local_mgr: LocalBrowserManager,
    accounts: list[dict],
    login_detector: LoginDetector,
    cookie_manager: CookieManager,
):
    """本地模式刷新主循环（支持 once / max_rounds / schedule 调度）"""
    run_mode = CONFIG["run_mode"]
    max_rounds = CONFIG["max_rounds"]
    round_count = 0

    while not shutdown_event.is_set():
        # ── schedule 模式：检查时间窗口 ──
        if run_mode == "schedule":
            if not is_in_schedule_window():
                sleep_secs = seconds_until_window_open()
                logger.info(
                    f"当前时间不在运行窗口 "
                    f"({CONFIG['schedule_window']})，休眠 {sleep_secs // 60} 分钟后自动恢复"
                )
                # 在 schedule 休眠期间关闭浏览器以释放资源
                if local_mgr.running:
                    logger.info("休眠期间关闭浏览器以释放资源...")
                    await local_mgr.close_all()

                check_int = CONFIG["schedule_sleep_check_interval"]
                while not shutdown_event.is_set() and not is_in_schedule_window():
                    wait = min(check_int, sleep_secs)
                    if await _wait_or_break(wait):
                        break
                    sleep_secs -= wait

                if shutdown_event.is_set():
                    break

                # 时间窗口打开，重新启动浏览器
                logger.info("进入运行时间窗口，重新启动浏览器...")
                for a in accounts:
                    if shutdown_event.is_set():
                        break
                    name = a["name"]
                    if not account_status.get(name, {}).get("login_expired", False):
                        await local_mgr.launch_account(name)
                        await asyncio.sleep(1)
                await asyncio.sleep(3)
            else:
                logger.info(f"在运行窗口内 ({CONFIG['schedule_window']})")

        # ── 执行一轮刷新 ──
        ran = await _run_one_round_local(local_mgr, accounts, login_detector, cookie_manager)

        round_count += 1
        logger.info(f"已完成第 {round_count} 轮刷新")

        # ── once 模式 ──
        if run_mode == "once":
            logger.info("--once 模式：刷新一轮完成，退出")
            break

        # ── max_rounds ──
        if max_rounds > 0 and round_count >= max_rounds:
            logger.info(f"已达最大轮数 {max_rounds}，退出")
            break

        # ── 全部过期 ──
        if not ran:
            break

        if shutdown_event.is_set():
            break

        # ── 等待下一轮 ──
        wait_time = random.randint(CONFIG["refresh_interval_min"], CONFIG["refresh_interval_max"])
        logger.info(f"下一轮刷新将在 {wait_time}s ({wait_time // 60}m {wait_time % 60}s) 后开始")
        await _wait_or_break(wait_time)


# ============================================================================
# 主函数
# ============================================================================

async def main_manager():
    """Manager 模式主函数"""
    stats["start_time"] = datetime.now()

    logger.info("=" * 60)
    logger.info("CloakBrowser 批量自动刷新脚本 [Manager 模式]")
    logger.info("=" * 60)
    logger.info(f"目标 URL: {CONFIG['target_url']}")
    logger.info(f"刷新间隔: {CONFIG['refresh_interval_min']}-{CONFIG['refresh_interval_max']}s")
    logger.info(f"运行模式: {CONFIG['run_mode']}" + (f", 窗口: {CONFIG['schedule_window']}" if CONFIG['run_mode'] == 'schedule' else ''))
    if CONFIG['max_rounds'] > 0:
        logger.info(f"最大轮数: {CONFIG['max_rounds']}")
    else:
        logger.info("最大轮数: 无限")

    api = CloakBrowserAPI(CONFIG["base_url"], CONFIG["auth_token"])
    login_detector = LoginDetector()
    cookie_manager = CookieManager()

    try:
        status = await api.get_status()
        logger.info(f"系统状态: {status['running_count']} 个 profile 运行中")
    except Exception as e:
        logger.error(f"无法连接到 CloakBrowser Manager: {e}")
        logger.error(f"请确认服务已启动: {CONFIG['base_url']}")
        logger.error(f"或者使用本地模式: python auto_refresh.py --local")
        return

    logger.info("正在启动所有 profile...")
    profiles = await api.launch_all_profiles(wait=True)
    if not profiles:
        logger.error("没有可用的 profile，请先在 Web UI 中创建并登录")
        return

    active_profiles = []
    for p in profiles:
        if CONFIG["skip_profiles"] and p["name"] in CONFIG["skip_profiles"]:
            continue
        if CONFIG["only_profiles"] and p["name"] not in CONFIG["only_profiles"]:
            continue
        active_profiles.append(p)

    logger.info(f"准备刷新 {len(active_profiles)} 个账号:")
    for p in active_profiles:
        logger.info(f"  - {p['name']}")

    async with async_playwright() as pw:
        logger.info("开始刷新循环 (按 Ctrl+C 停止)")
        await refresh_loop_manager(pw, api, active_profiles, login_detector, cookie_manager)

    print_stats()


async def main_local(args):
    """本地模式主函数"""
    stats["start_time"] = datetime.now()

    logger.info("=" * 60)
    logger.info("CloakBrowser 批量自动刷新脚本 [本地模式]")
    logger.info("=" * 60)
    logger.info(f"目标 URL: {CONFIG['target_url']}")
    logger.info(f"刷新间隔: {CONFIG['refresh_interval_min']}-{CONFIG['refresh_interval_max']}s")
    logger.info(f"运行模式: {CONFIG['run_mode']}" + (f", 窗口: {CONFIG['schedule_window']}" if CONFIG['run_mode'] == 'schedule' else ''))
    if CONFIG['max_rounds'] > 0:
        logger.info(f"最大轮数: {CONFIG['max_rounds']}")
    else:
        logger.info("最大轮数: 无限")
    logger.info(f"Profile 目录: {CONFIG['local_profile_dir']}")
    logger.info(f"Headless: {CONFIG['local_headless']}")

    local_mgr = LocalBrowserManager()
    await local_mgr.start()

    try:
        # ── 交互式登录模式 ──
        if args.setup_logins:
            accounts = CONFIG["accounts"] if CONFIG["accounts"] else local_mgr.list_accounts()
            if not accounts:
                logger.error("没有配置账号，请在 CONFIG['accounts'] 中添加账号名")
                logger.error("例如: {'name': 'Account-01'}")
                return
            account_names = [a["name"] for a in accounts]
            await local_mgr.setup_logins(account_names)
            return

        # ── 正常刷新模式 ──
        # 获取账号列表
        if CONFIG["accounts"]:
            accounts = CONFIG["accounts"]
        else:
            accounts = local_mgr.list_accounts()

        if not accounts:
            logger.error("没有找到本地 profile，请先运行:")
            logger.error(f"  python auto_refresh.py --local --setup-logins")
            return

        # 过滤账号
        active_accounts = []
        for a in accounts:
            name = a["name"]
            if CONFIG["skip_profiles"] and name in CONFIG["skip_profiles"]:
                continue
            if CONFIG["only_profiles"] and name not in CONFIG["only_profiles"]:
                continue
            active_accounts.append(a)

        logger.info(f"准备刷新 {len(active_accounts)} 个账号:")
        for a in active_accounts:
            logger.info(f"  - {a['name']}")

        # 启动所有账号的浏览器
        logger.info("正在启动浏览器...")
        for a in active_accounts:
            if shutdown_event.is_set():
                break
            await local_mgr.launch_account(a["name"])
            await asyncio.sleep(1)

        await asyncio.sleep(3)

        login_detector = LoginDetector()
        cookie_manager = CookieManager()

        logger.info("开始刷新循环 (按 Ctrl+C 停止)")
        await refresh_loop_local(local_mgr, active_accounts, login_detector, cookie_manager)

        print_stats()

    finally:
        await local_mgr.stop()


def print_stats():
    """打印统计信息"""
    elapsed = datetime.now() - stats["start_time"]
    minutes = elapsed.total_seconds() / 60

    logger.info("=" * 60)
    logger.info("运行统计")
    logger.info("=" * 60)
    logger.info(f"运行时长: {minutes:.1f} 分钟")
    logger.info(f"总刷新次数: {stats['total_refreshes']}")
    logger.info(f"成功: {stats['successful_refreshes']}")
    logger.info(f"失败: {stats['failed_refreshes']}")
    logger.info(f"登录过期: {stats['login_expired_count']}")
    if stats['total_refreshes'] > 0:
        success_rate = stats['successful_refreshes'] / stats['total_refreshes'] * 100
        logger.info(f"成功率: {success_rate:.1f}%")

    logger.info("-" * 60)
    logger.info("账号状态:")
    for name, st in account_status.items():
        if st.get("login_expired"):
            logger.info(f"  {name}: 登录过期 - {st.get('last_error', '未知')}")
        elif st.get("consecutive_failures", 0) > 0:
            logger.info(f"  {name}: 连续失败 {st['consecutive_failures']} 次")
        else:
            logger.info(f"  {name}: 正常")


def signal_handler(sig, frame):
    """处理 Ctrl+C 信号"""
    logger.info("收到停止信号，正在优雅退出...")
    shutdown_event.set()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="CloakBrowser 批量自动刷新脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用示例:
  # Manager 模式（需要 Docker）
  python auto_refresh.py

  # 本地模式（无需 Docker，Windows 直接运行）
  python auto_refresh.py --local

  # 本地模式 - 首次登录
  python auto_refresh.py --local --setup-logins

  # 本地模式 - 无头模式
  python auto_refresh.py --local --headless

  # 指定浏览器通道（使用系统已安装的 Chrome）
  python auto_refresh.py --local --channel chrome

  # 只刷新一轮就退出
  python auto_refresh.py --local --once

  # 刷新 10 轮后退出
  python auto_refresh.py --local --max-rounds 10

  # 每天只在 09:00-23:00 之间循环刷新，其他时间休眠
  python auto_refresh.py --local --schedule "09:00-23:00"

  # 组合：本地模式 + 每天 09:00-23:00 运行 + 最多 50 轮
  python auto_refresh.py --local --schedule "09:00-23:00" --max-rounds 50
""",    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="使用本地模式（不依赖 Docker/Manager，直接用 Playwright 管理浏览器）",
    )
    parser.add_argument(
        "--setup-logins",
        action="store_true",
        help="交互式登录模式：逐个打开浏览器手动登录（仅本地模式有效）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头模式（不显示浏览器窗口）",
    )
    parser.add_argument(
        "--channel",
        type=str,
        default="",
        choices=["", "chrome", "msedge"],
        help="指定浏览器通道: chrome, msedge, 或留空使用 Playwright Chromium",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只刷新一轮就退出（不循环）",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="最大刷新轮数（0=无限，默认 0）。达到后自动退出",
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default="",
        help='定时运行窗口，格式 "HH:MM-HH:MM"。例如 "09:00-23:00" 表示每天该时段运行',
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # 命令行参数覆盖配置
    if args.headless:
        CONFIG["local_headless"] = True
    if args.channel:
        CONFIG["local_browser_channel"] = args.channel
    if args.once:
        CONFIG["run_mode"] = "once"
    elif args.schedule:
        CONFIG["run_mode"] = "schedule"
        CONFIG["schedule_window"] = args.schedule
    if args.max_rounds:
        CONFIG["max_rounds"] = args.max_rounds

    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.local:
            asyncio.run(main_local(args))
        else:
            asyncio.run(main_manager())
    except KeyboardInterrupt:
        logger.info("程序已停止")
