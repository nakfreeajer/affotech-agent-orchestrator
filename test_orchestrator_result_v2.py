import hashlib

import pytest

from orchestrator_result_v2 import is_attempted_v2_control, parse_dual_protocol_control, parse_v2_control_envelope


TASK = "000102"
DIGEST = hashlib.sha256(b"prompt").hexdigest()
EXECUTE = (
    "<ORCHESTRATOR_RESULT>\n"
    "schemaVersion=2\nclassification=ACCEPTED\naction=EXECUTE\n"
    f"taskId={TASK}\ndocumentation=REQUIRED\npromptTransport=ARTIFACT_V1\n"
    f"promptSourceArtifactId=prompt-source-{DIGEST}\npromptSha256={DIGEST}\n"
    "promptByteLength=6\n</ORCHESTRATOR_RESULT>"
)


@pytest.mark.parametrize("action", ["HUMAN_REQUIRED", "STOP"])
def test_v2_terminal_actions(action):
    text = ("<ORCHESTRATOR_RESULT>\nschemaVersion=2\nclassification=BLOCKED\n"
            f"action={action}\ntaskId={TASK}\ndocumentation=COMPLETE\n</ORCHESTRATOR_RESULT>")
    result = parse_v2_control_envelope(text, TASK)
    assert result["action"] == action
    assert "promptSha256" not in result


def test_v2_execute_parses_exact_identity():
    result = parse_v2_control_envelope(EXECUTE, TASK)
    assert result["protocolVersion"] == 2
    assert result["promptSha256"] == DIGEST
    assert result["promptByteLength"] == 6


@pytest.mark.parametrize("mutate", [
    lambda s: s.replace(f"taskId={TASK}", "taskId=000103"),
    lambda s: s.replace("schemaVersion=2\n", ""),
    lambda s: s + "\n<ORCHESTRATOR_RESULT>",
    lambda s: s.replace("classification=ACCEPTED", "action=STOP\nclassification=ACCEPTED"),
    lambda s: s.replace("promptTransport=ARTIFACT_V1\n", "path=C:\\temp\\x\npromptTransport=ARTIFACT_V1\n"),
    lambda s: s.replace(f"promptSourceArtifactId=prompt-source-{DIGEST}", "promptSourceArtifactId=prompt.md"),
    lambda s: s.replace(DIGEST, DIGEST.upper()),
    lambda s: s.replace("promptByteLength=6", "promptByteLength=0"),
    lambda s: s.replace("promptByteLength=6", "promptByteLength=-1"),
    lambda s: s.replace(f"promptSourceArtifactId=prompt-source-{DIGEST}", f"promptSourceArtifactId=prompt-source-{'0' * 64}"),
    lambda s: s.replace("promptByteLength=6", "promptByteLength=6\npromptBegin\nx\npromptEnd"),
    lambda s: s.replace("schemaVersion=2", "schemaVersion=3"),
])
def test_v2_malformed_execute_fails_closed(mutate):
    with pytest.raises(ValueError):
        parse_v2_control_envelope(mutate(EXECUTE), TASK)


@pytest.mark.parametrize("action", ["HUMAN_REQUIRED", "STOP"])
def test_terminal_rejects_prompt_fields(action):
    text = ("<ORCHESTRATOR_RESULT>\nschemaVersion=2\nclassification=ACCEPTED\n"
            f"action={action}\ntaskId={TASK}\ndocumentation=COMPLETE\n"
            f"promptSha256={DIGEST}\n</ORCHESTRATOR_RESULT>")
    with pytest.raises(ValueError):
        parse_v2_control_envelope(text, TASK)


def test_explicit_malformed_v2_never_falls_back_to_v1():
    with pytest.raises(ValueError):
        parse_dual_protocol_control(EXECUTE.replace("schemaVersion=2", "schemaVersion=9"), TASK)


def test_dual_api_routes_valid_v2():
    assert parse_dual_protocol_control(EXECUTE, TASK)["protocolVersion"] == 2


def test_v2_only_fields_are_attempted_v2_without_schema_and_do_not_fall_through():
    text = EXECUTE.replace("schemaVersion=2\n", "")
    assert is_attempted_v2_control(text)
    with pytest.raises(ValueError):
        parse_dual_protocol_control(text, TASK)
