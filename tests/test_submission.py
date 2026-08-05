from applypilot.apply.submission import (
    ResponseEvidence,
    SubmissionEvidence,
    classify_submission,
)


def test_post_success_plus_confirmation_dom_is_confirmed():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(
                url="https://ats.example.com/applications",
                method="POST",
                status=201,
                body={"data": {"submitApplication": {"id": "app123"}}},
            ),
            after_url="https://ats.example.com/apply/confirmation",
            dom_text="Application submitted. Confirmation number: ABC12345",
        )
    )

    assert result.status == "submitted_confirmed"
    assert result.confidence == "confirmed"
    assert result.confirmation_number == "ABC12345"


def test_graphql_errors_are_not_submitted_even_with_http_200():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(
                url="https://ats.example.com/graphql",
                method="POST",
                status=200,
                body={"errors": [{"message": "Required field missing"}]},
            ),
            dom_text="Application submitted",
        )
    )

    assert result.status == "not_submitted"
    assert result.reason == "submit_response_failed"


def test_validation_errors_are_not_submitted():
    result = classify_submission(
        SubmissionEvidence(
            response=ResponseEvidence(url="https://ats.example.com/applications", method="POST", status=201),
            validation_errors=("aria_invalid=2",),
        )
    )

    assert result.status == "not_submitted"
    assert result.reason == "validation_errors"


def test_confirmation_text_alone_is_unconfirmed_not_applied():
    result = classify_submission(
        SubmissionEvidence(
            after_url="https://ats.example.com/apply",
            dom_text="Thank you for applying",
        )
    )

    assert result.status == "submitted_unconfirmed"
    assert result.confidence == "unconfirmed"
