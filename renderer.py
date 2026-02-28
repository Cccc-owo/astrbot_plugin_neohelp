from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, select_autoescape
from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright


_env = Environment(autoescape=select_autoescape(default_for_string=True, default=True))

_browser: Browser | None = None
_playwright_instance: Playwright | None = None
_lock = asyncio.Lock()
_semaphore = asyncio.Semaphore(3)  # 限制同时渲染的页面数


async def _get_browser():
    """懒加载复用浏览器实例"""
    global _browser, _playwright_instance
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        # 清理旧实例，防止资源泄漏
        if _browser:
            with contextlib.suppress(Exception):
                await _browser.close()
            _browser = None
        if _playwright_instance:
            with contextlib.suppress(Exception):
                await _playwright_instance.stop()
            _playwright_instance = None
        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"],
        )
        return _browser


_TIMEOUT = 30_000  # 30 秒超时


async def render_template(tmpl_str: str, data: dict) -> bytes:
    """用 Jinja2 渲染模板，然后用 Playwright 截图，返回 PNG bytes"""
    html = _env.from_string(tmpl_str).render(**data)

    # 写入临时文件（Playwright 需要 file:// URL 来正确加载）
    fd, tmp_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)

        async with _semaphore:
            browser = await _get_browser()
            page = await browser.new_page(device_scale_factor=2)
            try:
                await page.goto(Path(tmp_path).as_uri(), wait_until="networkidle", timeout=_TIMEOUT)
                # 等待网络字体加载完成
                await page.wait_for_function("() => document.fonts.ready", timeout=_TIMEOUT)
                # 从 body 获取实际渲染尺寸（body 宽度由 CSS 变量精确计算）
                dimensions = await page.evaluate(
                    """() => {
                        const body = document.body;
                        const style = getComputedStyle(body);
                        return {
                            width: parseInt(style.width) || body.scrollWidth,
                            height: body.scrollHeight
                        };
                    }"""
                )
                await page.set_viewport_size({"width": dimensions["width"], "height": dimensions["height"]})
                screenshot = await page.screenshot(full_page=True, type="png", timeout=_TIMEOUT)
                return screenshot
            finally:
                await page.close()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def cleanup():
    """关闭浏览器实例（插件卸载时调用）"""
    global _browser, _playwright_instance
    async with _lock:
        if _browser:
            with contextlib.suppress(Exception):
                await _browser.close()
            _browser = None
        if _playwright_instance:
            with contextlib.suppress(Exception):
                await _playwright_instance.stop()
            _playwright_instance = None
