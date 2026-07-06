# Loop recipe catalog

A copy-paste library of **bounded-loop recipes across industries** — not just
software. Every recipe is a real loop-shaped task with a **real, mechanical
gate** (the verifier is the bottleneck, per loop engineering's own core idea),
a testable stopping condition, and a rung. Most run **keyless**; the ones that
need a tool or an API key say so plainly.

Every mainstream "agent loops" collection today is 100% software-dev (make
tests pass, get the build green). This catalog deliberately spans **finance,
legal, healthcare, compliance, data, content, marketing, support, and HR** —
because the loop pattern (goal → independent gate → repeat until it passes or a
bound trips) works anywhere there's a *checkable* definition of done.

## How to use a recipe

Two ways, both real:

1. **Straight into your agent** — copy the `/goal` line into Claude Code or
   Codex. It already names the gate command, so the agent has an objective
   stop condition instead of "looks done to me."
2. **As a bounded-loops loop** — `bl new <template>`, drop the recipe's `goal`
   into `PROMPT.md`, set `gate:` in `loop.yaml` to the recipe's gate (a
   `command`, `pytest`, `jsonschema`, `osv`, or `checkov` gate), and run
   `bl run` for the full nine-bound safety net + verdict ledger.

**Over 60 of these recipes are built out as fully runnable, validated example
loops** under [`../loops/`](../loops/), spanning a dozen industries — finance,
legal, retail, operations, enterprise/ERP, security, testing, content, research,
business, healthcare, and software (e.g. `citation-existence-check`,
`invoice-3way-match`, `gtin-checkdigit`, `idoc-xml-schema`, `secret-scan-keyless`,
`data-contract`). Each reaches `✓ [DONE] gate-passed` keyless — run `bl list` to
see them all. The remaining recipes are honest starting points: the gate tool is
real and named, but you supply the seed and wire it. **Nothing here claims to be
pre-built that isn't.**

## Legend

- **Keyless?** — `yes` = runs with only Python / nothing extra, fully offline.
  `needs-tool:<name>` = a CLI must be installed first (like the framework loops
  that need `pip install langgraph`). `needs-key:<what>` = an API key (only the
  LLM-judge recipes). `needs-tool:network` = makes real network calls.
- **Rung** — L1 (report / low autonomy), L2 (assisted, human approval before a
  side effect), L3 (unattended, but gated). Regulated-industry recipes skew
  L2/L3 on purpose: a passing gate should trigger human sign-off, not auto-merge.

---

## 1. Software Engineering · DevOps/SRE · Security-ops

Beyond the "make tests pass" basics every collection already has.

| Loop | Role | Goal (testable done-condition) | Gate | Keyless? | Rung |
|---|---|---|---|---|---|
| `dep-license-compliance` | license auditor | Every production dependency's license is in an approved allowlist | `command: license-checker --production --failOn 'GPL-3.0;AGPL-3.0;SSPL-1.0'` | needs-tool:license-checker | L1 |
| `circular-import-eliminator` | architecture cleaner | Zero circular import chains among own modules | `command: madge --circular --extensions js,ts src/` | needs-tool:madge | L1 |
| `dead-code-removal` | cleaner | Zero unused files/exports/deps | `command: npx knip --no-progress` | yes (npx) | L2 |
| `ts-prune-dead-exports` | cleaner | Zero unused TS exports outside an allowlist | `command: npx ts-prune -e` | yes (npx) | L1 |
| `type-coverage-ratchet` | type-safety eng | `tsc --noEmit --strict` exits 0 | `command: npx tsc --noEmit --strict` | yes (npx) | L2 |
| `openapi-contract-lint` | API-contract eng | OpenAPI spec has 0 Redocly lint errors | `command: npx @redocly/cli lint openapi.yaml` | yes (npx) | L2 |
| `openapi-breaking-change-gate` | API-contract eng | 0 breaking changes vs baseline spec | `command: oasdiff breaking base.yaml openapi.yaml` | needs-tool:oasdiff | L2 |
| `dockerfile-hardening` | container-sec eng | `hadolint Dockerfile` exits 0 | `command: hadolint Dockerfile` | needs-tool:hadolint | L1 |
| `container-image-cve-scan` | container-sec eng | 0 HIGH/CRITICAL CVEs in image | `command: trivy image --severity HIGH,CRITICAL --exit-code 1 <img>` | needs-tool:trivy | L2 |
| `k8s-manifest-validation` | IaC eng | Every manifest validates vs k8s schema | `command: kubeconform -strict -summary manifests/` | needs-tool:kubeconform | L1 |
| `iac-security-baseline` | IaC-sec eng | 0 failed Checkov checks at HIGH+ | `checkov` | needs-tool:checkov | L2 |
| `rust-supply-chain-audit` | supply-chain eng | 0 denied advisories/licenses/bans | `command: cargo deny check` | needs-tool:cargo-deny | L2 |
| `filesystem-vuln-scan` | supply-chain eng | 0 HIGH/CRITICAL fs vulns | `command: trivy fs --severity HIGH,CRITICAL --exit-code 1 .` | needs-tool:trivy | L2 |
| `secret-scanning-gate` | security-ops eng | 0 secrets in history + tree | `command: gitleaks detect --source . --exit-code 1` | needs-tool:gitleaks | L1 |
| `sbom-completeness` | supply-chain eng | CycloneDX SBOM validates, no missing version/license | `jsonschema` on generated `sbom.json` | yes | L2 |
| `db-migration-reversibility` | DB-migration eng | up→down→up restores identical schema hash | `pytest` round-trip schema-hash assertion | yes | L3 |
| `sql-migration-style-lint` | DB eng | 0 sqlfluff violations in migrations | `command: sqlfluff lint migrations/ --dialect postgres` | needs-tool:sqlfluff | L1 |
| `i18n-key-completeness` | i18n eng | Every base-locale key exists non-empty in all locales | `pytest` cross-locale key-set + non-empty check | yes | L1 |
| `flaky-test-quarantine` | test-reliability eng | 20 consecutive runs give identical pass/fail | `command: pytest --count=20 -x` (pytest-repeat) | needs-tool:pytest-repeat | L3 |
| `observability-alert-schema` | SRE | Every Prometheus rule file passes `promtool check rules` | `command: promtool check rules alerts/*.yml` | needs-tool:promtool | L1 |
| `llm-output-eval-gate` | AI-quality eng | promptfoo suite ≥ 90% pass | `command: promptfoo eval` | needs-key:LLM | L3 |

## 2. Data Engineering · Analytics · ML/AI

The keyless, offline gates (frictionless, jsonschema, pandera-via-pytest,
sqlfluff, pyarrow) are the strongest here.

| Loop | Role | Goal (testable done-condition) | Gate | Keyless? | Rung |
|---|---|---|---|---|---|
| `sql-lint-clean` | data eng | 0 sqlfluff violations in `models/` | `command: sqlfluff lint models/ --dialect ansi` | needs-tool:sqlfluff | L1 |
| `csv-schema-conformance` | data eng | Every CSV conforms to a table schema | `command: frictionless validate data/ --schema schema.json` | yes | L2 |
| `pandera-dataframe-contract` | ML eng | DataFrame passes a Pandera dtype/range/null contract | `pytest` wrapping `schema.validate` | yes | L2 |
| `json-api-contract-check` | backend eng | Captured API response conforms to JSON Schema | `command: check-jsonschema --schemafile api-schema.json response.json` | yes | L1 |
| `csv-lint-basic` | data eng | CSV passes structural lint (columns/quoting/encoding) | `command: csvlint data/export.csv` | yes | L1 |
| `notebook-reproducibility` | ML eng | Notebook executes top-to-bottom, no cell errors | `command: jupyter nbconvert --to notebook --execute` | yes | L2 |
| `parquet-schema-drift` | data eng | Output Parquet matches reference schema exactly | `pytest` pyarrow schema-equality | yes | L2 |
| `data-diff-regression` | data eng | 0 unexpected row diffs vs production | `command: data-diff prod.orders new.orders --pk id --fail-on-diff` | needs-tool:data-diff | L3 |
| `pii-scan-dataset` | data eng | 0 PII findings in export | `pytest` wrapping Presidio analyze | needs-tool:presidio | L3 |
| `great-expectations-checkpoint` | data eng | GE checkpoint succeeds for all expectations | `command: great_expectations checkpoint run <name>` | needs-tool:great-expectations | L2 |
| `datacontract-cli-test` | data eng | ODCS data-contract passes vs live dataset | `command: datacontract test datacontract.yaml` | needs-tool:datacontract-cli | L2 |
| `dbt-test-green` | analytics eng | Every `dbt test` passes | `command: dbt test` | needs-tool:dbt | L2 |
| `dbt-unit-tests` | analytics eng | All dbt unit tests pass | `command: dbt test --select test_type:unit` | needs-tool:dbt | L2 |
| `airflow-dag-integrity` | data eng | DAG imports cleanly, 0 parse errors | `pytest` using `airflow dags list-import-errors` | needs-tool:airflow | L2 |
| `feature-store-validation` | ML eng | Materialized features match Feast schema | `pytest` on `get_historical_features` | needs-tool:feast | L3 |
| `pipeline-io-contract` | data eng | Pandera input/output contracts hold across stages | `pytest` chained `schema.validate` | yes | L2 |
| `frictionless-datapackage` | data eng | Multi-file dataset passes schema + referential integrity | `command: frictionless validate datapackage.json` | yes | L2 |
| `experiment-config-conformance` | ML eng | Every training config validates vs JSON Schema | `command: check-jsonschema --schemafile config-schema.json configs/*.json` | yes | L1 |
| `retrieval-eval-threshold` | ML eng | recall@k on a labeled set clears a fixed threshold | `pytest` computing recall@k (no LLM judge) | yes | L3 |
| `promptfoo-regression` | ML eng | Prompt-regression suite 100% pass | `command: promptfoo eval` | needs-key:LLM | L3 |
| `deepeval-model-threshold` | ML eng | RAG/answer metrics clear DeepEval thresholds | `command: deepeval test run` | needs-key:LLM | L3 |

## 3. Content · Marketing · SEO · Technical Writing

Keyless mechanical linters (vale, cspell, proselint, alex, markdownlint) are
the strongest; the brand-voice judge is the one key-gated recipe.

| Loop | Role | Goal (testable done-condition) | Gate | Keyless? | Rung |
|---|---|---|---|---|---|
| `prose-style-lint` | editor | 0 Vale style violations vs a style guide | `command: vale <file>.md` | yes | L2 |
| `spellcheck-sweep` | editor | 0 cspell misspellings vs a project dictionary | `command: cspell "**/*.md"` | yes | L1 |
| `markdown-lint-fix` | editor | Full markdownlint compliance | `command: markdownlint-cli2 "**/*.md"` | yes | L1 |
| `inclusive-language-pass` | editor | 0 alex-flagged insensitive terms | `command: alex <file>.md` | needs-tool:alex | L1 |
| `passive-voice-cleanup` | editor | 0 write-good issues | `command: write-good <file>.md` | needs-tool:write-good | L2 |
| `proselint-pass` | editor | 0 proselint warnings | `command: proselint <file>.md` | needs-tool:proselint | L2 |
| `readability-grade-target` | editor | Flesch-Kincaid grade ≤ 8 | `command: textstat` check script | yes | L2 |
| `frontmatter-schema-fix` | editor | YAML frontmatter validates vs JSON Schema | `jsonschema` | yes | L1 |
| `jsonld-structured-data-fix` | editor | Embedded JSON-LD validates vs schema.org | `jsonschema` | yes | L2 |
| `meta-tag-seo-fix` | editor | Title/description lengths + presence pass SEO schema | `jsonschema` | yes | L1 |
| `heading-structure-fix` | editor | Single H1, no skipped levels (MD001/MD025) | `command: markdownlint-cli2 --config .heading-rules.json` | yes | L1 |
| `html-lint-docs-export` | editor | 0 htmlhint errors in rendered export | `command: htmlhint <file>.html` | needs-tool:htmlhint | L2 |
| `changelog-format-fix` | editor | Changelog matches Keep-a-Changelog structure | `jsonschema` over parsed sections | yes | L1 |
| `release-notes-version-check` | editor | Notes reference only real git tags | `pytest` vs `git tag` list | needs-tool:git | L3 |
| `terminology-consistency` | editor | Consistent product/term casing | `command: cspell --config .cspell-terms.json` | yes | L1 |
| `keyword-coverage-check` | editor | Target keyword + variants each appear | `pytest` keyword-coverage | yes | L2 |
| `markdown-alt-text` | editor | No missing image alt attributes in markdown | `pytest` alt-presence check | yes | L1 |
| `docs-code-drift-check` | editor | Doc snippets match real source signatures | `pytest` AST-diff | yes | L3 |
| `i18n-doc-parity` | editor | Translated file has parity with source (no missing keys) | `pytest` key-diff | yes | L2 |
| `max-sentence-length` | editor | No sentence exceeds N words | `pytest` sentence-length | yes | L1 |
| `brand-voice-judge` | editor | LLM-judge brand-voice eval above threshold | `command: promptfoo eval` | needs-key:LLM | L3 |

## 4. Regulated & Ops — Finance · Legal · Healthcare · Compliance · Support · HR

The mechanical core of "subjective" domains: structured output that must
conform to a schema, numbers that must reconcile, fields that must all be
present. Rungs skew L2/L3 — a passing gate here means *ready for human sign-off*.

| Loop | Role | Goal (testable done-condition) | Gate | Keyless? | Rung |
|---|---|---|---|---|---|
| `ledger-reconciliation` | accounting | Categorize txns; debits == credits exactly | `pytest` balance + category-enum assertion | yes | L2 |
| `expense-report-extraction` | AP clerk | Extract expense fields into schema-valid JSON | `jsonschema` | yes | L1 |
| `invoice-data-extraction` | AP clerk | Extract invoice fields; subtotal+tax == total | `jsonschema` + `pytest` math check | yes | L2 |
| `budget-variance-report` | FP&A | Per-line variance sums to reported total | `pytest` column-sum + `jsonschema` | yes | L2 |
| `regulatory-filing-validation` | compliance | Filing validates vs required XSD/JSON schema | `command: xmllint --schema filing.xsd` | yes | L3 |
| `aml-transaction-flagging` | AML analyst | Over-threshold txns flagged; flags == justifications | `jsonschema` + `pytest` count-match | yes | L2 |
| `contract-clause-extraction` | legal analyst | All required clause types present or marked absent | `jsonschema` | yes | L2 |
| `redaction-completeness` | redaction | No PII patterns remain in output | `pytest` regex `re.findall` == 0 | yes | L2 |
| `citation-format-lint` | legal | All citations conform to Bluebook regex | `pytest` regex | yes | L1 |
| `nda-field-completeness` | legal intake | NDA fields filled; no `{{placeholder}}` left | `jsonschema` + `pytest` placeholder check | yes | L1 |
| `clinical-note-completeness` | clinical docs | SOAP note complete; valid ICD-10 codes | `jsonschema` + `pytest` ICD-10 code-set | yes | L3 |
| `prior-auth-completeness` | prior-auth | Every payer-required field present, non-empty | `jsonschema` | yes | L2 |
| `fhir-resource-validation` | health IT | Output is a valid FHIR R4 resource | `command: FHIR validator_cli.jar` | needs-tool:fhir-validator | L2 |
| `medication-reconciliation` | med-rec | Every admission med accounted for with status+reason | `pytest` coverage assertion | yes | L3 |
| `discharge-summary-sections` | clinical docs | All required sections present, non-empty | `jsonschema` | yes | L2 |
| `pii-secret-scan-clean` | compliance | 0 secrets/PII in a document/dataset export | `command: gitleaks detect --no-git --exit-code 1` | needs-tool:gitleaks | L2 |
| `policy-as-code-compliance` | policy eng | OPA policy tests pass | `command: opa test policies/ -v` | needs-tool:opa | L2 |
| `infra-compliance-conftest` | compliance/devops | IaC passes org compliance policies | `command: conftest test main.tf -p policy/` | needs-tool:conftest | L2 |
| `vendor-risk-completeness` | vendor risk | Every control Q answered with evidence link | `jsonschema` | yes | L2 |
| `support-reply-compliance` | support | Reply matches structure schema + required disclaimer | `jsonschema` + `pytest` disclaimer regex | yes | L1 |
| `faq-kb-citation-check` | support KB | Every answer cites a KB id that exists in the index | `pytest` kb_id existence | yes | L1 |
| `job-posting-inclusive-language` | HR/recruiting | 0 alex-flagged biased terms in a job post | `command: alex job_post.md` | needs-tool:alex | L1 |
| `offer-letter-completeness` | HR | Offer fields filled; no leftover placeholders | `jsonschema` + `pytest` placeholder check | yes | L2 |
| `hr-policy-required-sections` | HR/policy | All legally-required sections present | `pytest` section-presence | yes | L2 |

---

*~86 recipes. The gate tools were verified real (2026); recipes whose tools
could not be confirmed were dropped rather than invented. Contributions welcome —
see [`../CONTRIBUTING.md`](../CONTRIBUTING.md); the bar is that every recipe names
a real gate and a testable done-condition, never "an LLM decides."*
