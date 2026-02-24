import contextlib
import os
import tempfile

from jinja2 import Template
from playwright.async_api import async_playwright

_browser = None
_playwright_instance = None


async def _get_browser():
    """懒加载复用浏览器实例"""
    global _browser, _playwright_instance
    if _browser and _browser.is_connected():
        return _browser
    _playwright_instance = await async_playwright().start()
    _browser = await _playwright_instance.chromium.launch(
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"],
    )
    return _browser


async def render_template(tmpl_str: str, data: dict) -> bytes:
    """用 Jinja2 渲染模板，然后用 Playwright 截图，返回 PNG bytes"""
    html = Template(tmpl_str).render(**data)

    # 写入临时文件（Playwright 需要 file:// URL 来正确加载）
    fd, tmp_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)

        browser = await _get_browser()
        page = await browser.new_page(device_scale_factor=2)
        try:
            await page.goto(f"file://{tmp_path}", wait_until="load")
            # 用 JS 获取完整内容尺寸，避免截断
            dimensions = await page.evaluate(
                """() => {
                    const body = document.body;
                    return {
                        width: body.scrollWidth,
                        height: body.scrollHeight
                    };
                }"""
            )
            await page.set_viewport_size({"width": dimensions["width"], "height": dimensions["height"]})
            screenshot = await page.screenshot(full_page=True, type="png")
            return screenshot
        finally:
            await page.close()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def cleanup():
    """关闭浏览器实例（插件卸载时调用）"""
    global _browser, _playwright_instance
    if _browser:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
    if _playwright_instance:
        with contextlib.suppress(Exception):
            await _playwright_instance.stop()
        _playwright_instance = None
