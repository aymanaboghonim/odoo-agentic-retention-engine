# Facts register

Volatile facts (prices, model names, limits, versions) must be checked in official docs. Do not use a fact in a decision while its status is `unverified`.

Status: `unverified`, `verified`, `stale` (older than 90 days).

| ID | Fact | Value (as claimed) | Source URL | Checked on | Status |
| :-- | :-- | :-- | :-- | :-- | :-- |
| F-001 | Odoo version to use | Odoo 17 (from draft) | to find | - | unverified |
| F-002 | Odoo Community licence and demo data content (draft carts, customers) | LGPL, demo data has products and customers | to find | - | unverified |
| F-003 | Odoo built-in abandoned-cart reminder email | Exists in website_sale | to find | - | unverified |
| F-004 | Gemini free tier and paid tier 1 | Owner says: free tier allows Flash-Lite only, with strict TPM and RPM limits. Owner has 5 USD credit on paid tier 1, usable on any Gemini model. | to find | - | unverified |
| F-005 | GPT-6 Luna exists and price | Owner pasted: 0.10 input / 0.50 output per 1M tokens (assumed) | to find | - | unverified |
| F-006 | GPT-6 Luna available on Azure OpenAI, GCC regions | unknown | to find | - | unverified |
| F-007 | FastEmbed supports BAAI/bge-m3 | unknown (doubtful) | to find | - | unverified |
| F-008 | pgvector sparse vector support and Arabic text search config in Postgres | unknown | to find | - | unverified |
| F-009 | Meta WhatsApp Cloud API test number limits | up to 5 recipients (from memory) | to find | - | unverified |
| F-010 | Langfuse Cloud free tier limits and data region | unknown | to find | - | unverified |
| F-011 | Grafana Cloud free tier limits | unknown | to find | - | unverified |
| F-012 | Hetzner plan price for 4 vCPU / 8 GB | about 8-12 USD per month (draft) | to find | - | unverified |
| F-013 | Cloudflare Pages and R2 free-tier limits | unknown | to find | - | unverified |
| F-014 | Odoo and Odoo-with-demo-data RAM use | unknown | measure in S1 | - | unverified |
| F-015 | GitHub Projects built-in workflows: which exist (auto-add, item closed, item reopened, PR merged, PR opened to In progress), and plan limits on auto-add | my belief: all but 'PR opened' exist | to find | - | unverified |
| F-016 | GitHub branch protection or rulesets on a private repo with a free plan, and merge-method settings | my belief: not available on free private repos | to find | - | unverified |
| F-017 | CPython wheel availability in likely POC dependencies | PyPI metadata checked: ONNX Runtime 1.31.0 and Tokenizers 0.23.3 have CPython 3.14 wheels, but no CPython 3.15 wheels. Snapshot only; recheck against selected dependency versions. | https://pypi.org/project/onnxruntime/; https://pypi.org/project/tokenizers/ | 2026-10-24 | verified |
| F-018 | CI Python development tool versions | uv 0.13.5, pre-commit 4.7.0, pytest 9.1.1, Ruff 0.17.2, mypy 2.4.0 | https://pypi.org/pypi/uv/json; https://pypi.org/pypi/pre-commit/json; https://pypi.org/pypi/pytest/json; https://pypi.org/pypi/ruff/json; https://pypi.org/pypi/mypy/json | 2026-10-24 | verified |
| F-019 | CI hook revisions and container images | Revisions/images are pinned in `.pre-commit-config.yaml` and scripts; resolved from official Git tags and Docker image digests on 2026-10-24. | https://github.com/pre-commit/pre-commit-hooks/tags; https://github.com/DavidAnson/markdownlint-cli2/tags; https://github.com/lycheeverse/lychee/tags; https://github.com/gitleaks/gitleaks/tags; https://github.com/rhysd/actionlint/tags; https://github.com/actions/checkout/tags; https://github.com/astral-sh/setup-uv/tags | 2026-10-24 | verified |
