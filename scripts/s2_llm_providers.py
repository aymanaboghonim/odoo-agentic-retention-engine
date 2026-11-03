from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast
from urllib.parse import quote

from scripts.s2_llm_eval import (
    Candidate,
    CartContextTools,
    EvaluationCase,
    EvaluationError,
    GoldenDataset,
    JsonValue,
    ModelResult,
    Record,
    ToolCall,
    Usage,
    mask_sensitive_data,
)
from scripts.s2_llm_prompts import (
    DEFAULT_PROMPT_VARIANT,
    OUTPUT_SCHEMA,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_PARAMETERS,
    PromptVariant,
    system_prompt,
)


class LLMClient(Protocol):
    def run_case(
        self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
    ) -> ModelResult: ...


class GeminiAdapter:
    def __init__(
        self,
        client: Any,
        candidate: Candidate,
        *,
        max_output_tokens: int | None = None,
        max_input_tokens: int | None = None,
        api_key: str | None = None,
        count_tokens_transport: Callable[[str, dict[str, object]], int] | None = None,
        prompt_variant: PromptVariant = DEFAULT_PROMPT_VARIANT,
        poll_seconds: int = 30,
        timeout_seconds: int = 86_400,
    ) -> None:
        if candidate.provider != "google":
            raise ValueError("GeminiAdapter requires a Google candidate")
        if max_input_tokens is not None and max_input_tokens < 1:
            raise ValueError("max_input_tokens must be positive")
        self.client = client
        self.candidate = candidate
        self.max_output_tokens = max_output_tokens
        self.max_input_tokens = max_input_tokens
        self.api_key = api_key
        self.count_tokens_transport = count_tokens_transport
        self.prompt_variant = prompt_variant
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def run_case(
        self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
    ) -> ModelResult:
        prompt = _case_prompt(case, dataset)
        first_request = self._request([_user_content(prompt)])
        self._enforce_input_limit(first_request)
        first_response = self.client.models.generate_content(
            model=self.candidate.model,
            contents=first_request["contents"],
            config=first_request["config"],
        )
        first = self._turn(case, first_response, dataset)
        if not _valid_single_cart_call(case, first.tool_calls, dataset):
            return first

        call = first.tool_calls[0]
        tool_result = tools.get_cart_context(case, _cart_id_argument(call))
        model_content = _gemini_model_content(first_response)
        followup_contents: list[object] = [
            _user_content(prompt),
            model_content,
            {
                "role": "user",
                "parts": [{"function_response": {"name": call.name, "response": tool_result}}],
            },
        ]
        followup_request: dict[str, object] = {
            "contents": followup_contents,
            "config": self._generation_config(allow_tool_calls=False),
        }
        self._enforce_input_limit(followup_request)
        second_response = self.client.models.generate_content(
            model=self.candidate.model,
            contents=followup_request["contents"],
            config=followup_request["config"],
        )
        second = self._turn(case, second_response, dataset)
        return _combine_results(case.case_id, first, second)

    def _generation_config(self, *, allow_tool_calls: bool = True) -> dict[str, object]:
        config: dict[str, object] = {
            "system_instruction": system_prompt("google", self.prompt_variant),
            "response_mime_type": "application/json",
            "response_json_schema": OUTPUT_SCHEMA,
            "thinking_config": {"thinking_level": self.candidate.configuration["thinking_level"]},
        }
        if allow_tool_calls:
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "parameters_json_schema": TOOL_PARAMETERS,
                        }
                    ]
                }
            ]
            config["tool_config"] = {
                "function_calling_config": {
                    "mode": "AUTO",
                }
            }
        if self.max_output_tokens is not None:
            config["max_output_tokens"] = self.max_output_tokens
        return config

    def _request(self, contents: list[dict[str, object]]) -> dict[str, object]:
        return {"contents": contents, "config": self._generation_config()}

    def _enforce_input_limit(self, request: dict[str, object]) -> None:
        if self.max_input_tokens is None:
            return
        input_tokens = self._count_input_tokens(request)
        if input_tokens > self.max_input_tokens:
            raise EvaluationError(
                f"Gemini request has {input_tokens} input tokens, exceeding the approved "
                f"per-request ceiling of {self.max_input_tokens}"
            )

    def _count_input_tokens(self, request: dict[str, object]) -> int:
        contents = request.get("contents")
        config = request.get("config")
        if not isinstance(contents, list) or not isinstance(config, dict):
            raise EvaluationError("Gemini request has an invalid input shape for countTokens")
        if self.count_tokens_transport is not None:
            return self.count_tokens_transport(self.candidate.model, request)
        if self.api_key is None:
            raise EvaluationError("Gemini countTokens requires an API key")
        return _count_gemini_request_tokens(self.api_key, self.candidate.model, request)

    def _turn(self, case: EvaluationCase, response: object, dataset: GoldenDataset) -> ModelResult:
        candidate = _first(_field(response, "candidates"))
        content = _field(candidate, "content")
        parts = _field(content, "parts")
        tool_calls = tuple(
            _gemini_tool_call(part)
            for part in _items(parts)
            if _field(part, "function_call") is not None
        )
        raw_text = _gemini_text(response, parts)
        output, parse_error = ({}, None) if tool_calls else _parse_output(raw_text)
        finish_reason = _enum_value(_field(candidate, "finish_reason"))
        if finish_reason.endswith("MAX_TOKENS"):
            parse_error = "Gemini response reached its output-token limit"
        return ModelResult(
            case_id=case.case_id,
            output=output,
            tool_calls=tool_calls,
            usage=_gemini_usage(response),
            raw_output_text=_mask_text(raw_text, case, dataset),
            parse_error=parse_error,
        )


class OpenAIAdapter:
    def __init__(
        self,
        client: Any,
        candidate: Candidate,
        *,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        prompt_variant: PromptVariant = DEFAULT_PROMPT_VARIANT,
        poll_seconds: int = 30,
        timeout_seconds: int = 86_400,
    ) -> None:
        if candidate.provider != "openai":
            raise ValueError("OpenAIAdapter requires an OpenAI candidate")
        self.client = client
        self.candidate = candidate
        self.max_input_tokens = max_input_tokens
        self.prompt_variant = prompt_variant
        self.max_output_tokens = max_output_tokens
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def run_case(
        self, case: EvaluationCase, dataset: GoldenDataset, tools: CartContextTools
    ) -> ModelResult:
        prompt = _case_prompt(case, dataset)
        input_items: list[object] = [_openai_user_input(prompt)]
        first_body = self._body(input_items)
        self._enforce_input_limit(first_body)
        first_response = self.client.responses.create(**first_body)
        first = self._turn(case, first_response, dataset)
        if not _valid_single_cart_call(case, first.tool_calls, dataset):
            return first

        call = first.tool_calls[0]
        raw_call = _first(_openai_function_items(_field(first_response, "output")))
        call_id = _field(raw_call, "call_id")
        if not isinstance(call_id, str) or not call_id:
            raise EvaluationError("OpenAI function call returned no call_id")
        tool_result = tools.get_cart_context(case, _cart_id_argument(call))
        input_items.extend(_items(_field(first_response, "output")))
        input_items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(tool_result, ensure_ascii=False),
            }
        )
        second_body = self._body(input_items, allow_tool_calls=False)
        self._enforce_input_limit(second_body)
        second_response = self.client.responses.create(**second_body)
        second = self._turn(case, second_response, dataset)
        return _combine_results(case.case_id, first, second)

    def _enforce_input_limit(self, body: dict[str, object]) -> None:
        if self.max_input_tokens is None:
            return
        counter = _field(self.client.responses, "input_tokens")
        count = _field(counter, "count")
        if not callable(count):
            raise EvaluationError("OpenAI input-token counting is unavailable")
        count_parameters = {
            key: body[key]
            for key in (
                "model",
                "input",
                "instructions",
                "parallel_tool_calls",
                "reasoning",
                "text",
                "tool_choice",
                "tools",
                "truncation",
            )
            if key in body
        }
        response = count(**count_parameters)
        input_tokens = _required_token_count(_field(response, "input_tokens"), "OpenAI input")
        if input_tokens > self.max_input_tokens:
            raise EvaluationError(
                f"OpenAI request has {input_tokens} input tokens, exceeding the approved "
                f"per-request ceiling of {self.max_input_tokens}"
            )

    def _body(
        self, input_items: list[object], *, allow_tool_calls: bool = True
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self.candidate.model,
            "instructions": system_prompt("openai", self.prompt_variant),
            "input": input_items,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "s2_retention_output",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                }
            },
            "reasoning": {"effort": self.candidate.configuration["reasoning_effort"]},
            "truncation": "disabled",
            "store": False,
        }
        if allow_tool_calls:
            body["tools"] = [
                {
                    "type": "function",
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "parameters": TOOL_PARAMETERS,
                    "strict": True,
                }
            ]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = False
        if self.max_output_tokens is not None:
            body["max_output_tokens"] = self.max_output_tokens
        return body

    def _turn(self, case: EvaluationCase, response: object, dataset: GoldenDataset) -> ModelResult:
        tool_calls = tuple(
            _openai_tool_call(item) for item in _openai_function_items(_field(response, "output"))
        )
        raw_text = _openai_text(response)
        output, parse_error = ({}, None) if tool_calls else _parse_output(raw_text)
        if _field(response, "status") == "incomplete":
            details = _field(response, "incomplete_details")
            reason = _field(details, "reason")
            parse_error = f"OpenAI response is incomplete: {reason}"
        return ModelResult(
            case_id=case.case_id,
            output=output,
            tool_calls=tool_calls,
            usage=_openai_usage(response),
            raw_output_text=_mask_text(raw_text, case, dataset),
            parse_error=parse_error,
        )


def _gemini_count_tokens_body(model: str, request: dict[str, object]) -> dict[str, object]:
    contents = request.get("contents")
    config = request.get("config")
    if not isinstance(contents, list) or not isinstance(config, dict):
        raise EvaluationError("Gemini request has an invalid input shape for countTokens")
    raw_tools = config.get("tools")
    if raw_tools is not None and not isinstance(raw_tools, list):
        raise EvaluationError("Gemini request has invalid tool declarations for countTokens")
    from google.genai import types  # type: ignore[import-not-found]

    try:
        serialized_contents = [
            types.Content.model_validate(content).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            for content in contents
        ]
        system_instruction = config.get("system_instruction")
        if isinstance(system_instruction, str):
            serialized_system_instruction = types.Content(
                parts=[types.Part(text=system_instruction)]
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
        elif system_instruction is not None:
            serialized_system_instruction = types.Content.model_validate(
                system_instruction
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
        else:
            serialized_system_instruction = None

        serialized_tools = (
            [
                types.Tool.model_validate(tool).model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
                for tool in raw_tools
            ]
            if isinstance(raw_tools, list)
            else None
        )
        raw_tool_config = config.get("tool_config")
        serialized_tool_config = (
            types.ToolConfig.model_validate(raw_tool_config).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            if raw_tool_config is not None
            else None
        )
        generation_fields = {
            key: value
            for key, value in config.items()
            if key not in {"system_instruction", "tools", "tool_config"}
        }
        serialized_generation_config = types.GenerateContentConfig.model_validate(
            generation_fields
        ).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
    except (TypeError, ValueError) as error:
        raise EvaluationError("Gemini countTokens request does not match the SDK schema") from error
    generate_content_request: dict[str, object] = {
        "model": model,
        "contents": serialized_contents,
    }
    if serialized_system_instruction is not None:
        generate_content_request["systemInstruction"] = serialized_system_instruction
    if serialized_tools is not None:
        generate_content_request["tools"] = serialized_tools
    if serialized_tool_config is not None:
        generate_content_request["toolConfig"] = serialized_tool_config
    if serialized_generation_config:
        generate_content_request["generationConfig"] = serialized_generation_config
    return {"generateContentRequest": generate_content_request}


def _count_gemini_request_tokens(api_key: str, model: str, request: dict[str, object]) -> int:
    model_resource = model if model.startswith("models/") else f"models/{model}"
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"{quote(model_resource, safe='/')}:countTokens"
    )
    body = json.dumps(
        _gemini_count_tokens_body(model_resource, request),
        ensure_ascii=False,
    ).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            response_body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise EvaluationError(f"Gemini countTokens returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise EvaluationError("Gemini countTokens transport failed") from error
    except TimeoutError as error:
        raise EvaluationError("Gemini countTokens request timed out") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError("Gemini countTokens returned invalid JSON") from error
    if not isinstance(response_body, dict):
        raise EvaluationError("Gemini countTokens response has an invalid shape")
    return _required_token_count(response_body.get("totalTokens"), "Gemini countTokens totalTokens")


def create_live_client(
    candidate: Candidate,
    *,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    prompt_variant: PromptVariant = DEFAULT_PROMPT_VARIANT,
) -> LLMClient:
    if candidate.provider == "google":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EvaluationError("Set GEMINI_API_KEY in the process environment")
        from google import genai  # type: ignore[import-not-found]

        gemini_client = genai.Client(
            api_key=api_key,
            http_options={"retry_options": {"attempts": 1}},
        )
        return GeminiAdapter(
            gemini_client,
            candidate,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            api_key=api_key,
            prompt_variant=prompt_variant,
        )
    if candidate.provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EvaluationError("Set OPENAI_API_KEY in the process environment")
        from openai import OpenAI  # type: ignore[import-not-found]

        openai_client = OpenAI(api_key=api_key, max_retries=0)
        return OpenAIAdapter(
            openai_client,
            candidate,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            prompt_variant=prompt_variant,
        )
    raise EvaluationError(f"Unsupported provider {candidate.provider!r}")


def _case_prompt(case: EvaluationCase, dataset: GoldenDataset) -> str:
    fixture = dataset.fixtures[case.fixture_id]
    prompt = mask_sensitive_data(case.prompt, fixture)
    if not isinstance(prompt, str):
        raise EvaluationError(f"Masked prompt for {case.case_id} is not text")
    return prompt


def _user_content(prompt: str) -> dict[str, object]:
    return {"role": "user", "parts": [{"text": prompt}]}


def _openai_user_input(prompt: str) -> dict[str, object]:
    return {"role": "user", "content": [{"type": "input_text", "text": prompt}]}


def _valid_single_cart_call(
    case: EvaluationCase, calls: tuple[ToolCall, ...], dataset: GoldenDataset
) -> bool:
    if not case.requires_cart_tool or len(calls) != 1:
        return False
    fixture = dataset.fixtures[case.fixture_id]
    call = calls[0]
    return call.name == "get_cart_context" and call.arguments == {"cart_id": fixture.get("cart_id")}


def _cart_id_argument(call: ToolCall) -> str:
    cart_id = call.arguments.get("cart_id")
    if not isinstance(cart_id, str):
        raise EvaluationError("Cart tool call has a non-string cart_id")
    return cart_id


def _gemini_model_content(response: object) -> object:
    candidate = _first(_field(response, "candidates"))
    content = _field(candidate, "content")
    if content is None:
        raise EvaluationError("Gemini tool call response has no model content")
    return content


def _gemini_tool_call(part: object) -> ToolCall:
    function_call = _field(part, "function_call")
    name = _field(function_call, "name")
    arguments = _field(function_call, "args")
    if not isinstance(name, str) or not name:
        raise EvaluationError("Gemini returned a function call without a name")
    parsed_arguments: Record
    if isinstance(arguments, dict):
        parsed_arguments = _provider_record(
            cast(object, arguments), f"Gemini function arguments for {name}"
        )
    else:
        parsed_arguments = {"_invalid_arguments": str(arguments)}
    return ToolCall(name=name, arguments=parsed_arguments)


def _gemini_text(response: object, parts: object) -> str | None:
    text = _field(response, "text")
    if isinstance(text, str) and text:
        return text
    strings = [value for part in _items(parts) if isinstance((value := _field(part, "text")), str)]
    return "\n".join(strings) if strings else None


def _gemini_usage(response: object) -> Usage:
    usage = _field(response, "usage_metadata")
    component_fields = (
        "prompt_token_count",
        "tool_use_prompt_token_count",
        "thoughts_token_count",
        "candidates_token_count",
    )
    components = {name: _optional_token_count(_field(usage, name)) for name in component_fields}
    total = _optional_token_count(_field(usage, "total_token_count"))
    missing = [name for name, value in components.items() if value is None]
    if missing and total is not None:
        known_total = sum(value for value in components.values() if value is not None)
        derived = total - known_total
        if derived < 0:
            raise EvaluationError(
                "Gemini total_token_count implies a negative missing usage component"
            )
        if len(missing) == 1:
            components[missing[0]] = derived
        elif derived == 0:
            for name in missing:
                components[name] = 0
        else:
            raise EvaluationError(
                "Gemini total_token_count cannot allocate a nonzero residual across "
                "multiple missing usage components"
            )
    elif missing:
        raise EvaluationError(
            "Gemini usage metadata is missing exact component count(s): " + ", ".join(missing)
        )

    prompt_tokens = _required_token_count(
        components["prompt_token_count"], "Gemini prompt_token_count"
    )
    tool_use_prompt_tokens = _required_token_count(
        components["tool_use_prompt_token_count"], "Gemini tool_use_prompt_token_count"
    )
    reasoning = _required_token_count(
        components["thoughts_token_count"], "Gemini thoughts_token_count"
    )
    candidate_tokens = _required_token_count(
        components["candidates_token_count"], "Gemini candidates_token_count"
    )
    component_total = prompt_tokens + tool_use_prompt_tokens + reasoning + candidate_tokens
    if total is not None and component_total != total:
        raise EvaluationError("Gemini total_token_count does not equal its usage components")
    cached = _optional_token_count(_field(usage, "cached_content_token_count"))
    return Usage(
        input_tokens=prompt_tokens + tool_use_prompt_tokens,
        output_tokens=candidate_tokens + reasoning,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        tool_use_prompt_tokens=tool_use_prompt_tokens,
    )


def _openai_function_items(output: object) -> list[object]:
    return [item for item in _items(output) if _field(item, "type") == "function_call"]


def _openai_tool_call(item: object) -> ToolCall:
    name = _field(item, "name")
    arguments = _field(item, "arguments")
    if not isinstance(name, str) or not name:
        raise EvaluationError("OpenAI returned a function call without a name")
    record: Record
    if isinstance(arguments, dict):
        record = _provider_record(cast(object, arguments), f"OpenAI function arguments for {name}")
    elif isinstance(arguments, str):
        try:
            record = _provider_record(
                json.loads(arguments), f"OpenAI function arguments for {name}"
            )
        except json.JSONDecodeError:
            record = {"_invalid_arguments": arguments}
    else:
        record = {"_invalid_arguments": str(arguments)}
    return ToolCall(name, record)


def _openai_text(response: object) -> str | None:
    output_text = _field(response, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    strings: list[str] = []
    for item in _items(_field(response, "output")):
        if _field(item, "type") != "message":
            continue
        for content in _items(_field(item, "content")):
            text = _field(content, "text")
            if _field(content, "type") == "output_text" and isinstance(text, str):
                strings.append(text)
    return "\n".join(strings) if strings else None


def _openai_usage(response: object) -> Usage:
    usage = _field(response, "usage")
    input_tokens = _required_token_count(_field(usage, "input_tokens"), "OpenAI input_tokens")
    output_tokens = _required_token_count(_field(usage, "output_tokens"), "OpenAI output_tokens")
    input_details = _field(usage, "input_tokens_details")
    output_details = _field(usage, "output_tokens_details")
    cached = _optional_token_count(_field(input_details, "cached_tokens"))
    reasoning = _optional_token_count(_field(output_details, "reasoning_tokens"))
    return Usage(input_tokens, output_tokens, cached, reasoning)


def _parse_output(text: str | None) -> tuple[Record, str | None]:
    if text is None:
        return {}, "provider returned no structured text"
    try:
        output = json.loads(text)
    except json.JSONDecodeError as error:
        return {}, f"invalid JSON at character {error.pos}"
    try:
        return _provider_record(output, "provider structured output"), None
    except EvaluationError:
        return {}, "structured output is not an object with string keys"


def _mask_text(text: str | None, case: EvaluationCase, dataset: GoldenDataset) -> str | None:
    if text is None:
        return None
    result = mask_sensitive_data(text, dataset.fixtures[case.fixture_id])
    return result if isinstance(result, str) else None


def _combine_results(case_id: str, first: ModelResult, second: ModelResult) -> ModelResult:
    return ModelResult(
        case_id=case_id,
        output=second.output,
        tool_calls=first.tool_calls + second.tool_calls,
        usage=_sum_usage(first.usage, second.usage),
        raw_output_text=second.raw_output_text,
        parse_error=first.parse_error or second.parse_error,
    )


def _sum_usage(first: Usage, second: Usage) -> Usage:
    cached = (
        first.cached_input_tokens + second.cached_input_tokens
        if first.cached_input_tokens is not None and second.cached_input_tokens is not None
        else None
    )
    reasoning = (
        first.reasoning_tokens + second.reasoning_tokens
        if first.reasoning_tokens is not None and second.reasoning_tokens is not None
        else None
    )
    tool_use_prompt = (
        first.tool_use_prompt_tokens + second.tool_use_prompt_tokens
        if first.tool_use_prompt_tokens is not None and second.tool_use_prompt_tokens is not None
        else None
    )
    return Usage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        tool_use_prompt_tokens=tool_use_prompt,
    )


def _required_token_count(value: object, name: str) -> int:
    count = _optional_token_count(value)
    if count is None:
        raise EvaluationError(f"Provider response is missing {name}")
    return count


def _optional_token_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationError(f"Provider returned an invalid token count: {value!r}")
    return value


def _items(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _first(value: object) -> object | None:
    items = _items(value)
    return items[0] if items else None


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return cast(Mapping[str, object], value).get(name)
    return getattr(value, name, None)


def _enum_value(value: object) -> str:
    name = _field(value, "name")
    if isinstance(name, str):
        return name
    raw = _field(value, "value")
    if isinstance(raw, str):
        return raw
    return str(value)


def _provider_record(value: object, context: str) -> Record:
    if not isinstance(value, dict):
        raise EvaluationError(f"{context} must be a JSON object")
    record: Record = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise EvaluationError(f"{context} must have string keys")
        record[key] = _provider_json_value(item, context)
    return record


def _provider_json_value(value: object, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        items = cast(list[object], value)
        return [_provider_json_value(item, context) for item in items]
    if isinstance(value, dict):
        return _provider_record(cast(object, value), context)
    raise EvaluationError(f"{context} contains a non-JSON value")
