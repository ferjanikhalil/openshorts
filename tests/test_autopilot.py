"""Unit tests for render_options.py autopilot additions:
resolve_cascade (batch->video merge) and render_options_to_operations (recipe compile).
"""
from render_options import (
    RenderOptions,
    resolve_cascade,
    render_options_to_operations,
)


def _recipe(**kw) -> RenderOptions:
    return RenderOptions.model_validate(kw)


class TestResolveCascade:
    def test_no_override_returns_equivalent(self):
        base = _recipe(subtitles={"enabled": True, "font_size": 20})
        out = resolve_cascade(base, None)
        assert out.subtitles.enabled is True
        assert out.subtitles.font_size == 20

    def test_returns_fresh_object_no_mutation(self):
        base = _recipe(subtitles={"enabled": True, "font_size": 20})
        out = resolve_cascade(base, {"subtitles": {"font_size": 40}})
        assert out.subtitles.font_size == 40
        # The shared batch recipe must never be mutated by baking a child.
        assert base.subtitles.font_size == 20
        assert out is not base

    def test_field_merge_keeps_inherited_fields(self):
        base = _recipe(subtitles={"enabled": True, "font_size": 20, "font_name": "Arial"})
        out = resolve_cascade(base, {"subtitles": {"font_size": 40}})
        assert out.subtitles.font_size == 40       # overridden
        assert out.subtitles.font_name == "Arial"  # inherited
        assert out.subtitles.enabled is True       # inherited

    def test_video_enables_module_batch_left_off(self):
        base = _recipe(subtitles={"enabled": True})
        out = resolve_cascade(base, {"hook": {"enabled": True, "position": "center"}})
        assert out.hook is not None
        assert out.hook.enabled is True
        assert out.hook.position == "center"

    def test_video_disables_inherited_module(self):
        base = _recipe(subtitles={"enabled": True}, hook={"enabled": True})
        out = resolve_cascade(base, {"hook": {"enabled": False}})
        assert out.subtitles.enabled is True
        assert out.hook.enabled is False

    def test_sparse_override_untouched_modules_inherit(self):
        base = _recipe(
            subtitles={"enabled": True, "font_size": 18},
            hook={"enabled": True, "size": "L"},
        )
        out = resolve_cascade(base, {"subtitles": {"font_size": 30}})
        assert out.subtitles.font_size == 30
        assert out.hook.size == "L"  # untouched


class TestRenderOptionsToOperations:
    def test_empty_recipe_no_ops(self):
        assert render_options_to_operations(_recipe()) == []

    def test_disabled_modules_gated_out(self):
        r = _recipe(subtitles={"enabled": False}, hook={"enabled": False})
        assert render_options_to_operations(r) == []

    def test_canonical_order(self):
        r = _recipe(
            translate={"enabled": True, "target_language": "es"},
            hook={"enabled": True},
            subtitles={"enabled": True},
            auto_edit={"enabled": True},
            branding={"logo": {"enabled": True}},
        )
        ops = render_options_to_operations(r, gemini_key="g", elevenlabs_key="e")
        assert [o["type"] for o in ops] == [
            "auto_edit", "subtitle", "hook", "branding", "translate",
        ]

    def test_subtitles_field_maps_to_subtitle_op(self):
        r = _recipe(subtitles={"enabled": True, "font_size": 22})
        ops = render_options_to_operations(r)
        assert len(ops) == 1
        assert ops[0]["type"] == "subtitle"
        assert ops[0]["config"]["font_size"] == 22
        assert "enabled" not in ops[0]["config"]

    def test_branding_config_is_empty(self):
        r = _recipe(branding={"logo": {"enabled": True, "position": "top_left"}})
        ops = render_options_to_operations(r)
        assert ops == [{"type": "branding", "config": {}}]

    def test_translate_requires_target_language(self):
        # enabled but no target -> not emitted
        r = _recipe(translate={"enabled": True})
        assert render_options_to_operations(r) == []
        # enabled + target -> emitted with the ElevenLabs key threaded in
        r2 = _recipe(translate={"enabled": True, "target_language": "fr"})
        ops = render_options_to_operations(r2, elevenlabs_key="EL")
        assert ops == [{"type": "translate",
                        "config": {"api_key": "EL", "target_language": "fr"}}]

    def test_auto_edit_carries_gemini_key(self):
        r = _recipe(auto_edit={"enabled": True})
        ops = render_options_to_operations(r, gemini_key="GKEY")
        assert ops == [{"type": "auto_edit", "config": {"api_key": "GKEY"}}]

    def test_hook_without_text_omits_text(self):
        r = _recipe(hook={"enabled": True, "position": "top"})
        ops = render_options_to_operations(r)
        assert ops[0]["type"] == "hook"
        assert "text" not in ops[0]["config"]

    def test_translate_source_language_passed_through(self):
        r = _recipe(translate={"enabled": True, "target_language": "de",
                               "source_language": "en"})
        ops = render_options_to_operations(r, elevenlabs_key="EL")
        assert ops[0]["config"]["source_language"] == "en"
