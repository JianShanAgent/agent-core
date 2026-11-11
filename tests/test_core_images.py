import threading
import time
from io import BytesIO

import pytest
from PIL import Image

import jianshanagent.core.images as images_module
from jianshanagent.core.clock import now_text
from jianshanagent.core.images import ImageError, jpeg_data_uri, normalize_image


def test_normalize_flattens_transparency_onto_white_and_shrinks() -> None:
    source = Image.new("RGBA", (400, 200), (0, 0, 0, 0))  # 全透明
    source.paste((200, 30, 30, 255), (0, 0, 100, 200))  # 左边四分之一是不透明的红色
    raw = BytesIO()
    source.save(raw, format="PNG")

    out = Image.open(BytesIO(normalize_image(raw.getvalue(), max_side=100)))
    assert (out.format, out.mode, out.size) == ("JPEG", "RGB", (100, 50))
    red, white = out.getpixel((5, 25)), out.getpixel((90, 25))
    assert red[0] > 150 and red[1] < 80  # 原来有颜色的地方还在
    assert min(white) > 240  # 透明的地方成了白底

    small = normalize_image(raw.getvalue())  # 本来就不大的不放大
    assert Image.open(BytesIO(small)).size == (400, 200)
    assert jpeg_data_uri(small).startswith("data:image/jpeg;base64,/9j/")


def test_normalize_rejects_non_images_and_oversized_ones() -> None:
    with pytest.raises(ImageError, match="不是能识别的图片"):
        normalize_image(b"not an image")
    raw = BytesIO()
    Image.new("RGB", (300, 200)).save(raw, format="PNG")
    with pytest.raises(ImageError, match="图片太大（300×200）"):  # 按像素数拦，不看文件大小
        normalize_image(raw.getvalue(), max_pixels=50_000)


def test_big_phone_photo_is_shrunk_while_decoding_and_still_turned_upright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 手机竖着拍的大照片：文件里是横的 1600×800，EXIF 说要转 90 度。左半边红、右半边蓝
    source = Image.new("RGB", (1600, 800), (220, 30, 30))
    source.paste((30, 30, 220), (800, 0, 1600, 800))
    exif = Image.Exif()
    exif[0x0112] = 6
    raw = BytesIO()
    source.save(raw, format="JPEG", quality=95, exif=exif)

    decoded: list[tuple[int, int]] = []
    transpose = images_module.ImageOps.exif_transpose

    def spy(image: Image.Image, **kwargs: object) -> object:
        decoded.append(image.size)  # 解码出来的尺寸（转向之前）
        return transpose(image, **kwargs)

    monkeypatch.setattr(images_module.ImageOps, "exif_transpose", spy)
    out = Image.open(BytesIO(normalize_image(raw.getvalue(), max_side=100)))

    # 解码时就缩成了 1/4，没有把 1600×800 整张摊开（不缩到 1/8：两条边都要留够两倍 max_side）
    assert decoded == [(400, 200)]
    assert out.size == (50, 100)  # 摆正了：竖的
    top, bottom = out.getpixel((25, 10)), out.getpixel((25, 90))
    assert top[0] > 150 > top[2] and bottom[2] > 150 > bottom[0]  # 原来的左边转到了上面


def test_opaque_and_transparent_palette_images_both_end_up_on_white() -> None:
    # 调色板图的透明色不在模式里，记在 info 里：有透明色的要铺白底，没有的直接转
    source = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    source.paste((20, 160, 60, 255), (0, 0, 20, 40))
    with_hole, opaque = BytesIO(), BytesIO()
    source.quantize(8).save(with_hole, format="PNG")
    source.convert("RGB").quantize(8).save(opaque, format="PNG")
    assert "transparency" in Image.open(BytesIO(with_hole.getvalue())).info
    assert "transparency" not in Image.open(BytesIO(opaque.getvalue())).info

    holed = Image.open(BytesIO(normalize_image(with_hole.getvalue())))
    solid = Image.open(BytesIO(normalize_image(opaque.getvalue())))
    assert min(holed.getpixel((30, 20))) > 240  # 透明的那一半成了白的
    assert max(solid.getpixel((30, 20))) < 20  # 不透明的黑还是黑，没有被当成透明铺白
    assert holed.getpixel((5, 20))[1] > 120 and solid.getpixel((5, 20))[1] > 120


def test_at_most_two_images_are_decoded_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = BytesIO()
    Image.new("RGB", (50, 50), (9, 9, 9)).save(raw, format="PNG")
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()
    on_white = images_module._on_white

    def slow(image: Image.Image) -> Image.Image:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return on_white(image)

    monkeypatch.setattr(images_module, "_on_white", slow)
    workers = [threading.Thread(target=normalize_image, args=(raw.getvalue(),)) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert state["peak"] == 2  # 六张同时来，任何时刻最多两张在解


def test_now_text_has_date_weekday_and_time() -> None:
    text = now_text()
    date, weekday, clock = text.split(" ")
    assert len(date) == 10 and weekday.startswith("星期") and len(clock) == 5
