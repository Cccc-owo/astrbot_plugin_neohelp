import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.star.context import Context as FullContext

from . import renderer
from .collector import PluginCollector
from .models import CommandInfo, PluginInfo
from .utils import DEFAULT_LOGO_PATH, PLUGIN_DIR, read_image_as_data_uri, read_template


class CustomHelpPlugin(Star):
    # ==================== 生命周期 ====================

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ctx: FullContext = context  # type: ignore[assignment]
        self._plugins_base_dir = PLUGIN_DIR.parent
        self._data_dir = StarTools.get_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._image_cache: dict[str, bytes] = {}
        self._terminated = False
        self._preheat_task: asyncio.Task | None = None
        self._render_locks: dict[str, asyncio.Lock] = {}
        self._disk_cache = bool(getattr(self.config, "disk_cache", False))
        self._cache_dir = self._data_dir / "cache"
        if self._disk_cache:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._collector = PluginCollector(self._ctx, self.config, self._plugins_base_dir)

    async def initialize(self):
        """插件完全加载后启动缓存预热"""
        self._preheat_task = asyncio.create_task(self._preheat_cache())

    async def terminate(self):
        """插件卸载时关闭浏览器并清理缓存"""
        self._terminated = True
        if self._preheat_task and not self._preheat_task.done():
            self._preheat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._preheat_task
        if not self._disk_cache:
            self._image_cache.clear()
        await renderer.cleanup()

    # ==================== 缓存 ====================

    @staticmethod
    def _cache_key(template_name: str, data: dict) -> str:
        """根据模板名和数据内容生成缓存 key"""
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        h = hashlib.md5(raw.encode()).hexdigest()
        name = template_name.replace(".html", "")
        return f"{name}_{h}"

    def _disk_cache_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.png"

    async def _get_cached_or_render(self, template_name: str, template: str, data: dict) -> bytes:
        """查缓存，命中直接返回；未命中则加锁渲染并存入缓存"""
        key = self._cache_key(template_name, data)
        _debug = getattr(self.config, "debug", False)

        # 读缓存（无锁快路径）
        if self._disk_cache:
            cache_file = self._disk_cache_path(key)
            try:
                if cache_file.is_file():
                    content = cache_file.read_bytes()
                    if content:
                        if _debug:
                            logger.info(f"[NeoHelp] disk cache hit: {template_name}")
                        return content
                    cache_file.unlink(missing_ok=True)
            except OSError:
                with contextlib.suppress(OSError):
                    cache_file.unlink(missing_ok=True)
        else:
            cached = self._image_cache.get(key)
            if cached is not None:
                if _debug:
                    logger.info(f"[NeoHelp] memory cache hit: {template_name}")
                return cached

        # 加 key 级别锁，防止并发重复渲染
        lock = self._render_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # double-check：拿到锁后再查一次缓存
            if self._disk_cache:
                cache_file = self._disk_cache_path(key)
                try:
                    if cache_file.is_file():
                        content = cache_file.read_bytes()
                        if content:
                            return content
                except OSError:
                    pass
            else:
                cached = self._image_cache.get(key)
                if cached is not None:
                    return cached

            if _debug:
                logger.info(f"[NeoHelp] cache miss: {template_name}, rendering...")
            img_bytes = await renderer.render_template(template, data)

            # 写缓存
            if self._disk_cache:
                try:
                    self._disk_cache_path(key).write_bytes(img_bytes)
                except OSError as e:
                    logger.warning(f"[NeoHelp] 磁盘缓存写入失败: {e}")
            else:
                self._image_cache[key] = img_bytes
            self._render_locks.pop(key, None)
            return img_bytes

    async def _preheat_cache(self):
        """预热缓存（主菜单 + 所有子菜单，普通版 + 管理员版）"""
        # 短暂等待，避免插件更新时双重加载导致重复预热
        await asyncio.sleep(1)
        if self._terminated:
            return
        logger.info("[NeoHelp] 开始缓存预热...")
        try:
            # 记录本轮预热生成的 cache key，用于清理过期磁盘缓存
            valid_keys: set[str] = set()

            for show_all in (False, True):
                if self._terminated:
                    return
                await self._preheat_main_menu(show_all, valid_keys)

            # 稍微延时后预热所有子菜单
            await asyncio.sleep(2)
            for show_all in (False, True):
                if self._terminated:
                    return
                await self._preheat_sub_menus(show_all, valid_keys)

            if not self._terminated:
                # 磁盘模式：清理过期缓存文件
                if self._disk_cache:
                    self._cleanup_disk_cache(valid_keys)
                else:
                    # 内存模式：移除过期缓存
                    self._image_cache = {k: v for k, v in self._image_cache.items() if k in valid_keys}
                count = len(valid_keys)
                logger.info(f"[NeoHelp] 缓存预热完成，共 {count} 项")
        except Exception as e:
            if not self._terminated:
                logger.warning(f"[NeoHelp] 缓存预热失败: {e}")

    def _cleanup_disk_cache(self, valid_keys: set[str]):
        """删除不再有效的磁盘缓存文件"""
        if not self._cache_dir.is_dir():
            return
        removed = 0
        for f in self._cache_dir.iterdir():
            if f.suffix == ".png" and f.stem not in valid_keys:
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(f"[NeoHelp] 清理过期磁盘缓存 {removed} 项")

    async def _preheat_main_menu(self, show_all: bool, valid_keys: set[str]):
        """预热单份主菜单缓存"""
        plugins = self._collect_plugins(skip_blacklist=show_all)
        plugins = [p for p in plugins if p.commands]
        if not plugins:
            return

        expand = getattr(self.config, "expand_commands", False)
        template_name = "expanded_menu.html" if expand else "main_menu.html"
        custom_dir = self._data_dir / "custom_templates" if getattr(self.config, "custom_templates", False) else None
        template = read_template(template_name, custom_dir)
        prefix = self._get_wake_prefix()

        data = self._build_main_menu_data(plugins, prefix, expand)
        valid_keys.add(self._cache_key(template_name, data))
        await self._get_cached_or_render(template_name, template, data)

    async def _preheat_sub_menus(self, show_all: bool, valid_keys: set[str]):
        """预热所有插件的子菜单缓存"""
        plugins = self._collect_plugins(skip_blacklist=show_all)
        plugins = [p for p in plugins if p.commands]
        if not plugins:
            return

        custom_dir = self._data_dir / "custom_templates" if getattr(self.config, "custom_templates", False) else None
        template = read_template("sub_menu.html", custom_dir)
        prefix = self._get_wake_prefix()

        for p in plugins:
            if self._terminated:
                return
            data = self._build_sub_menu_data(p, prefix)
            valid_keys.add(self._cache_key("sub_menu.html", data))
            await self._get_cached_or_render("sub_menu.html", template, data)

    # ==================== 命令入口 ====================

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断消息发送者是否为 AstrBot 管理员"""
        sender_id = str(event.get_sender_id())
        admins = [str(a) for a in self._ctx.get_config().get("admins_id", [])]
        return sender_id in admins

    def _get_wake_prefix(self) -> str:
        """获取 AstrBot 唤醒前缀（取第一个，默认 '/'）"""
        prefixes = self._ctx.get_config().get("wake_prefix", ["/"])
        if prefixes and isinstance(prefixes, list):
            return prefixes[0]
        return "/"

    @filter.command("help", alias={"帮助", "菜单", "功能"})
    async def help_command(self, event: AstrMessageEvent, query: str = ""):
        """查看帮助菜单"""
        query = query.strip()

        # 解析 --admin 标志
        is_admin = self._is_admin(event)
        parts = query.split()
        has_admin_flag = "--admin" in parts
        if has_admin_flag:
            parts.remove("--admin")
            query = " ".join(parts)

        admin_show_all = getattr(self.config, "admin_show_all", False)
        show_all = is_admin and (has_admin_flag or admin_show_all)

        if getattr(self.config, "debug", False):
            logger.info(
                f"[NeoHelp] sender={event.get_sender_id()}, "
                f"admins={self._ctx.get_config().get('admins_id', [])}, "
                f"is_admin={is_admin}, has_admin_flag={has_admin_flag}, "
                f"admin_show_all={admin_show_all}, show_all={show_all}, "
                f"query={query!r}"
            )

        if query:
            yield await self._render_sub_menu(event, query, show_all)
        else:
            yield await self._render_main_menu(event, show_all)

    # ==================== 数据收集 ====================

    def _collect_plugins(self, skip_blacklist: bool = False) -> list[PluginInfo]:
        """委托给 PluginCollector 收集插件信息"""
        return self._collector.collect(skip_blacklist=skip_blacklist)

    # ==================== 渲染配置 ====================

    @staticmethod
    def _cmd_display_name(cmd: CommandInfo, prefix: str) -> str:
        """生成命令的显示名称，拼接正确的前缀"""
        p = prefix if cmd.custom_prefix is None else cmd.custom_prefix
        return f"{p}{cmd.name}"

    def _get_footer(self) -> str:
        custom = getattr(self.config, "footer_text", "") or ""
        if custom:
            return custom
        version = getattr(self.config, "version", "")
        return f"AstrBot{' v' + version if version else ''}"

    def _get_accent_color(self) -> str:
        color = getattr(self.config, "accent_color", "#4e96f7") or "#4e96f7"
        if not color.startswith("#") or len(color) not in (4, 7):
            return "#4e96f7"
        return color

    def _resolve_data_path(self, raw: str) -> Path | None:
        """将相对路径解析到数据目录，校验不越界，返回 None 表示非法"""
        p = Path(raw)
        if not p.is_absolute():
            p = self._data_dir / p
        resolved = p.resolve()
        if not resolved.is_relative_to(self._data_dir.resolve()):
            logger.warning(f"路径越界，已拒绝: {raw}")
            return None
        return resolved

    def _get_banner_data_uri(self) -> str:
        """读取 Banner 背景图，路径相对于插件数据目录"""
        banner_path_str = getattr(self.config, "banner_image", "") or ""
        if not banner_path_str:
            return ""
        banner_path = self._resolve_data_path(banner_path_str)
        if not banner_path:
            return ""
        return read_image_as_data_uri(banner_path)

    def _get_header_logo_uri(self) -> str:
        """获取顶部 Logo 的 data URI，优先使用配置，否则使用插件自带 logo.svg"""
        logo_path_str = getattr(self.config, "header_logo", "") or ""
        if logo_path_str:
            logo_path = self._resolve_data_path(logo_path_str)
            if logo_path:
                uri = read_image_as_data_uri(logo_path)
                if uri:
                    return uri
        return read_image_as_data_uri(DEFAULT_LOGO_PATH)

    def _get_font_config(self) -> dict:
        """获取自定义字体配置"""
        font_urls_raw = getattr(self.config, "font_urls", []) or []
        font_urls = [u.strip() for u in font_urls_raw if isinstance(u, str) and u.strip()]
        font_family = (getattr(self.config, "font_family", "") or "").strip()
        latin_font_family = (getattr(self.config, "latin_font_family", "") or "").strip()
        mono_font_family = (getattr(self.config, "mono_font_family", "") or "").strip()
        return {
            "font_urls": font_urls,
            "font_family": font_family,
            "latin_font_family": latin_font_family,
            "mono_font_family": mono_font_family,
        }

    # ==================== 数据构建与渲染 ====================

    def _build_main_menu_data(self, plugins: list[PluginInfo], prefix: str, expand: bool) -> dict:
        """构建主菜单模板数据"""
        title = getattr(self.config, "title", "帮助菜单") or "帮助菜单"
        default_subtitle = f"发送 {prefix}help <插件名> 查看详细命令"
        subtitle = getattr(self.config, "subtitle", "") or default_subtitle

        if expand:
            plugins_data = [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "description": p.description,
                    "icon_url": p.icon_url,
                    "commands": [
                        {
                            "display_name": self._cmd_display_name(c, prefix),
                            "description": c.description,
                            "admin_only": c.admin_only,
                        }
                        for c in p.commands
                    ],
                }
                for p in plugins
            ]
        else:
            plugins_data = [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "description": p.description,
                    "icon_url": p.icon_url,
                    "cmd_count": len(p.commands),
                }
                for p in plugins
            ]

        return {
            "title": title,
            "subtitle": subtitle,
            "prefix": prefix,
            "accent_color": self._get_accent_color(),
            "banner_image": self._get_banner_data_uri(),
            "header_logo": self._get_header_logo_uri(),
            **self._get_font_config(),
            "plugins": plugins_data,
            "footer": self._get_footer(),
        }

    def _build_sub_menu_data(self, plugin: PluginInfo, prefix: str) -> dict:
        """构建子菜单模板数据"""
        return {
            "plugin": {
                "name": plugin.name,
                "display_name": plugin.display_name,
                "description": plugin.description,
                "icon_url": plugin.icon_url,
            },
            "commands": [
                {
                    "display_name": self._cmd_display_name(c, prefix),
                    "description": c.description,
                    "aliases": c.aliases,
                    "usage": c.usage,
                    "admin_only": c.admin_only,
                }
                for c in plugin.commands
            ],
            "prefix": prefix,
            "accent_color": self._get_accent_color(),
            **self._get_font_config(),
            "footer": self._get_footer(),
        }

    async def _render_main_menu(self, event: AstrMessageEvent, show_all: bool = False):
        """渲染主菜单"""
        plugins = self._collect_plugins(skip_blacklist=show_all)
        plugins = [p for p in plugins if p.commands]

        if not plugins:
            return event.plain_result("没有找到任何可用的插件命令。")

        expand = getattr(self.config, "expand_commands", False)
        template_name = "expanded_menu.html" if expand else "main_menu.html"
        custom_dir = self._data_dir / "custom_templates" if getattr(self.config, "custom_templates", False) else None
        template = read_template(template_name, custom_dir)
        prefix = self._get_wake_prefix()
        data = self._build_main_menu_data(plugins, prefix, expand)

        try:
            img_bytes = await self._get_cached_or_render(template_name, template, data)
        except Exception as e:
            logger.error(f"渲染主菜单失败: {e}")
            return event.plain_result("渲染帮助菜单失败，请稍后重试。")
        return event.chain_result([Image.fromBytes(img_bytes)])

    async def _render_sub_menu(self, event: AstrMessageEvent, query: str, show_all: bool = False):
        """渲染子菜单（某个插件的详细命令）"""
        plugins = self._collect_plugins(skip_blacklist=show_all)

        target = None
        query_lower = query.lower()

        for p in plugins:
            if p.name.lower() == query_lower or p.display_name.lower() == query_lower:
                target = p
                break

        if not target:
            for p in plugins:
                if query_lower in p.name.lower() or query_lower in p.display_name.lower():
                    target = p
                    break

        if not target:
            prefix = self._get_wake_prefix()
            return event.plain_result(f"未找到插件「{query}」，请发送 {prefix}help 查看所有可用插件。")

        custom_dir = self._data_dir / "custom_templates" if getattr(self.config, "custom_templates", False) else None
        template = read_template("sub_menu.html", custom_dir)
        prefix = self._get_wake_prefix()
        data = self._build_sub_menu_data(target, prefix)

        try:
            img_bytes = await self._get_cached_or_render("sub_menu.html", template, data)
        except Exception as e:
            logger.error(f"渲染子菜单失败: {e}")
            return event.plain_result("渲染帮助菜单失败，请稍后重试。")
        return event.chain_result([Image.fromBytes(img_bytes)])
