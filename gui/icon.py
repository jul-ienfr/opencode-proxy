from PIL import Image, ImageDraw

_SIZE = 64
_MARGIN = 6


def _circle(color_rgb):
    img = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        [_MARGIN, _MARGIN, _SIZE - _MARGIN, _SIZE - _MARGIN],
        fill=color_rgb + (255,),
    )
    return img


def running_icon():
    return _circle((46, 204, 113))


def stopped_icon():
    return _circle((231, 76, 60))
