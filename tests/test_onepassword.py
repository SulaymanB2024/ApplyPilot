import json
import subprocess

from applypilot.apply.onepassword import (
    OnePasswordClient,
    build_login_title,
    chrome_profiles_with_extension,
    domain_from_url,
    redact_data,
    redact_text,
)


def completed(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr="")


def test_domain_from_url_normalizes_common_inputs():
    assert domain_from_url("https://www.example.com/jobs/1") == "example.com"
    assert domain_from_url("boards.greenhouse.io/company") == "boards.greenhouse.io"


def test_redact_text_and_data_remove_secret_values():
    assert "secret123" not in redact_text("password=secret123", ["secret123"])
    data = redact_data({"password": "secret123", "nested": {"token": "abc"}, "ok": "value"})

    assert data["password"] == "[REDACTED]"
    assert data["nested"]["token"] == "[REDACTED]"
    assert data["ok"] == "value"


def test_chrome_profiles_with_extension_detects_profile(tmp_path):
    extension_id = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"
    (tmp_path / "Profile 2" / "Extensions" / extension_id).mkdir(parents=True)
    (tmp_path / "Default").mkdir()

    assert chrome_profiles_with_extension(tmp_path, extension_id) == ["Profile 2"]


def test_create_login_uses_1password_generated_password_and_metadata():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[1:4] == ["whoami", "--format", "json"]:
            return completed(args, stdout='{"account_uuid":"acct"}')
        payload = {
            "id": "item123",
            "title": "ApplyPilot - Example - candidate@example.com",
            "urls": [{"href": "https://example.com/apply"}],
            "fields": [
                {"id": "username", "value": "candidate@example.com"},
                {"id": "password", "value": "generated-secret"},
            ],
        }
        return completed(args, stdout=json.dumps(payload))

    client = OnePasswordClient(op_path="/usr/bin/op", vault="Private", runner=runner)
    login = client.create_login(
        title=build_login_title(domain="example.com", company="Example", email="candidate@example.com"),
        domain="example.com",
        username="candidate@example.com",
        login_url="https://example.com/apply",
        job_url="https://jobs.example.com/1",
        application_url="https://example.com/apply",
        run_id="worker-0",
    )

    create_call = calls[1]
    assert "password[generate]=letters,digits,symbols,32" in create_call
    assert "--vault" in create_call
    assert "ApplyPilot.status[text]=pending" in create_call
    assert login.password == "generated-secret"
    assert login.pending is True


def test_find_login_filters_by_domain_and_username():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[1:4] == ["whoami", "--format", "json"]:
            return completed(args, stdout='{"account_uuid":"acct"}')
        if args[1:5] == ["item", "list", "--categories", "login"]:
            return completed(
                args,
                stdout=json.dumps([
                    {
                        "id": "item123",
                        "title": "ApplyPilot - Example - candidate@example.com",
                        "urls": ["https://example.com"],
                    }
                ]),
            )
        return completed(
            args,
            stdout=json.dumps({
                "id": "item123",
                "title": "ApplyPilot - Example - candidate@example.com",
                "urls": [{"href": "https://example.com"}],
                "fields": [
                    {"id": "username", "value": "candidate@example.com"},
                    {"id": "password", "value": "secret"},
                ],
            }),
        )

    client = OnePasswordClient(op_path="/usr/bin/op", runner=runner)
    login = client.find_login(domain="https://example.com/apply", username="candidate@example.com")

    assert login is not None
    assert login.item_id == "item123"
    assert login.username == "candidate@example.com"
