"""Demo mode: DRY_RUN=1 with some language-model roles real (LIVE_LLM_ROLES). Nothing here calls a real model."""
from pathlib import Path
import subprocess

from test_phase5 import env  # noqa: F401 (fixture)

from pydantic import BaseModel

from app.vendors import llm


class Answer(BaseModel):
    text: str


class FakeRaw:
    usage_metadata = {"input_tokens": 1000, "output_tokens": 500}
    content = "real words"


class FakeChat:
    calls: list[str] = []

    def __init__(self, model):
        self.model = model

    def invoke(self, msgs):
        FakeChat.calls.append(self.model)
        return FakeRaw()

    def with_structured_output(self, schema, method, include_raw):
        chat = self

        class R:
            def invoke(self, msgs):
                FakeChat.calls.append(chat.model)
                return {"raw": FakeRaw(), "parsed": schema(text="from the real model"), "parsing_error": None}

        return R()


def patch_model(monkeypatch):
    import langchain.chat_models as cm

    FakeChat.calls = []
    monkeypatch.setattr(cm, "init_chat_model", lambda model, **kw: FakeChat(model))


def test_only_the_listed_roles_call_the_real_model_in_a_dry_run(env, monkeypatch):
    patch_model(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("LIVE_LLM_ROLES", "writer, director")
    out, usage = llm.complete("writer", "system", [{"role": "user", "content": "hi"}], schema=Answer, fixture=Answer(text="sample"))
    assert out.text == "from the real model" and usage["cost_usd"] > 0 and FakeChat.calls == ["anthropic:claude-sonnet-5-5"]
    other, other_usage = llm.complete("vision_qa", "system", [{"role": "user", "content": "hi"}], schema=Answer, fixture=Answer(text="sample"))
    assert other.text == "sample" and other_usage["cost_usd"] == 0 and len(FakeChat.calls) == 1        # not listed: still the sample
    text, _ = llm.complete("director", "system", [{"role": "user", "content": "hi"}])
    assert text == "real words" and len(FakeChat.calls) == 2


def test_without_the_setting_a_dry_run_never_calls_a_model(env, monkeypatch):
    patch_model(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    out, _ = llm.complete("writer", "system", [{"role": "user", "content": "hi"}], schema=Answer, fixture=Answer(text="sample"))
    assert out.text == "sample" and FakeChat.calls == []


def test_the_story_prompt_states_a_shorter_episode_range(env, monkeypatch):
    from app.stages import story

    assert story.episode_count_note() == ""                                         # the guide's range includes 45: nothing to add
    monkeypatch.setattr(story, "get_config", lambda key: [6, 8])
    note = story.episode_count_note()
    assert "between 6 and 8" in note and "replaces the default of 45" in note


def test_preflight_checks_the_anthropic_key_only_for_a_live_demo(env, monkeypatch):
    from scripts import preflight

    monkeypatch.setattr(preflight, "CORE", [])
    monkeypatch.setattr(preflight, "VENDOR", [("anthropic", lambda: (preflight.OK, "key works")), ("openai", lambda: (preflight.FAIL, "must not be called"))])
    assert {r[0]: r[1] for r in preflight.run_all(dry_run=True)} == {"anthropic": preflight.SKIP, "openai": preflight.SKIP}
    monkeypatch.setenv("LIVE_LLM_ROLES", "story")
    assert {r[0]: r[1] for r in preflight.run_all(dry_run=True)} == {"anthropic": preflight.OK, "openai": preflight.SKIP}


def test_the_demo_switch_script_is_valid_and_keeps_the_key_off_the_command_line():
    path = Path(__file__).resolve().parent.parent / "scripts" / "live_story.sh"
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    text = path.read_text()
    assert "read -rsp" in text and "chmod 600 .env" in text and "set -euo pipefail" in text
    assert "orchestr" not in text and 'LIVE_ROLES="story,writer,originality,director,copy"' in text
