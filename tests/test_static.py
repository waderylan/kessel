from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"


def test_model_control_is_provider_aware_dropdown() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert '<select\n              id="model"' in html
    assert 'id="model"\n              name="model"\n              type="text"' not in html
    assert "`/v1/${provider}/models`" in script
    assert 'value: "default", label: "Default (account selection)"' in script
    assert "modelRequestSequence" in script
    assert 'app.js?v=20260920-model-select' in html
    assert 'styles.css?v=20260920-model-select' in html


def test_ui_copy_has_no_long_dash_characters() -> None:
    for path in STATIC.glob("*"):
        if path.suffix not in {".html", ".js", ".css"}:
            continue
        content = path.read_text(encoding="utf-8")
        assert "—" not in content
        assert "–" not in content
