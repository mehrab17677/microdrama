import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import clear_config_cache
from app.vendors import images, local_images
from scripts import migrate, seed_config


def png(size, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def env(db_url):
    migrate.apply_migrations(db_url)
    seed_config.seed(db_url)
    clear_config_cache()


class FakePipe:
    """Stands in for a diffusers pipeline so the wiring is tested without the model."""

    def __init__(self, tag):
        self.tag, self.calls = tag, []

    def __call__(self, **kw):
        self.calls.append(kw)
        size = kw["image"].size if "image" in kw else (kw["width"], kw["height"])
        return SimpleNamespace(images=[Image.new("RGB", size, (200, 120, 80) if self.tag == "t2i" else (80, 120, 200))])


@pytest.fixture()
def fake(monkeypatch):
    pipes = {"t2i": FakePipe("t2i"), "i2i": FakePipe("i2i")}
    monkeypatch.setattr(local_images, "_pipes", pipes)
    monkeypatch.setitem(__import__("sys").modules, "torch", SimpleNamespace(Generator=lambda: SimpleNamespace(manual_seed=lambda s: s)))
    return pipes


def test_square_result_becomes_a_1080x1920_canvas_with_the_picture_in_the_middle():
    src = Image.new("RGB", (512, 512), (255, 0, 0))
    out = local_images.fit_to_9x16(src)
    assert out.size == (1080, 1920)
    assert out.getpixel((540, 960)) == (255, 0, 0)            # the sharp band is full width, centred
    assert out.getpixel((540, 960 - 540 + 2)) == (255, 0, 0) and out.getpixel((540, 960 + 540 - 3)) == (255, 0, 0)
    assert out.getpixel((540, 5)) != (0, 0, 0)                 # the blurred bands are filled, not black


def test_center_square_takes_the_middle_of_a_tall_image():
    tall = Image.new("RGB", (100, 300), (0, 0, 255))
    tall.paste((0, 255, 0), (0, 100, 100, 200))
    sq = local_images.center_square(tall)
    assert sq.size == (100, 100) and sq.getpixel((50, 50)) == (0, 255, 0)


def test_references_load_from_bytes_data_urls_and_files(tmp_path):
    data = png((40, 40), (9, 9, 9))
    p = tmp_path / "r.png"
    p.write_bytes(data)
    import base64

    for ref in (data, "data:image/png;base64," + base64.b64encode(data).decode(), p.as_uri()):
        assert local_images.load_reference(ref).size == (40, 40)


def test_the_backend_is_used_only_when_asked_for_and_is_free(env, monkeypatch, fake):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("IMAGE_BACKEND", "local_sd")
    data, usage = images.generate_image("a portrait")
    assert Image.open(io.BytesIO(data)).size == (1080, 1920) and usage["cost_usd"] == 0 and usage["model"].startswith("local:")
    assert fake["t2i"].calls[0]["width"] == 512 and fake["t2i"].calls[0]["num_inference_steps"] == 1 and not fake["i2i"].calls
    monkeypatch.delenv("IMAGE_BACKEND")
    data, usage = images.generate_image("a portrait")                  # back to the fixture
    assert usage["model"] == "gpt-image-2.5-flare" and len(fake["t2i"].calls) == 1


def test_a_reference_starts_an_image_to_image_run_from_its_middle_square(env, monkeypatch, fake):
    monkeypatch.setenv("IMAGE_BACKEND", "local_sd")
    tall = Image.new("RGB", (1080, 1920), (0, 0, 0))
    tall.paste((10, 200, 10), (0, 420, 1080, 1500))
    buf = io.BytesIO()
    tall.save(buf, "PNG")
    data, usage = images.generate_image("the same woman, full body", references=[buf.getvalue()])
    call = fake["i2i"].calls[0]
    assert usage["used_reference"] and call["image"].size == (512, 512) and call["image"].getpixel((256, 256)) == (10, 200, 10)
    assert call["num_inference_steps"] * call["strength"] >= 1          # SD-Turbo's requirement
    assert not fake["t2i"].calls


def test_same_prompt_same_seed_and_too_many_references_still_rejected(env, monkeypatch, fake):
    monkeypatch.setenv("IMAGE_BACKEND", "local_sd")
    assert local_images._seed("x") == local_images._seed("x") != local_images._seed("y")
    from app.jobs.errors import PermanentError

    with pytest.raises(PermanentError):
        images.generate_image("x", references=[b"1"] * 17)


def test_the_most_specific_instruction_survives_the_models_77_token_limit():
    long_description = " ".join(f"detail{i}" for i in range(120))
    prompt = f"A woman. {long_description}.\nLook: warm amber light.\nfront-facing head-and-shoulders portrait, neutral expression, plain background"
    out = local_images.prepare_prompt(prompt)
    assert out.startswith("front-facing head-and-shoulders portrait, neutral expression, plain background")
    assert len(out.split()) == local_images.PROMPT_WORDS and "detail100" not in out
    pack = "The same person as the reference portrait. " + long_description + ". Head-and-shoulders portrait showing a anger expression, plain background."
    assert local_images.prepare_prompt(pack).startswith("Head-and-shoulders portrait showing a anger expression")
    assert local_images.prepare_prompt("short prompt") == "short prompt" and local_images.prepare_prompt("") == ""
