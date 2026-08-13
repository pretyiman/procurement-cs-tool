import pytest


@pytest.fixture(autouse=True)
def _never_touch_real_custom_docx_templates_dir(tmp_path, monkeypatch):
    """Applies to every test in the suite, not just test_template_manager.py.
    Any test that triggers Contract Award / Purchase Proposal generation
    (directly or via an HTTP route) resolves a docx template path, which in
    dev mode defaults to living at the real repo root (paths.user_data_dir()
    - same convention as the SQLite DB). Without this, running the test
    suite could leave stray files/folders in the actual project directory
    depending on test order and timing (observed intermittently once real
    Word COM automation - test_template_manager.py's .doc conversion tests
    - was added into the mix)."""
    import app.paths as paths_module
    import app.template_manager as template_manager_module

    custom_dir = tmp_path / "docx_templates"
    custom_dir.mkdir()
    monkeypatch.setattr(paths_module, "custom_docx_templates_dir", lambda: custom_dir)
    monkeypatch.setattr(template_manager_module, "custom_docx_templates_dir", lambda: custom_dir)
