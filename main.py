import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
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

# 内置命令定义
BUILTIN_COMMANDS = [
    {"name": "t2i", "desc": "开关文本转图片"},
    {"name": "tts", "desc": "开关文本转语音"},
    {"name": "sid", "desc": "获取会话 ID"},
    {"name": "op", "desc": "管理员操作", "admin": True},
    {"name": "wl", "desc": "白名单管理", "admin": True},
    {"name": "provider", "desc": "大模型提供商"},
    {"name": "model", "desc": "模型列表"},
    {"name": "ls", "desc": "对话列表"},
    {"name": "new", "desc": "创建新对话"},
    {"name": "switch", "desc": "切换对话", "usage": "/switch <序号>"},
    {"name": "del", "desc": "删除当前会话对话", "admin": True},
    {"name": "reset", "desc": "重置 LLM 会话", "admin": True},
    {"name": "history", "desc": "当前对话的对话记录"},
    {"name": "persona", "desc": "人格情景管理", "admin": True},
    {"name": "tool", "desc": "函数工具管理"},
    {"name": "key", "desc": "API Key 管理", "admin": True},
    {"name": "websearch", "desc": "网页搜索"},
]


@dataclass
class CommandInfo:
    name: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    admin_only: bool = False


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


def _read_template(name: str) -> str:
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


@register(
    "astrbot_plugin_neohelp",
    "Cccc_",
    "美观的自定义帮助菜单插件",
    "1.0.0",
)
class CustomHelpPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ctx: FullContext = context  # type: ignore[assignment]
        self._plugins_base_dir = PLUGIN_DIR.parent
        self._data_dir = StarTools.get_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)

    async def terminate(self):
        """插件卸载时关闭浏览器"""
        await renderer.cleanup()

    @filter.command("help", alias={"帮助", "菜单", "功能"})
    async def help_command(self, event: AstrMessageEvent, query: str = ""):
        """查看帮助菜单"""
        query = query.strip()

        if query:
            yield await self._render_sub_menu(event, query)
        else:
            yield await self._render_main_menu(event)

    # ==================== 数据收集 ====================

    def _get_plugin_icon_uri(self, root_dir_name: str | None) -> str:
        """获取插件图标的 data URI，找不到则返回默认图标"""
        if root_dir_name:
            logo_path = self._plugins_base_dir / root_dir_name / "logo.png"
            uri = _read_image_as_data_uri(logo_path)
            if uri:
                return uri
        return _get_default_icon_uri()

    def _collect_plugins(self) -> list[PluginInfo]:
        """从已安装插件中自动收集命令信息"""
        plugins: dict[str, PluginInfo] = {}
        blacklist = set(getattr(self.config, "plugin_blacklist", []) or [])
        blacklist.add(PLUGIN_NAME)
        blacklist.add("astrbot")

        try:
            all_stars = self._ctx.get_all_stars()
            all_stars = [s for s in all_stars if s.activated]
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return []

        # 收集插件基本信息
        star_modules: dict[str, str] = {}  # module_path -> plugin_name
        for star in all_stars:
            name = getattr(star, "name", None)
            if not name or name in blacklist:
                continue
            module_path = getattr(star, "module_path", None)
            if not module_path:
                continue

            star_cls = getattr(star, "star_cls", None)
            if star_cls is self:
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
        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            plugin_name = star_modules.get(handler.handler_module_path)
            if not plugin_name or plugin_name not in plugins:
                continue

            self._extract_commands(handler, plugins[plugin_name])

        # 应用配置覆盖
        self._apply_overrides(plugins)

        # 添加内置命令
        if getattr(self.config, "show_builtin_cmds", False):
            builtin = PluginInfo(
                name="builtin",
                display_name="内置命令",
                description="AstrBot 内置系统命令",
                icon_url=_get_default_icon_uri(),
                order=0,
            )
            for cmd_def in BUILTIN_COMMANDS:
                builtin.commands.append(
                    CommandInfo(
                        name=cmd_def["name"],
                        description=cmd_def.get("desc", ""),
                        usage=cmd_def.get("usage", ""),
                        admin_only=cmd_def.get("admin", False),
                    )
                )
            plugins["builtin"] = builtin

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
            self._extract_group_commands(group_filter, handler, plugin, is_admin, prefix="")

    def _extract_group_commands(
        self,
        group: CommandGroupFilter,
        handler: StarHandlerMetadata,
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
            elif isinstance(sub, CommandGroupFilter):
                self._extract_group_commands(sub, handler, plugin, parent_admin, group_prefix)

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
            if override.get("order", 99) != 99:
                p.order = override["order"]
            for raw_cmd in override.get("extra_commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
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
                order=cat.get("order", 50),
            )
            for raw_cmd in cat.get("commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
                    p.commands.append(cmd)
            if p.commands:
                plugins[cat_name] = p

    @staticmethod
    def _parse_pipe_command(raw: str) -> CommandInfo | None:
        """解析 '命令名|描述' 格式的字符串为 CommandInfo"""
        if not isinstance(raw, str) or not raw.strip():
            return None
        parts = raw.split("|", 1)
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            return None
        return CommandInfo(name=name, description=desc)

    # ==================== 渲染 ====================

    def _get_footer(self) -> str:
        custom = getattr(self.config, "footer_text", "") or ""
        if custom:
            return custom
        version = getattr(self.config, "version", "")
        return f"AstrBot{' v' + version if version else ''}"

    def _get_accent_color(self) -> str:
        color = getattr(self.config, "accent_color", "#d4b163") or "#d4b163"
        if not color.startswith("#") or len(color) not in (4, 7):
            return "#d4b163"
        return color

    def _get_banner_data_uri(self) -> str:
        """读取 Banner 背景图，路径相对于插件数据目录"""
        banner_path_str = getattr(self.config, "banner_image", "") or ""
        if not banner_path_str:
            return ""
        banner_path = Path(banner_path_str)
        if not banner_path.is_absolute():
            banner_path = self._data_dir / banner_path
        return _read_image_as_data_uri(banner_path)

    def _get_font_config(self) -> dict:
        """获取自定义字体配置"""
        font_url = (getattr(self.config, "font_url", "") or "").strip()
        font_family = (getattr(self.config, "font_family", "") or "").strip()
        mono_font_family = (getattr(self.config, "mono_font_family", "") or "").strip()
        return {"font_url": font_url, "font_family": font_family, "mono_font_family": mono_font_family}

    async def _render_main_menu(self, event: AstrMessageEvent):
        """渲染主菜单"""
        plugins = self._collect_plugins()
        plugins = [p for p in plugins if p.commands]

        if not plugins:
            return event.plain_result("没有找到任何可用的插件命令。")

        template = _read_template("main_menu.html")
        accent = self._get_accent_color()
        title = getattr(self.config, "title", "帮助菜单") or "帮助菜单"
        subtitle = (
            getattr(self.config, "subtitle", "发送 /help <插件名> 查看详细命令") or "发送 /help <插件名> 查看详细命令"
        )

        data = {
            "title": title,
            "subtitle": subtitle,
            "accent_color": accent,
            "banner_image": self._get_banner_data_uri(),
            **self._get_font_config(),
            "plugins": [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "description": p.description,
                    "icon_url": p.icon_url,
                    "cmd_count": len(p.commands),
                }
                for p in plugins
            ],
            "footer": self._get_footer(),
        }

        try:
            img_bytes = await renderer.render_template(template, data)
        except Exception as e:
            logger.error(f"渲染主菜单失败: {e}")
            return event.plain_result("渲染帮助菜单失败，请稍后重试。")
        return event.chain_result([Image.fromBytes(img_bytes)])

    async def _render_sub_menu(self, event: AstrMessageEvent, query: str):
        """渲染子菜单（某个插件的详细命令）"""
        plugins = self._collect_plugins()

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
            return event.plain_result(f"未找到插件「{query}」，请发送 /help 查看所有可用插件。")

        template = _read_template("sub_menu.html")
        accent = self._get_accent_color()

        data = {
            "plugin": {
                "name": target.name,
                "display_name": target.display_name,
                "description": target.description,
                "icon_url": target.icon_url,
            },
            "commands": [
                {
                    "name": c.name,
                    "description": c.description,
                    "aliases": c.aliases,
                    "usage": c.usage,
                    "admin_only": c.admin_only,
                }
                for c in target.commands
            ],
            "accent_color": accent,
            **self._get_font_config(),
            "footer": self._get_footer(),
        }

        try:
            img_bytes = await renderer.render_template(template, data)
        except Exception as e:
            logger.error(f"渲染子菜单失败: {e}")
            return event.plain_result("渲染帮助菜单失败，请稍后重试。")
        return event.chain_result([Image.fromBytes(img_bytes)])
