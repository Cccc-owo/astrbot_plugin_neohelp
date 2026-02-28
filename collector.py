from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import StarHandlerMetadata, star_handlers_registry

from .models import CommandInfo, PluginInfo
from .utils import PLUGIN_NAME, get_default_icon_uri, read_image_as_data_uri

if TYPE_CHECKING:
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star.context import Context as FullContext


class PluginCollector:
    """从已安装插件中自动收集命令信息"""

    def __init__(self, ctx: FullContext, config: AstrBotConfig, plugins_base_dir: Path):
        self._ctx = ctx
        self._config = config
        self._plugins_base_dir = plugins_base_dir

    # ==================== 公开接口 ====================

    def collect(self, skip_blacklist: bool = False) -> list[PluginInfo]:
        """编排器：发现插件 → 填充命令 → 应用覆盖 → 自定义分类 → 默认图标 → 排序"""
        plugins, star_modules = self._discover_plugins(skip_blacklist)
        self._populate_commands(plugins, star_modules)
        self._apply_overrides(plugins)
        self._apply_custom_categories(plugins)
        self._assign_default_icons(plugins)
        return sorted(plugins.values(), key=lambda p: (p.order, p.name))

    # ==================== 插件发现 ====================

    def _discover_plugins(self, skip_blacklist: bool) -> tuple[dict[str, PluginInfo], dict[str, str]]:
        """扫描已激活 Star，创建 PluginInfo 和模块路径映射"""
        _debug = getattr(self._config, "debug", False)
        blacklist: set[str] = set()
        if not skip_blacklist:
            blacklist = set(getattr(self._config, "plugin_blacklist", []) or [])
        blacklist.add(PLUGIN_NAME)
        show_builtin = getattr(self._config, "show_builtin_cmds", False)

        if _debug:
            logger.info(
                f"[NeoHelp] _discover_plugins: skip_blacklist={skip_blacklist}, "
                f"blacklist={blacklist}, show_builtin={show_builtin}"
            )

        try:
            all_stars = self._ctx.get_all_stars()
            all_stars = [s for s in all_stars if s.activated]
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return {}, {}

        if _debug:
            star_names = [f"{getattr(s, 'name', '?')}(reserved={getattr(s, 'reserved', '?')})" for s in all_stars]
            logger.info(f"[NeoHelp] activated stars: {star_names}")

        plugins: dict[str, PluginInfo] = {}
        star_modules: dict[str, str] = {}  # module_path -> plugin_name

        for star in all_stars:
            name = getattr(star, "name", None)
            if not name or name in blacklist:
                continue
            module_path = getattr(star, "module_path", None)
            if not module_path:
                continue
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

        return plugins, star_modules

    # ==================== 命令提取 ====================

    def _populate_commands(self, plugins: dict[str, PluginInfo], star_modules: dict[str, str]):
        """两遍扫描 handler 注册表，将命令填入对应插件"""
        grouped_ids, nested_groups = self._scan_handler_groups()
        self._assign_handlers_to_plugins(plugins, star_modules, grouped_ids, nested_groups)

    def _scan_handler_groups(self) -> tuple[set[int], dict[str, set[str]]]:
        """第一遍：收集命令组子 handler ID 和嵌套子组名"""
        grouped_handler_ids: set[int] = set()
        nested_groups_by_module: dict[str, set[str]] = {}

        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            for f in handler.event_filters:
                if isinstance(f, CommandGroupFilter):
                    self._collect_group_handler_ids(f, grouped_handler_ids)
                    names = nested_groups_by_module.setdefault(handler.handler_module_path, set())
                    self._collect_nested_group_names(f, names)

        return grouped_handler_ids, nested_groups_by_module

    def _assign_handlers_to_plugins(
        self,
        plugins: dict[str, PluginInfo],
        star_modules: dict[str, str],
        grouped_ids: set[int],
        nested_groups: dict[str, set[str]],
    ):
        """第二遍：将未被组收录的 handler 命令分配到插件"""
        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            if id(handler) in grouped_ids:
                continue
            # 跳过作为同插件内其他组嵌套子组的独立 handler
            nested_names = nested_groups.get(handler.handler_module_path, set())
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

    @staticmethod
    def _collect_group_handler_ids(group: CommandGroupFilter, ids: set[int]):
        """递归收集命令组中所有子 handler 的 id"""
        for sub in group.sub_command_filters:
            if isinstance(sub, CommandFilter) and sub.handler_md:
                ids.add(id(sub.handler_md))
            elif isinstance(sub, CommandGroupFilter):
                PluginCollector._collect_group_handler_ids(sub, ids)

    @staticmethod
    def _collect_nested_group_names(group: CommandGroupFilter, names: set[str]):
        """递归收集作为其他组嵌套子组的组名"""
        for sub in group.sub_command_filters:
            if isinstance(sub, CommandGroupFilter):
                names.add(sub.group_name)
                PluginCollector._collect_nested_group_names(sub, names)

    # ==================== 配置覆盖 ====================

    def _apply_overrides(self, plugins: dict[str, PluginInfo]):
        """应用配置中的插件覆盖"""
        overrides = getattr(self._config, "plugin_overrides", []) or []
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
                with contextlib.suppress(TypeError, ValueError):
                    p.order = int(override["order"])
            for raw_cmd in override.get("extra_commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
                    # 覆盖同名已有命令
                    p.commands = [c for c in p.commands if c.name != cmd.name]
                    p.commands.append(cmd)

    def _apply_custom_categories(self, plugins: dict[str, PluginInfo]):
        """应用自定义分类"""
        categories = getattr(self._config, "custom_categories", []) or []
        if not isinstance(categories, list):
            return

        for cat in categories:
            if not isinstance(cat, dict) or not cat.get("name"):
                continue
            cat_name = f"custom_{cat['name']}"
            raw_order = cat.get("order", 99)
            try:
                order = int(raw_order)
            except (TypeError, ValueError):
                order = 99
            p = PluginInfo(
                name=cat_name,
                display_name=cat["name"],
                description=cat.get("description", ""),
                order=order,
            )
            for raw_cmd in cat.get("commands", []):
                cmd = self._parse_pipe_command(raw_cmd)
                if cmd:
                    p.commands.append(cmd)
            if p.commands:
                plugins[cat_name] = p

    @staticmethod
    def _parse_pipe_command(raw: str) -> CommandInfo | None:
        """解析 '命令名|描述|前缀' 格式的字符串为 CommandInfo"""
        if not isinstance(raw, str) or not raw.strip():
            return None
        parts = raw.split("|")
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        custom_prefix = parts[2].strip() if len(parts) > 2 else None
        if not name:
            return None
        return CommandInfo(name=name, description=desc, custom_prefix=custom_prefix)

    # ==================== 图标 ====================

    def _get_plugin_icon_uri(self, root_dir_name: str | None) -> str:
        """获取插件图标的 data URI，找不到则返回默认图标"""
        if root_dir_name:
            logo_path = self._plugins_base_dir / root_dir_name / "logo.png"
            uri = read_image_as_data_uri(logo_path)
            if uri:
                return uri
        return get_default_icon_uri()

    @staticmethod
    def _assign_default_icons(plugins: dict[str, PluginInfo]):
        """为缺少图标的插件分配默认图标"""
        default_uri = get_default_icon_uri()
        for p in plugins.values():
            if not p.icon_url:
                p.icon_url = default_uri
