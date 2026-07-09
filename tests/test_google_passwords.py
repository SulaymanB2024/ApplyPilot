from applypilot.apply.google_passwords import (
    choose_chrome_profile_for_google_passwords,
    chrome_profiles_with_password_store,
)


def test_chrome_profiles_with_password_store_detects_metadata_only(tmp_path):
    profile = tmp_path / "Profile 2"
    profile.mkdir()
    (profile / "Login Data").write_bytes(b"sqlite metadata")
    (tmp_path / "Default").mkdir()

    assert chrome_profiles_with_password_store(tmp_path) == ["Profile 2"]


def test_choose_chrome_profile_prefers_default_with_password_store(tmp_path, monkeypatch):
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Login Data").write_bytes(b"sqlite metadata")
    other = tmp_path / "Profile 3"
    other.mkdir()
    (other / "Login Data").write_bytes(b"sqlite metadata")
    monkeypatch.delenv("APPLYPILOT_CHROME_PROFILE_DIRECTORY", raising=False)

    assert choose_chrome_profile_for_google_passwords(tmp_path) == "Default"


def test_choose_chrome_profile_honors_explicit_profile(tmp_path, monkeypatch):
    (tmp_path / "Default").mkdir()
    (tmp_path / "Profile 7").mkdir()
    monkeypatch.setenv("APPLYPILOT_CHROME_PROFILE_DIRECTORY", "Profile 7")

    assert choose_chrome_profile_for_google_passwords(tmp_path) == "Profile 7"
