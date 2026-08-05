from applypilot.apply.google_passwords import (
    PasswordFormResult,
    browser_credential_tool_contract,
    choose_chrome_profile_for_google_passwords,
    chrome_profiles_with_password_store,
    satisfy_password_form_with_google_password_manager,
)


def test_browser_credential_tool_generates_and_saves_without_secret_access():
    contract = browser_credential_tool_contract(allow_account_creation=True)

    assert contract == {
        "provider": "google_password_manager",
        "interface": "chrome_inline_password_manager_ui",
        "allowed_operations": [
            "autofill_existing_login",
            "generate_and_save_new_password",
        ],
        "secret_access": "browser_only_never_model_or_response",
        "prompt_policy": "never_ask_applicant_for_authentication",
        "unavailable_behavior": "return_structured_blocker_without_prompting",
        "completion_evidence": (
            "password_fields_populated_account_continuation_activated_and_gate_cleared"
        ),
    }


class _PasswordField:
    def __init__(self, *, populated=False):
        self.populated = populated
        self.clicks = 0

    def evaluate(self, script):
        if "Boolean" in script:
            return self.populated
        assert "preventDefault" in script
        return None

    def click(self, **_kwargs):
        self.clicks += 1


class _PasswordFields:
    def __init__(self, fields):
        self.fields = fields

    def count(self):
        return len(self.fields)

    def nth(self, index):
        return self.fields[index]

    @property
    def first(self):
        return self.fields[0]


class _Keyboard:
    def __init__(self, fields, *, accept):
        self.fields = fields
        self.accept = accept
        self.keys = []

    def press(self, key):
        self.keys.append(key)
        if key == "Enter" and self.accept:
            for field in self.fields:
                field.populated = True


class _PasswordPage:
    def __init__(self, *, field_count=2, populated=False, accept=True):
        self.fields = [_PasswordField(populated=populated) for _ in range(field_count)]
        self.password_fields = _PasswordFields(self.fields)
        self.keyboard = _Keyboard(self.fields, accept=accept)

    def locator(self, selector):
        assert selector == 'input[type="password"]:visible'
        return self.password_fields

    @staticmethod
    def wait_for_timeout(_milliseconds):
        return None


def test_google_password_manager_actually_populates_generated_password_fields():
    page = _PasswordPage()

    result = satisfy_password_form_with_google_password_manager(
        page,
        allow_generation=True,
    )

    assert result == PasswordFormResult("generated", 2)
    assert page.keyboard.keys == ["ArrowDown", "Enter"]
    assert all(field.populated for field in page.fields)
    assert page.fields[0].clicks == 1


def test_google_password_manager_fails_closed_when_inline_ui_does_not_fill():
    page = _PasswordPage(accept=False)

    result = satisfy_password_form_with_google_password_manager(
        page,
        allow_generation=True,
    )

    assert result == PasswordFormResult("unavailable", 2)


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
