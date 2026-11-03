import json
import sys
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from urllib.request import Request

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
from scripts.s2_llm_prompts import (
    OUTPUT_SCHEMA,
    TOOL_DESCRIPTION,
    TOOL_PARAMETERS,
    system_prompt,
)
from scripts.s2_llm_providers import (
    GeminiAdapter,
    OpenAIAdapter,
    _count_gemini_request_tokens,
    create_live_client,
)


def test_prompts_list_every_schema_key_and_enum_value() -> None:
    properties = cast(dict[str, dict[str, object]], OUTPUT_SCHEMA["properties"])
    for provider in ("google", "openai"):
        prompt = system_prompt(provider)
        for key in properties:
            assert key in prompt
        for value in cast(list[str], properties["action"]["enum"]):
            assert value in prompt


def test_prompt_static_policy_comes_first_and_is_provider_specific() -> None:
    google, openai = system_prompt("google"), system_prompt("openai")
    assert google != openai
    assert google.split("\n", 1)[0] == openai.split("\n", 1)[0]
    assert "Call it at most once" in google
    assert "do not invent" in google.lower()


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


def _usage(
    input_tokens: int = 100,
    output_tokens: int = 25,
    *,
    tool_use_prompt_tokens: int = 0,
    thoughts_tokens: int = 3,
) -> dict[str, object]:
    return {
        "prompt_token_count": input_tokens,
        "tool_use_prompt_token_count": tool_use_prompt_tokens,
        "candidates_token_count": output_tokens,
        "cached_content_token_count": 0,
        "thoughts_token_count": thoughts_tokens,
        "total_token_count": (
            input_tokens + tool_use_prompt_tokens + output_tokens + thoughts_tokens
        ),
    }


class FakeGeminiModels:
    def __init__(
        self,
        responses: list[dict[str, object]],
        token_counts: list[int] | None = None,
    ) -> None:
        self.responses = responses
        self.token_counts = token_counts or []
        self.calls: list[dict[str, object]] = []
        self.count_calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeGeminiClient:
    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        token_counts: list[int] | None = None,
    ) -> None:
        self.models = FakeGeminiModels(responses or [], token_counts)


def _count_transport(
    client: FakeGeminiClient,
) -> Callable[[str, dict[str, object]], int]:
    def count(model: str, request: dict[str, object]) -> int:
        client.models.count_calls.append({"model": model, **request})
        return client.models.token_counts.pop(0)

    return count


class FakeOpenAIResponses:
    def __init__(
        self,
        responses: list[dict[str, object]],
        input_token_counts: list[int] | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.input_tokens = FakeOpenAIInputTokens(input_token_counts or [])

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeOpenAIInputTokens:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.calls: list[dict[str, object]] = []

    def count(self, **kwargs: object) -> dict[str, int]:
        self.calls.append(kwargs)
        return {"input_tokens": self.counts.pop(0) if self.counts else 900}


class FakeOpenAIClient:
    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        *,
        input_token_counts: list[int] | None = None,
    ) -> None:
        self.responses = FakeOpenAIResponses(responses or [], input_token_counts)


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
    assert result.parse_error is None
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 71
    assert result.usage.reasoning_tokens == 6
    first_config = _mapping_field(client.models.calls[0], "config")
    followup_config = _mapping_field(client.models.calls[1], "config")
    assert _mapping_field(first_config, "system_instruction") == system_prompt("google")
    assert _mapping_field(first_config, "response_json_schema") == OUTPUT_SCHEMA
    assert _mapping_field(first_config, "tools") is not None
    assert _mapping_field(followup_config, "tools") is None
    assert _mapping_field(followup_config, "tool_config") is None
    for field in ("system_instruction", "response_json_schema", "thinking_config"):
        assert _mapping_field(first_config, field) == _mapping_field(followup_config, field)
    followup = client.models.calls[1]["contents"]
    assert isinstance(followup, list)
    assert _mapping_field(followup[-1], "role") == "user"
    serialized = json.dumps(followup, ensure_ascii=False)
    assert _fixture_string(fixture, "email") not in serialized
    assert _fixture_string(fixture, "phone") not in serialized
    assert "<SYNTHETIC_EMAIL>" in serialized
    assert _mapping_field(first_config, "thinking_config") == {"thinking_level": "minimal"}


def test_gemini_smoke_output_cap_includes_thinking_tokens() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    client = FakeGeminiClient([_gemini_final_response()])
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "low"}),
        max_output_tokens=8192,
    )

    adapter.run_case(case, dataset, FixtureTools(dataset))

    config = _mapping_field(client.models.calls[0], "config")
    assert _mapping_field(config, "max_output_tokens") == 8192


def test_gemini_smoke_counts_each_request_before_generation() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    client = FakeGeminiClient(
        [
            _gemini_function_response(_fixture_string(fixture, "cart_id")),
            _gemini_final_response(),
        ],
        token_counts=[120, 140],
    )
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "low"}),
        max_input_tokens=140,
        max_output_tokens=8192,
        count_tokens_transport=_count_transport(client),
    )

    adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.models.count_calls) == len(client.models.calls) == 2
    followup_contents = client.models.calls[1]["contents"]
    assert isinstance(followup_contents, list)
    assert followup_contents[2]["role"] == "user"
    assert "function_response" in followup_contents[2]["parts"][0]
    for count_call, generation_call in zip(
        client.models.count_calls, client.models.calls, strict=True
    ):
        assert count_call["model"] == generation_call["model"]
        assert count_call["contents"] == generation_call["contents"]
        count_config = count_call["config"]
        assert _mapping_field(count_config, "system_instruction") == system_prompt("google")
        assert _mapping_field(count_config, "tools") == _mapping_field(
            generation_call["config"], "tools"
        )
    assert _mapping_field(client.models.calls[0]["config"], "tools") is not None
    assert _mapping_field(client.models.calls[1]["config"], "tools") is None


def test_gemini_smoke_rejects_input_over_ceiling_without_generation() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    client = FakeGeminiClient([_gemini_final_response()], token_counts=[141])
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "low"}),
        max_input_tokens=140,
        count_tokens_transport=_count_transport(client),
    )

    with pytest.raises(EvaluationError, match="exceeding the approved per-request ceiling"):
        adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.models.count_calls) == 1
    assert not client.models.calls


def test_gemini_smoke_rejects_over_ceiling_followup_before_generation() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    fixture = dataset.fixtures[case.fixture_id]
    client = FakeGeminiClient(
        [
            _gemini_function_response(_fixture_string(fixture, "cart_id")),
            _gemini_final_response(),
        ],
        token_counts=[139, 141],
    )
    adapter = GeminiAdapter(
        client,
        _candidate("google", {"thinking_level": "low"}),
        max_input_tokens=140,
        count_tokens_transport=_count_transport(client),
    )

    with pytest.raises(EvaluationError, match="exceeding the approved per-request ceiling"):
        adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.models.count_calls) == 2
    assert len(client.models.calls) == 1


def test_gemini_count_tokens_rest_request_includes_full_generation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("google.genai")
    request: dict[str, object] = {
        "contents": [
            {"role": "user", "parts": [{"text": "Synthetic smoke prompt"}]},
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "get_cart_context",
                            "args": {"cart_id": "s2-cart-001"},
                        },
                        "thought_signature": b"\x00\xff",
                    }
                ],
            },
        ],
        "config": {
            "system_instruction": system_prompt("google"),
            "tools": [
                {
                    "function_declarations": [
                        {
                            "name": "get_cart_context",
                            "description": TOOL_DESCRIPTION,
                            "parameters_json_schema": TOOL_PARAMETERS,
                        }
                    ]
                }
            ],
            "response_mime_type": "application/json",
            "response_json_schema": OUTPUT_SCHEMA,
            "tool_config": {
                "function_calling_config": {
                    "mode": "AUTO",
                }
            },
            "max_output_tokens": 8192,
        },
    }
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"totalTokens":123}'

    def fake_urlopen(http_request: object, timeout: int) -> FakeResponse:
        captured["request"] = http_request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("scripts.s2_llm_providers.urllib.request.urlopen", fake_urlopen)

    count = _count_gemini_request_tokens("test-api-key", "gemini-3.8-flash", request)

    assert count == 123
    http_request = cast(Request, captured["request"])
    assert isinstance(http_request.data, bytes)
    body = json.loads(http_request.data)
    generate_request = body["generateContentRequest"]
    assert generate_request["model"] == "models/gemini-3.8-flash"
    assert generate_request["systemInstruction"]["parts"][0]["text"] == system_prompt("google")
    assert generate_request["tools"][0]["functionDeclarations"][0]["name"] == "get_cart_context"
    assert generate_request["contents"][1]["parts"][0]["thoughtSignature"] == "AP8="
    assert "parametersJsonSchema" in generate_request["tools"][0]["functionDeclarations"][0]
    assert generate_request["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert "allowedFunctionNames" not in generate_request["toolConfig"]["functionCallingConfig"]
    assert generate_request["generationConfig"]["responseJsonSchema"] == OUTPUT_SCHEMA
    assert generate_request["generationConfig"]["maxOutputTokens"] == 8192
    assert generate_request["contents"][1]["parts"][0]["functionCall"]["name"] == "get_cart_context"
    assert captured["timeout"] == 30


def test_gemini_missing_usage_component_is_derived_from_total_when_exact() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    response = _gemini_final_response()
    usage = response["usage_metadata"]
    assert isinstance(usage, dict)
    usage.pop("thoughts_token_count")
    adapter = GeminiAdapter(
        FakeGeminiClient([response]),
        _candidate("google", {"thinking_level": "low"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert result.usage.reasoning_tokens == 3
    assert result.usage.output_tokens == 43


def test_gemini_multiple_missing_usage_components_are_derived_only_when_zero() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    response = _gemini_final_response()
    usage = response["usage_metadata"]
    assert isinstance(usage, dict)
    usage.pop("thoughts_token_count")
    usage.pop("tool_use_prompt_token_count")
    usage["total_token_count"] = usage["prompt_token_count"] + usage["candidates_token_count"]
    adapter = GeminiAdapter(
        FakeGeminiClient([response]),
        _candidate("google", {"thinking_level": "low"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 40
    assert result.usage.reasoning_tokens == 0
    assert result.usage.tool_use_prompt_tokens == 0


def test_gemini_tool_use_prompt_tokens_are_counted_as_input() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")
    response = _gemini_final_response()
    usage = response["usage_metadata"]
    assert isinstance(usage, dict)
    usage["tool_use_prompt_token_count"] = 7
    usage["total_token_count"] = 200
    adapter = GeminiAdapter(
        FakeGeminiClient([response]),
        _candidate("google", {"thinking_level": "low"}),
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert result.usage.input_tokens == 157
    assert result.usage.tool_use_prompt_tokens == 7
    assert result.usage.output_tokens == 43


def test_gemini_incomplete_or_inconsistent_usage_fails_closed() -> None:
    dataset = load_dataset()
    case = next(case for case in dataset.cases if case.case_id == "enmsa-010-en")

    incomplete = _gemini_final_response()
    incomplete_usage = incomplete["usage_metadata"]
    assert isinstance(incomplete_usage, dict)
    incomplete_usage.pop("thoughts_token_count")
    incomplete_usage.pop("tool_use_prompt_token_count")
    incomplete_adapter = GeminiAdapter(
        FakeGeminiClient([incomplete]),
        _candidate("google", {"thinking_level": "low"}),
    )
    with pytest.raises(EvaluationError, match="cannot allocate a nonzero residual"):
        incomplete_adapter.run_case(case, dataset, FixtureTools(dataset))

    inconsistent = _gemini_final_response()
    inconsistent_usage = inconsistent["usage_metadata"]
    assert isinstance(inconsistent_usage, dict)
    inconsistent_usage["total_token_count"] = 1
    inconsistent_adapter = GeminiAdapter(
        FakeGeminiClient([inconsistent]),
        _candidate("google", {"thinking_level": "low"}),
    )
    with pytest.raises(EvaluationError, match="does not equal its usage components"):
        inconsistent_adapter.run_case(case, dataset, FixtureTools(dataset))

    negative_residual = _gemini_final_response()
    negative_usage = negative_residual["usage_metadata"]
    assert isinstance(negative_usage, dict)
    negative_usage.pop("thoughts_token_count")
    negative_usage["total_token_count"] = 1
    negative_adapter = GeminiAdapter(
        FakeGeminiClient([negative_residual]),
        _candidate("google", {"thinking_level": "low"}),
    )
    with pytest.raises(EvaluationError, match="negative missing usage component"):
        negative_adapter.run_case(case, dataset, FixtureTools(dataset))


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
        max_input_tokens=4000,
    )

    result = adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.responses.calls) == 2
    assert result.output["action"] == "draft_message"
    assert result.parse_error is None
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 42
    first_body = client.responses.calls[0]
    first_count = client.responses.input_tokens.calls[0]
    second_body = client.responses.calls[1]
    second_count = client.responses.input_tokens.calls[1]
    assert first_body["store"] is False
    assert first_body["truncation"] == "disabled"
    assert first_body["reasoning"] == {"effort": "low"}
    assert first_body["tools"] == [
        {
            "type": "function",
            "name": "get_cart_context",
            "description": TOOL_DESCRIPTION,
            "parameters": TOOL_PARAMETERS,
            "strict": True,
        }
    ]
    final_input = client.responses.calls[1]["input"]
    final_body = client.responses.calls[1]
    assert "tools" not in final_body
    assert "tool_choice" not in final_body
    assert first_count["input"] == first_body["input"]
    assert first_count["instructions"] == first_body["instructions"]
    assert first_count["tools"] == first_body["tools"]
    assert first_count["text"] == first_body["text"]
    assert second_count["input"] == second_body["input"]
    assert "tools" not in second_count
    serialized = json.dumps(final_input, ensure_ascii=False)
    assert _fixture_string(fixture, "email") not in serialized
    assert "<SYNTHETIC_EMAIL>" in serialized


def test_openai_input_count_exceeds_approved_ceiling_before_generation() -> None:
    dataset = load_dataset()
    case = dataset.cases[0]
    client = FakeOpenAIClient(
        [_openai_final_response()],
        input_token_counts=[4001],
    )
    adapter = OpenAIAdapter(
        client,
        _candidate("openai", {"reasoning_effort": "low"}),
        max_input_tokens=4000,
    )

    with pytest.raises(EvaluationError, match="exceeding the approved per-request ceiling"):
        adapter.run_case(case, dataset, FixtureTools(dataset))

    assert len(client.responses.input_tokens.calls) == 1
    assert client.responses.calls == []


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


def test_model_output_schema_has_no_money_decision_fields() -> None:
    properties = cast(dict[str, dict[str, object]], OUTPUT_SCHEMA["properties"])
    assert set(properties) == {"action", "customer_message", "price_claim", "currency"}
    assert properties["action"]["enum"] == [
        "draft_message",
        "ask_clarification",
        "refuse",
        "no_action",
    ]
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
