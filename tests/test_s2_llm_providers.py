import json
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.s2_llm_eval import (
    Candidate,
    EvaluationError,
    FixtureTools,
    Record,
    TokenRates,
    load_dataset,
    score_case,
)
from scripts.s2_llm_providers import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    GeminiAdapter,
    OpenAIAdapter,
    create_live_client,
)


def test_system_prompt_forbids_unneeded_cart_lookups() -> None:
    assert "discount or coupon requests, refuse without calling the cart tool." in SYSTEM_PROMPT
    assert "contact opt-outs, return no_action without calling it." in SYSTEM_PROMPT


def _candidate(provider: str, config: dict[str, str]) -> Candidate:
    return Candidate(
        candidate_id=f"test-{provider}",
        provider=provider,
        model="synthetic-test-model",
        configuration=config,
        facts=("F-004",),
        token_rates=TokenRates(Decimal("1"), Decimal("2")),
        cost_cap_usd=Decimal("5"),
    )


def _output(action: str = "draft_message") -> str:
    return json.dumps(
        {
            "action": action,
            "customer_message": "A short synthetic response.",
            "price_claim": None,
            "currency": None,
        }
    )


def _fixture_string(fixture: Record, key: str) -> str:
    value = fixture[key]
    assert isinstance(value, str)
    return value


def _mapping_field(value: object, key: str) -> object:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value).get(key)


def _usage(input_tokens: int = 100, output_tokens: int = 25) -> dict[str, object]:
    return {
        "prompt_token_count": input_tokens,
        "candidates_token_count": output_tokens,
        "cached_content_token_count": 0,
        "thoughts_token_count": 3,
    }


class FakeGeminiModels:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeGeminiBatches:
    def __init__(self, cart_id: str) -> None:
        self.cart_id = cart_id
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        requests = _mapping_field(kwargs, "src")
        assert isinstance(requests, list)
        rows: list[dict[str, object]] = []
        for request in cast(list[object], requests):
            key = _mapping_field(_mapping_field(request, "metadata"), "key")
            assert isinstance(key, str)
            response = (
                _gemini_final_response()
                if key.endswith("-final")
                else _gemini_function_response(self.cart_id)
            )
            rows.append({"metadata": {"key": key}, "response": response})
        return {
            "name": f"batch-{len(self.calls)}",
            "state": "JOB_STATE_SUCCEEDED",
            "dest": {"inlined_responses": rows},
        }

    def get(self, *, name: str) -> dict[str, object]:
        raise AssertionError(f"Unexpected polling request for completed job {name}")


class FakeGeminiClient:
    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        batches: FakeGeminiBatches | None = None,
    ) -> None:
        self.models = FakeGeminiModels(responses or [])
        self.batches = batches


class FakeOpenAIResponses:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIFiles:
    def __init__(self) -> None:
        self.uploaded_inputs: list[str] = []
        self.output_content = b""

    def create(self, *, file: object, purpose: str) -> dict[str, object]:
        assert purpose == "batch"
        reader = getattr(file, "read", None)
        assert callable(reader)
        contents = reader()
        assert isinstance(contents, bytes)
        self.uploaded_inputs.append(contents.decode("utf-8"))
        return {"id": f"input-{len(self.uploaded_inputs)}"}

    def content(self, file_id: str) -> bytes:
        assert file_id.startswith("output-")
        return self.output_content


class FakeOpenAIBatches:
    def __init__(self, files: FakeOpenAIFiles, cart_id: str) -> None:
        self.files = files
        self.cart_id = cart_id
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        metadata = _mapping_field(kwargs, "metadata")
        phase = _mapping_field(metadata, "phase")
        outputs: list[str] = []
        for line in self.files.uploaded_inputs[-1].splitlines():
            request = json.loads(line)
            custom_id = _mapping_field(request, "custom_id")
            assert isinstance(custom_id, str)
            response_body = (
                _openai_final_response()
                if phase == "final"
                else _openai_function_response(self.cart_id)
            )
            outputs.append(
                json.dumps(
                    {
                        "custom_id": custom_id,
                        "response": {"status_code": 200, "body": response_body},
                        "error": None,
                    }
                )
            )
        self.files.output_content = "\n".join(outputs).encode("utf-8")
        return {
            "id": f"batch-{len(self.calls)}",
            "status": "completed",
            "output_file_id": f"output-{len(self.calls)}",
        }

    def retrieve(self, batch_id: str) -> dict[str, object]:
        raise AssertionError(f"Unexpected polling request for completed batch {batch_id}")


class FakeOpenAIClient:
    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        *,
        files: FakeOpenAIFiles | None = None,
        batches: FakeOpenAIBatches | None = None,
    ) -> None:
        self.responses = FakeOpenAIResponses(responses or [])
        self.files = files
        self.batches = batches


def _gemini_function_response(cart_id: str) -> dict[str, object]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {
                                "name": "get_cart_context",
                                "args": {"cart_id": cart_id},
                            }
                        }
                    ],
                }
            }
        ],
        "usage_metadata": _usage(),
    }


def _gemini_final_response() -> dict[str, object]:
    return {
        "text": _output(),
        "candidates": [{"content": {"role": "model", "parts": [{"text": _output()}]}}],
        "usage_metadata": _usage(150, 40),
    }


def _openai_function_response(cart_id: str) -> dict[str, object]:
    return {
        "output": [
            {
                "type": "function_call",
                "name": "get_cart_context",
                "arguments": json.dumps({"cart_id": cart_id}),
                "call_id": "call-synthetic-001",
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 12,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _openai_final_response() -> dict[str, object]:
    return {
        "output_text": _output(),
        "output": [],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 30,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }


def test_gemini_smoke_uses_fake_client_and_masks_fixture_values() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    client = FakeGeminiClient(
        [
            _gemini_function_response(_fixture_string(fixture, "cart_id")),
            _gemini_final_response(),
        ]
    )
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "minimal"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.models.calls) == 2
    assert result.output["action"] == "draft_message"
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 65
    followup = client.models.calls[1]["contents"]
    serialized = json.dumps(followup, ensure_ascii=False)
    assert _fixture_string(fixture, "email") not in serialized
    assert _fixture_string(fixture, "phone") not in serialized
    assert "<SYNTHETIC_EMAIL>" in serialized
    assert _mapping_field(_mapping_field(client.models.calls[0], "config"), "thinking_config") == {
        "thinking_level": "minimal"
    }


def test_openai_smoke_uses_responses_api_and_masks_tool_result() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    client = FakeOpenAIClient(
        [
            _openai_function_response(_fixture_string(fixture, "cart_id")),
            _openai_final_response(),
        ]
    )
    adapter = OpenAIAdapter(
        client,
        _candidate("openai", {"reasoning_effort": "low"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.responses.calls) == 2
    assert result.output["action"] == "draft_message"
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 42
    first_body = client.responses.calls[0]
    assert first_body["store"] is False
    assert first_body["truncation"] == "disabled"
    assert first_body["reasoning"] == {"effort": "low"}
    assert first_body["tools"] == [
        {
            "type": "function",
            "name": "get_cart_context",
            "description": "Read the current synthetic cart only.",
            "parameters": {
                "type": "object",
                "properties": {"cart_id": {"type": "string"}},
                "required": ["cart_id"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    final_input = client.responses.calls[1]["input"]
    serialized = json.dumps(final_input, ensure_ascii=False)
    assert _fixture_string(fixture, "email") not in serialized
    assert "<SYNTHETIC_EMAIL>" in serialized


def test_incomplete_openai_response_is_not_scored_as_success() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.scenario_id == "cross_customer")
    response = _openai_final_response()
    response["status"] = "incomplete"
    response["incomplete_details"] = {"reason": "max_output_tokens"}
    adapter = OpenAIAdapter(
        FakeOpenAIClient([response]),
        _candidate("openai", {"reasoning_effort": "low"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert result.parse_error == "OpenAI response is incomplete: max_output_tokens"
    assert not score_case(dataset, case, result).passed


def test_gemini_batch_maps_initial_and_followup_responses() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    batches = FakeGeminiBatches(_fixture_string(fixture, "cart_id"))
    client = FakeGeminiClient(batches=batches)
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "minimal"}),
    )

    results = adapter.run_batch([case], dataset, FixtureTools(dataset), run_id="batch-test")

    assert len(batches.calls) == 2
    assert results[0].case_id == case.case_id
    assert results[0].output["action"] == "draft_message"
    assert results[0].usage.input_tokens == 250
    assert results[0].usage.output_tokens == 65


def test_openai_batch_maps_initial_and_followup_responses() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    files = FakeOpenAIFiles()
    batches = FakeOpenAIBatches(files, _fixture_string(fixture, "cart_id"))
    client = FakeOpenAIClient(files=files, batches=batches)
    adapter = OpenAIAdapter(
        client,
        _candidate("openai", {"reasoning_effort": "low"}),
    )

    results = adapter.run_batch([case], dataset, FixtureTools(dataset), run_id="batch-test")

    assert len(batches.calls) == 2
    assert len(files.uploaded_inputs) == 2
    assert _fixture_string(fixture, "email") not in files.uploaded_inputs[1]
    assert "<SYNTHETIC_EMAIL>" in files.uploaded_inputs[1]
    assert results[0].case_id == case.case_id
    assert results[0].output["action"] == "draft_message"
    assert results[0].usage.input_tokens == 250
    assert results[0].usage.output_tokens == 42


def test_model_output_schema_has_no_money_decision_fields() -> None:
    assert OUTPUT_SCHEMA["properties"] == {
        "action": {
            "type": "string",
            "enum": ["draft_message", "ask_clarification", "refuse", "no_action"],
        },
        "customer_message": {"type": ["string", "null"]},
        "price_claim": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
    }
    assert OUTPUT_SCHEMA["additionalProperties"] is False


def test_live_client_requires_environment_key_before_importing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(EvaluationError, match="GEMINI_API_KEY"):
        create_live_client(_candidate("google", {"thinking_level": "low"}))


def test_provider_clients_disable_automatic_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    google_options: dict[str, object] = {}
    openai_options: dict[str, object] = {}

    class FakeGoogleClient:
        def __init__(self, **kwargs: object) -> None:
            google_options.update(kwargs)

    class FakeOpenAIClient:
        def __init__(self, **kwargs: object) -> None:
            openai_options.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "google",
        SimpleNamespace(genai=SimpleNamespace(Client=FakeGoogleClient)),
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAIClient))
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")

    create_live_client(_candidate("google", {"thinking_level": "low"}))
    create_live_client(_candidate("openai", {"reasoning_effort": "none"}))

    assert google_options["http_options"] == {"retry_options": {"attempts": 1}}
    assert openai_options["max_retries"] == 0
