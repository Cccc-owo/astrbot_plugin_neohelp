<p align="center">
  <img src="logo.png" width="120" height="120" alt="NeoHelp Logo">
</p>

<h1 align="center">NeoHelp</h1>

<p align="center">
  美观的自定义帮助菜单插件，适用于 <a href="https://github.com/AstrBotDevs/AstrBot">AstrBot</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AstrBot-v4.5.0+-blue" alt="AstrBot">
  <img src="https://img.shields.io/badge/license-GPL--3.0-green" alt="License">
</p>

---

## 功能

- 自动扫描所有已安装插件及其注册命令，生成两级帮助菜单（主菜单概览 + 子菜单详情）
- 使用 Playwright 将 HTML 模板渲染为高清图片，暗色主题，3 列卡片布局
- 自动读取各插件 `logo.png` 作为图标，无图标时使用默认图标
- 支持自定义 Banner 背景图、强调色、标题/副标题/底部文字
- 支持通过 Google Fonts 等 CSS URL 自定义正文和等宽字体
- 支持插件黑名单、显示覆盖（自定义显示名/描述/排序/额外命令）
- 支持添加自定义分类，用于展示非 AstrBot 插件的外部服务命令

## 使用

发送以下命令：

| 命令 | 说明 |
|------|------|
| `/help` | 查看主菜单（所有插件概览） |
| `/help <插件名>` | 查看某个插件的详细命令列表 |

别名：`/帮助`、`/菜单`、`/功能`

## 安装

~~在 AstrBot Dashboard 的插件市场中搜索 `NeoHelp` 安装~~（暂未上架）

通过 GitHub 仓库地址安装：

```
https://github.com/Cccc-owo/astrbot-plugin-neohelp
```

### 依赖

本插件需要 Playwright 浏览器引擎。如果渲染失败，检查是否未安装引擎。如提示没有安装好 Playwright ，可能需要执行：

```bash
playwright install chromium
```

## 配置

所有配置均可在 AstrBot Dashboard 中修改，无需手动编辑文件。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `title` | 帮助菜单标题 | `帮助菜单` |
| `subtitle` | 帮助菜单副标题 | `发送 /help <插件名> 查看详细命令` |
| `accent_color` | 强调色（十六进制） | `#d4b163` |
| `show_builtin_cmds` | 是否显示 AstrBot 内置命令 | `false` |
| `plugin_blacklist` | 不显示的插件列表 | `[]` |
| `plugin_overrides` | 插件显示覆盖 | `[]` |
| `custom_categories` | 自定义分类 | `[]` |
| `font_url` | 自定义字体 CSS URL | 空 |
| `font_family` | 自定义正文字体名称 | 空 |
| `mono_font_family` | 自定义等宽字体名称 | 空 |
| `banner_image` | Banner 背景图文件名（相对于插件数据目录） | 空 |
| `footer_text` | 底部自定义文字 | 空（显示版本信息） |

## 许可证

[GPL-3.0](LICENSE)
