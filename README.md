<p align="center">
  <img src="resources/logo.svg" width="120" height="120" alt="NeoHelp Logo">
</p>

<h1 align="center">NeoHelp</h1>

<p align="center">
  可选地展示已安装插件命令，渲染为精美的图片帮助菜单。支持高度自定义。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AstrBot-v4.5.0+-blue" alt="AstrBot">
  <img src="https://img.shields.io/badge/license-GPL--3.0-green" alt="License">
</p>

---

## 功能

- 自动扫描所有已安装插件及其注册命令，生成帮助菜单
- 两种主菜单模式：插件卡片概览 或 展开所有命令列表
- 使用 Playwright 将 HTML 模板渲染为高清图片，暗色主题，3 列卡片布局
- 自动读取各插件 `logo.png` 作为图标，无图标时使用默认图标
- 自动适配 AstrBot 自定义唤醒前缀
- 管理员可查看完整帮助（含显示黑名单中的插件）
- 支持自定义 Banner 背景图、顶部 Logo、强调色、标题/副标题/底部文字
- 支持通过 Google Fonts 等 CSS URL 自定义正文和等宽字体
- 支持插件显示黑名单、显示覆盖（自定义显示名/描述/排序/额外命令）
- 支持添加自定义分类，用于展示非 AstrBot 插件的外部服务命令
- 支持自定义 HTML 模板，完全控制渲染样式

## 示例

见 [example.md](example.md)

## 使用

发送以下命令（前缀跟随 AstrBot 唤醒前缀设置，以 `/` 为例）：

| 命令 | 说明 |
|------|------|
| `/help` | 查看主菜单 |
| `/help <插件名>` | 查看某个插件的详细命令列表 |
| `/help --admin` | 管理员查看完整帮助（含黑名单插件） |

别名：`/帮助`、`/菜单`、`/功能`

## 安装

在 AstrBot Dashboard 的插件市场中搜索 `NeoHelp` 安装

通过 GitHub 仓库地址安装：

```
https://github.com/Cccc-owo/astrbot-plugin-neohelp
```

### 依赖

本插件需要 Playwright 浏览器引擎。如果渲染失败，检查是否未安装引擎。如提示没有安装好 Playwright ，可能需要执行：

```bash
playwright install chromium
```

### 禁用内置 help 命令

AstrBot 自带 `help` 命令，与本插件冲突。安装后请前往 **管理面板 → 插件 → 管理行为** 中禁用内置的 help 命令，否则两者会同时响应。

## 配置

所有配置均可在 AstrBot Dashboard 中修改，无需手动编辑文件。完整配置项及说明见 [`_conf_schema.json`](_conf_schema.json)。

### 自定义模板

开启 `custom_templates` 后，插件会优先从数据目录的 `custom_templates` 子目录加载同名 HTML 模板文件：

```
data/plugin_data/astrbot_plugin_neohelp/custom_templates/
├── main_menu.html       # 主菜单（卡片模式）
├── expanded_menu.html   # 主菜单（展开模式）
└── sub_menu.html        # 子菜单（插件详情）
```

只需放入需要自定义的模板，未提供的文件会自动回退到插件默认模板。

## 贡献

欢迎提交 💡Issue 和 🔧Pull Request。

当然，如果能点亮那颗⭐小星星就更好啦！

## 许可证

[MIT](LICENSE)
