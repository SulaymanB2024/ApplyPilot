from applypilot.apply.safety import PageInput, PageState, classify_page_state, classify_page_state_with_evidence


def test_benign_ssn_text_does_not_stop_without_structural_field():
    state = PageState(
        url="https://jobs.example.com/apply",
        text="We never ask for your SSN or bank account during applications.",
    )

    assert classify_page_state(state) is None


def test_password_input_stops_with_evidence():
    verdict = classify_page_state_with_evidence(
        PageState(
            url="https://jobs.example.com/apply",
            inputs=(PageInput(selector="#pw", type="password", label="Password"),),
        )
    )

    assert verdict is not None
    assert verdict.reason == "login_issue"
    assert "input[type=password]" in verdict.evidence


def test_password_can_be_deferred_to_login_handler_without_weakening_other_gates():
    password_state = PageState(
        url="https://jobs.example.com/login",
        inputs=(PageInput(selector="#pw", type="password", label="Password"),),
    )
    mfa_state = PageState(
        url="https://jobs.example.com/login",
        inputs=(
            PageInput(selector="#pw", type="password", label="Password"),
            PageInput(selector="#otp", type="text", autocomplete="one-time-code"),
        ),
    )

    assert classify_page_state_with_evidence(password_state, allow_password=True) is None
    verdict = classify_page_state_with_evidence(mfa_state, allow_password=True)
    assert verdict is not None
    assert verdict.reason == "mfa_required"


def test_otp_and_payment_autocomplete_stop():
    assert (
        classify_page_state(
            PageState(
                url="https://jobs.example.com/apply",
                inputs=(PageInput(type="text", autocomplete="one-time-code"),),
            )
        )
        == "mfa_required"
    )
    assert (
        classify_page_state(
            PageState(
                url="https://jobs.example.com/apply",
                inputs=(PageInput(type="text", autocomplete="cc-number"),),
            )
        )
        == "payment_or_tax_info"
    )


def test_sso_captcha_idv_and_id_upload_stop():
    assert classify_page_state("https://accounts.google.com/o/oauth", "") == "sso_required"
    assert (
        classify_page_state(
            PageState(url="https://jobs.example.com/apply", iframe_origins=("https://hcaptcha.com",))
        )
        == "captcha"
    )
    assert (
        classify_page_state(
            PageState(url="https://jobs.example.com/apply", iframe_origins=("https://withpersona.com",))
        )
        == "unsafe_verification"
    )
    assert (
        classify_page_state(
            PageState(
                url="https://jobs.example.com/apply",
                inputs=(
                    PageInput(
                        selector="#id",
                        type="file",
                        label="Upload government ID",
                        accept="image/png,image/jpeg",
                    ),
                ),
            )
        )
        == "unsafe_verification"
    )


def test_expired_text_still_maps_to_expired():
    assert classify_page_state("https://example.com", "This job is no longer available.") == "expired"


def test_page_inspection_failure_fails_closed():
    assert (
        classify_page_state(PageState(url="https://example.com", inspection_error="RuntimeError"))
        == "inspection_failed"
    )
