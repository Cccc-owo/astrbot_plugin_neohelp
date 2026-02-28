import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.star.context import Context as FullContext
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import StarHandlerMetadata, star_handlers_registry

from . import renderer

PLUGIN_NAME = "astrbot_plugin_neohelp"
PLUGIN_DIR = Path(__file__).parent
TEMPLATES_DIR = PLUGIN_DIR / "templates"
RESOURCES_DIR = PLUGIN_DIR / "resources"
DEFAULT_ICON_PATH = RESOURCES_DIR / "default_icon.webp"
DEFAULT_LOGO_PATH = RESOURCES_DIR / "logo.svg"


@dataclass
class CommandInfo:
    name: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    admin_only: bool = False
    custom_prefix: str | None = None  # None 表示使用全局唤醒前缀，"" 表示无前缀


@dataclass
class PluginInfo:
    name: str
    display_name: str = ""
    description: str = ""
    commands: list[CommandInfo] = field(default_factory=list)
    icon_url: str = ""  # base64 data URI 或空
    order: int = 99

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name


def _read_template(name: str, data_dir: Path | None = None) -> str:
    """读取模板文件，开启自定义模板时优先从数据目录加载"""
    if data_dir:
        custom_path = data_dir / name
        if custom_path.is_file():
            try:
                with open(custom_path, encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"读取自定义模板失败 {custom_path}: {e}，回退到默认模板")
    path = TEMPLATES_DIR / name
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_image_as_data_uri(path: Path) -> str:
    """读取图片文件并转为 base64 data URI"""
    if not path.is_file():
        return ""
    try:
        mime, _ = mimetypes.guess_type(str(path))
        if not mime:
            mime = "image/png"
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning(f"读取图片失败 {path}: {e}")
        return ""


# 缓存默认图标 data URI（只读一次）
_default_icon_uri: str | None = None


def _get_default_icon_uri() -> str:
    global _default_icon_uri
    if _default_icon_uri is None:
        _default_icon_uri = _read_image_as_data_uri(DEFAULT_ICON_PATH)
    return _default_icon_uri


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
                # 清理不再需要的锁
                self._render_locks = {k: v for k, v in self._render_locks.items() if k in valid_keys}
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
        template = _read_template(template_name, custom_dir)
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
        template = _read_template("sub_menu.html", custom_dir)
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
        sender_id = event.get_sender_id()
        admins = self._ctx.get_config().get("admins_id", [])
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

    def _get_plugin_icon_uri(self, root_dir_name: str | None) -> str:
        """获取插件图标的 data URI，找不到则返回默认图标"""
        if root_dir_name:
            logo_path = self._plugins_base_dir / root_dir_name / "logo.png"
            uri = _read_image_as_data_uri(logo_path)
            if uri:
                return uri
        return _get_default_icon_uri()

    def _collect_plugins(self, skip_blacklist: bool = False) -> list[PluginInfo]:
        """从已安装插件中自动收集命令信息"""
        _debug = getattr(self.config, "debug", False)
        plugins: dict[str, PluginInfo] = {}
        blacklist: set[str] = set()
        if not skip_blacklist:
            blacklist = set(getattr(self.config, "plugin_blacklist", []) or [])
        blacklist.add(PLUGIN_NAME)
        show_builtin = getattr(self.config, "show_builtin_cmds", False)

        if _debug:
            logger.info(
                f"[NeoHelp] _collect_plugins: skip_blacklist={skip_blacklist}, "
                f"blacklist={blacklist}, show_builtin={show_builtin}"
            )

        try:
            all_stars = self._ctx.get_all_stars()
            all_stars = [s for s in all_stars if s.activated]
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return []

        if _debug:
            star_names = [f"{getattr(s, 'name', '?')}(reserved={getattr(s, 'reserved', '?')})" for s in all_stars]
            logger.info(f"[NeoHelp] activated stars: {star_names}")

        # 收集插件基本信息
        star_modules: dict[str, str] = {}  # module_path -> plugin_name
        for star in all_stars:
            name = getattr(star, "name", None)
            if not name or name in blacklist:
                continue
            module_path = getattr(star, "module_path", None)
            if not module_path:
                continue

            # 过滤内置 star（reserved=True），除非开启了显示内置命令
            if getattr(star, "reserved", False) and not show_builtin:
                continue

            desc = getattr(star, "desc", None) or ""
            display_name = getattr(star, "display_name", None) or name
            root_dir_name = getattr(star, "root_dir_name", None)

            plugins[name] = PluginInfo(
                name=name,
                display_name=display_name,
                description=desc,
                icon_url=self._get_plugin_icon_uri(root_dir_name),
            )
            star_modules[module_path] = name

        # 遍历 handler 注册表，收集命令
        # 第一遍：收集命令组中子 handler 的 id 和每个插件的嵌套子组名
        grouped_handler_ids: set[int] = set()
        nested_groups_by_module: dict[str, set[str]] = {}  # module_path -> {子组名}
        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            for f in handler.event_filters:
                if isinstance(f, CommandGroupFilter):
                    self._collect_group_handler_ids(f, grouped_handler_ids)
                    names = nested_groups_by_module.setdefault(handler.handler_module_path, set())
                    self._collect_nested_group_names(f, names)

        # 第二遍：提取命令，跳过已被命令组收录的子 handler 和嵌套子组的独立 handler
        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            if id(handler) in grouped_handler_ids:
                continue
            # 跳过作为同插件内其他组嵌套子组的独立 handler
            nested_names = nested_groups_by_module.get(handler.handler_module_path, set())
            is_nested = False
            for f in handler.event_filters:
                if isinstance(f, CommandGroupFilter) and f.group_name in nested_names:
                    is_nested = True
                    break
            if is_nested:
                continue
            plugin_name = star_modules.get(handler.handler_module_path)
            if not plugin_name or plugin_name not in plugins:
                continue

            self._extract_commands(handler, plugins[plugin_name])

        # 应用配置覆盖
        self._apply_overrides(plugins)

        # 添加自定义分类
        self._apply_custom_categories(plugins)

        # 为没有图标的插件分配默认图标
        default_uri = _get_default_icon_uri()
        for p in plugins.values():
            if not p.icon_url:
                p.icon_url = default_uri

        # 排序
        return sorted(plugins.values(), key=lambda p: (p.order, p.name))

    def _extract_commands(self, handler: StarHandlerMetadata, plugin: PluginInfo):
        """从 handler 的 event_filters 中提取命令信息"""
        cmd_filter: CommandFilter | None = None
        group_filter: CommandGroupFilter | None = None
        is_admin = False

        for f in handler.event_filters:
            if isinstance(f, CommandFilter):
                cmd_filter = f
            elif isinstance(f, CommandGroupFilter):
                group_filter = f
            elif isinstance(f, PermissionTypeFilter) and f.permission_type == PermissionType.ADMIN:
                is_admin = True

        if cmd_filter:
            existing_names = {c.name for c in plugin.commands}
            if cmd_filter.command_name not in existing_names:
                plugin.commands.append(
                    CommandInfo(
                        name=cmd_filter.command_name,
                        description=handler.desc or "",
                        aliases=list(cmd_filter.alias) if cmd_filter.alias else [],
                        admin_only=is_admin,
                    )
                )
        elif group_filter:
            self._extract_group_commands(group_filter, plugin, is_admin, prefix="")

    @staticmethod
    def _collect_group_handler_ids(group: CommandGroupFilter, ids: set[int]):
        """递归收集命令组中所有子 handler 的 id"""
        for sub in group.sub_command_filters:
            if isinstance(sub, CommandFilter) and sub.handler_md:
                ids.add(id(sub.handler_md))
            elif isinstance(sub, CommandGroupFilter):
                CustomHelpPlugin._collect_group_handler_ids(sub, ids)

    @staticmethod
    def _collect_nested_group_names(group: CommandGroupFilter, names: set[str]):
        """递归收集作为其他组嵌套子组的组名"""
        for sub in group.sub_command_filters:
            if isinstance(sub, CommandGroupFilter):
                names.add(sub.group_name)
                CustomHelpPlugin._collect_nested_group_names(sub, names)

    def _extract_group_commands(
        self,
        group: CommandGroupFilter,
        plugin: PluginInfo,
        parent_admin: bool,
        prefix: str,
    ):
        """递归提取命令组中的子命令"""
        group_prefix = f"{prefix}{group.group_name} " if prefix else f"{group.group_name} "
        existing_names = {c.name for c in plugin.commands}

        for sub in group.sub_command_filters:
            if isinstance(sub, CommandFilter):
                full_name = f"{group_prefix}{sub.command_name}"
                if full_name not in existing_names:
                    sub_desc = ""
                    sub_admin = parent_admin
                    if sub.handler_md:
                        sub_desc = sub.handler_md.desc or ""
                        for f in sub.handler_md.event_filters:
                            if isinstance(f, PermissionTypeFilter) and f.permission_type == PermissionType.ADMIN:
                                sub_admin = True
                    plugin.commands.append(
                        CommandInfo(
                            name=full_name,
                            description=sub_desc,
                            aliases=list(sub.alias) if sub.alias else [],
                            admin_only=sub_admin,
                        )
                    )
                    existing_names.add(full_name)
            elif isinstance(sub, CommandGroupFilter):
                self._extract_group_commands(sub, plugin, parent_admin, group_prefix)

    def _apply_overrides(self, plugins: dict[str, PluginInfo]):
        """应用配置中的插件覆盖"""
        overrides = getattr(self.config, "plugin_overrides", []) or []
        if not isinstance(overrides, list):
            return

        for override in overrides:
            if not isinstance(override, dict):
                continue
            plugin_name = override.get("plugin_name", "")
            if not plugin_name:
                continue
            if plugin_name not in plugins:
                plugins[plugin_name] = PluginInfo(name=plugin_name)

            p = plugins[plugin_name]
            if override.get("display_name"):
                p.display_name = override["display_name"]
            if override.get("description"):
                p.description = override["description"]
            if "order" in override:
                p.order = override["order"]
            for raw_cmd in override.get("extra_commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
                    # 覆盖同名已有命令
                    p.commands = [c for c in p.commands if c.name != cmd.name]
                    p.commands.append(cmd)

    def _apply_custom_categories(self, plugins: dict[str, PluginInfo]):
        """应用自定义分类"""
        categories = getattr(self.config, "custom_categories", []) or []
        if not isinstance(categories, list):
            return

        for cat in categories:
            if not isinstance(cat, dict) or not cat.get("name"):
                continue
            cat_name = f"custom_{cat['name']}"
            p = PluginInfo(
                name=cat_name,
                display_name=cat["name"],
                description=cat.get("description", ""),
                order=cat.get("order", 99),
            )
            for raw_cmd in cat.get("commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
                    p.commands.append(cmd)
            if p.commands:
                plugins[cat_name] = p

    @staticmethod
    def _parse_pipe_command(raw: str) -> CommandInfo | None:
        """解析 '命令名|描述|前缀' 格式的字符串为 CommandInfo

        前缀为可选第三段：省略则默认无前缀，填写则使用自定义前缀。
        """
        if not isinstance(raw, str) or not raw.strip():
            return None
        parts = raw.split("|")
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        custom_prefix = parts[2].strip() if len(parts) > 2 else ""
        if not name:
            return None
        return CommandInfo(name=name, description=desc, custom_prefix=custom_prefix)

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
        return _read_image_as_data_uri(banner_path)

    def _get_header_logo_uri(self) -> str:
        """获取顶部 Logo 的 data URI，优先使用配置，否则使用插件自带 logo.svg"""
        logo_path_str = getattr(self.config, "header_logo", "") or ""
        if logo_path_str:
            logo_path = self._resolve_data_path(logo_path_str)
            if logo_path:
                uri = _read_image_as_data_uri(logo_path)
                if uri:
                    return uri
        return _read_image_as_data_uri(DEFAULT_LOGO_PATH)

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
        template = _read_template(template_name, custom_dir)
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
        template = _read_template("sub_menu.html", custom_dir)
        prefix = self._get_wake_prefix()
        data = self._build_sub_menu_data(target, prefix)

        try:
            img_bytes = await self._get_cached_or_render("sub_menu.html", template, data)
        except Exception as e:
            logger.error(f"渲染子菜单失败: {e}")
            return event.plain_result("渲染帮助菜单失败，请稍后重试。")
        return event.chain_result([Image.fromBytes(img_bytes)])
