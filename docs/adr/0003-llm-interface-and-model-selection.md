# ADR-0003: One LLM interface; model chosen by evaluation

- Status: proposed (final after spike S2)
- Date: 2026-10-24

## Context

We need provider freedom and data sovereignty options (Azure OpenAI for GCC). The budget is near zero in the POC. We do not know yet if a small model (for example Gemini Flash-Lite) is good enough for tool calls, strict JSON, and Arabic.

## Decision

- All LLM calls go through one `LLMClient` port. Adapters: Gemini, OpenAI/Azure OpenAI. Others can be added.
- Candidates: Gemini Flash-Lite, Gemini Flash, GPT-6 Luna (existence, price and Azure availability are unverified: F-004 to F-006).
- Choose by a golden-set evaluation (about 30 cases): tool-call correctness, valid JSON, discount-cap respect, Arabic quality, cost per 1,000 messages.
- Embeddings run on CPU. The RTX 3070 is for local development only. The MVP never assumes a GPU server.
- Only synthetic data is sent to cloud LLMs. PII is masked before any outbound call.

## Alternatives

- Lock to one provider: rejected, bad for sovereignty.
- Local model only: possible for dev, weaker on Arabic and tools. Kept as an optional adapter.

## Consequences

- Eval work is needed before the final choice.
- Switching models is a config change plus a re-run of the golden set.
