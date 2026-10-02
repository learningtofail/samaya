"""services/images.py: normalisation profiles and the routes that use them (spec §71.4)."""
import base64
import io

import pytest
from PIL import Image

from services.images import (
    EVENT_COVER, TENANT_ICON, THEME_BANNER, THEME_HEADER, THEME_HEADER_MOBILE, ImageProfile, ImageRejected, process_data_uri,
)


def _uri(image: Image.Image, fmt: str, **save) -> str:
    buf = io.BytesIO()
    image.save(buf, fmt, **save)
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}[fmt]
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


def _open(uri: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


class TestProfiles:
    def test_cover_is_bounded_and_jpeg(self):
        out = process_data_uri(_uri(Image.new("RGB", (4000, 1000), "red"), "PNG"), EVENT_COVER)
        img = _open(out)
        assert out.startswith("data:image/jpeg;base64,")
        assert img.format == "JPEG" and img.width <= 1600 and img.height <= 800

    def test_cover_never_upscales(self):
        img = _open(process_data_uri(_uri(Image.new("RGB", (200, 100), "blue"), "PNG"), EVENT_COVER))
        assert img.size == (200, 100)

    def test_cover_flattens_alpha_onto_white(self):
        src = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        img = _open(process_data_uri(_uri(src, "PNG"), EVENT_COVER)).convert("RGB")
        assert img.getpixel((5, 5)) == (255, 255, 255)

    def test_icon_is_cropped_square_and_bounded_png_with_alpha(self):
        src = Image.new("RGBA", (600, 300), (255, 0, 0, 128))
        img = _open(process_data_uri(_uri(src, "PNG"), TENANT_ICON))
        assert img.format == "PNG"
        assert img.width == img.height <= 256
        assert img.mode == "RGBA"

    def test_banner_is_cropped_to_its_exact_size(self):
        img = _open(process_data_uri(_uri(Image.new("RGB", (3200, 800), "green"), "PNG"), THEME_BANNER))
        assert img.format == "WEBP" and img.size == (1200, 360)

    @pytest.mark.parametrize("profile,size", [(THEME_HEADER, (1920, 400)), (THEME_HEADER_MOBILE, (900, 500))])
    def test_header_art_exact_size_and_weight_budget(self, profile, size):
        sky = Image.linear_gradient("L").resize((2400, 1200)).convert("RGB")
        textured = Image.blend(sky, Image.effect_noise((2400, 1200), 80).convert("RGB"), 0.15)
        out = process_data_uri(_uri(textured, "JPEG"), profile)
        assert _open(out).size == size
        assert len(base64.b64decode(out.split(",", 1)[1])) <= profile.max_bytes

    @pytest.mark.parametrize("profile", [THEME_BANNER, THEME_HEADER, THEME_HEADER_MOBILE])
    def test_art_smaller_than_the_target_is_refused_not_upscaled(self, profile):
        with pytest.raises(ImageRejected, match="too small"):
            process_data_uri(_uri(Image.new("RGB", (profile.crop[0] - 1, profile.crop[1]), "red"), "PNG"), profile)

    def test_art_that_cannot_fit_its_budget_is_refused(self):
        noise = Image.effect_noise((1920, 400), 128).convert("RGB")
        tight = ImageProfile("tight", 1920, 400, "WEBP", crop=(1920, 400), max_bytes=2000)
        with pytest.raises(ImageRejected, match="too detailed"):
            process_data_uri(_uri(noise, "PNG"), tight)

    def test_exif_orientation_applied_and_metadata_dropped(self):
        src = Image.new("RGB", (40, 20), "white")
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees
        exif[0x010F] = "SecretCamera"
        img = _open(process_data_uri(_uri(src, "JPEG", exif=exif), EVENT_COVER))
        assert img.size == (20, 40)
        assert not img.getexif()

    def test_animated_gif_becomes_first_frame(self):
        frames = [Image.new("RGB", (20, 20), c) for c in ("red", "blue")]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
        uri = "data:image/gif;base64," + base64.b64encode(buf.getvalue()).decode()
        img = _open(process_data_uri(uri, EVENT_COVER))
        assert getattr(img, "n_frames", 1) == 1

    def test_already_normal_image_is_kept_byte_for_byte(self):
        once = process_data_uri(_uri(Image.new("RGB", (500, 300), "red"), "PNG"), EVENT_COVER)
        assert process_data_uri(once, EVENT_COVER) == once


class TestRejections:
    def test_svg_refused(self):
        uri = "data:image/svg+xml;base64," + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
        with pytest.raises(ImageRejected, match="SVG"):
            process_data_uri(uri, EVENT_COVER)

    def test_not_an_image_refused(self):
        uri = "data:image/png;base64," + base64.b64encode(b"hello world").decode()
        with pytest.raises(ImageRejected, match="could not be read"):
            process_data_uri(uri, EVENT_COVER)

    def test_bad_base64_refused(self):
        with pytest.raises(ImageRejected):
            process_data_uri("data:image/png;base64,@@@@", EVENT_COVER)

    def test_oversized_refused(self):
        with pytest.raises(ImageRejected, match="8 MB"):
            process_data_uri("data:image/png;base64," + "A" * (8 * 1024 * 1024), EVENT_COVER)

    def test_pixel_bomb_refused(self):
        buf = io.BytesIO()
        Image.new("1", (9000, 9000)).save(buf, "PNG")  # 81 MP, a few KB encoded
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        with pytest.raises(ImageRejected, match="pixels"):
            process_data_uri(uri, EVENT_COVER)


class TestRoutes:
    @staticmethod
    async def _server(client, tenant):
        return (await client.post("/admin/api/discord-servers", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Img Server", "guild_id": "111222333",
        })).json()

    async def test_icon_upload_is_normalised(self, client, tenant):
        server = await self._server(client, tenant)
        big = _uri(Image.new("RGB", (800, 400), "red"), "PNG")
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Img", "slug": "img",
            "server_id": server["id"], "icon_image_data": big,
        })
        assert r.status_code == 201, r.text
        icon = _open(r.json()["icon_image_data"])
        assert icon.width == icon.height <= 256

    async def test_unreadable_icon_returns_422_naming_the_field(self, client, tenant):
        server = await self._server(client, tenant)
        bad = "data:image/png;base64," + base64.b64encode(b"nope").decode()
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Bad", "slug": "bad",
            "server_id": server["id"], "icon_image_data": bad,
        })
        assert r.status_code == 422
        assert "Icon" in r.json()["detail"]
