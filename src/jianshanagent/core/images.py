"""图片的统一预处理。入库的 wiki 图片、用户发来的图片、给大模型看的图片，都先过这一道。

做四件事：动图只取第一帧；手机照片按拍摄方向摆正；透明的地方铺成白底；
长边缩到 ``max_side``，存成 JPEG。

为什么要铺底：透明 PNG 不铺底的话，同一张图在不同底色下算出来的向量差得很远
（wiki 的立绘大多是透明底的）。入库和查询必须用同一种处理，所以都调这里。
"""

from __future__ import annotations

import base64
import threading
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

DEFAULT_MAX_SIDE = 1024
#: 像素总数超过这个的图片不处理：解码按像素占内存（4000 万像素的 PNG 四五百兆），文件不大也可能很占
DEFAULT_MAX_PIXELS = 40_000_000
_WHITE = (255, 255, 255)
#: 自带透明度的几种图片模式（调色板图的透明色记在 info["transparency"] 里，另外判断）
_ALPHA_MODES = {"RGBA", "LA", "PA", "RGBa", "La"}
#: 同时最多解几张。接近像素上限的 PNG 一张要四五百兆（原图 + 带透明度的一份 + 铺底的一份），
#: 群里一下子来好几张大图时全部同时解，会把容器的内存上限（deploy/docker-compose.yml）顶穿。
#: 这个函数是在工作线程里调用的，多出来的在这里排队，不占事件循环
_DECODING = threading.BoundedSemaphore(2)


class ImageError(ValueError):
    """给的不是能识别的图片（格式不认识、文件损坏、尺寸大得离谱）。"""


def normalize_image(
    data: bytes,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    quality: int = 90,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> bytes:
    """把任意常见格式的图片整理成白底、长边不超过 ``max_side`` 的 JPEG。"""
    with _DECODING:
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size  # 这时还没解码，只读了文件头
                if width * height > max_pixels:
                    raise ImageError(f"图片太大（{width}×{height}）")
                image.seek(0)
                # JPEG 可以在解码时就按 1/2、1/4、1/8 缩小（别的格式这句不起作用）：手机拍的
                # 几千万像素的照片不用先整张摊开再缩。要的尺寸留了一倍余量，解出来仍然
                # 不小于两倍 max_side，最后那一步缩小照常做，出来的图和原来几乎没有差别
                image.draft(None, (max_side * 2, max_side * 2))
                ImageOps.exif_transpose(image, in_place=True)
                flat = _on_white(image)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            if isinstance(exc, ImageError):
                raise
            raise ImageError(f"不是能识别的图片：{exc}") from exc
        if max(flat.size) > max_side:
            flat.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = BytesIO()
        flat.save(out, format="JPEG", quality=quality)
        return out.getvalue()


def _on_white(image: Image.Image) -> Image.Image:
    """铺成白底的 RGB 图。没有透明度的图直接转，不多摊一份带透明度的（大图省一两百兆）。"""
    if image.mode not in _ALPHA_MODES and "transparency" not in image.info:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, _WHITE)
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


def jpeg_data_uri(jpeg: bytes) -> str:
    """JPEG 字节 → ``data:image/jpeg;base64,…``（大模型和嵌入接口都认这种写法）。"""
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
