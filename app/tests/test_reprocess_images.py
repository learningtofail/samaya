"""app/reprocess_images.py: the per-image step reports unreadable images and is idempotent."""
import base64
import io

from PIL import Image

import reprocess_images


def _png_uri(size) -> str:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_reprocess_value_reports_unreadable(capsys):
    from services.images import EVENT_COVER
    bad = "data:image/png;base64," + base64.b64encode(b"junk").decode()
    assert reprocess_images.reprocess_value(bad, EVENT_COVER) is None
    assert "unreadable" in capsys.readouterr().out


def test_reprocess_value_is_idempotent():
    from services.images import EVENT_COVER
    once = reprocess_images.reprocess_value(_png_uri((3000, 1500)), EVENT_COVER)
    assert reprocess_images.reprocess_value(once, EVENT_COVER) == once
