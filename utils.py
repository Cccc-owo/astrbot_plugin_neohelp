import base64
import mimetypes
from pathlib import Path

from astrbot.api import logger


PLUGIN_NAME = "astrbot_plugin_neohelp"
PLUGIN_DIR = Path(__file__).parent
TEMPLATES_DIR = PLUGIN_DIR / "templates"
RESOURCES_DIR = PLUGIN_DIR / "resources"
DEFAULT_ICON_PATH = RESOURCES_DIR / "default_icon.webp"
DEFAULT_LOGO_PATH = RESOURCES_DIR / "logo.svg"


def read_template(name: str, data_dir: Path | None = None) -> str:
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


def read_image_as_data_uri(path: Path) -> str:
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


def get_default_icon_uri() -> str:
    global _default_icon_uri
    if _default_icon_uri is None:
        _default_icon_uri = read_image_as_data_uri(DEFAULT_ICON_PATH)
    return _default_icon_uri
