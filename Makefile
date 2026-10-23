.DEFAULT_GOAL := help
TARGET ?= dev

.PHONY: help validate deploy deploy-prod test typegen-check setup profiler wave-plan dry-run convert validate-stage rollback cleanup e2e-all

# Convert selector (schema is the safe default). Streaming waves are excluded from a
# schema/bucket scope unless INCLUDE_STREAMING=true (or BUCKET=streaming).
# NOTE: `databricks bundle run --params` is a comma-separated key=value list, so a value
# cannot itself contain commas. WAVES therefore accepts a SINGLE wave id from the CLI; to
# convert several explicit waves at once, set the job's `wave_ids` parameter (comma-
# separated) from the Jobs UI, or run this target once per wave.
SCHEMA ?=
BUCKET ?=
WAVES ?=
INCLUDE_STREAMING ?= false

# E2E conductor selectors (backend/.e2e/e2e_all.py). PROFILE is REQUIRED — never auto-selected.
# The default is a non-mutating read-mostly pass (L2 runs as a dry-run); set CONFIRM_EXECUTE=true
# for the full mutating backbone. THROUGH stops early (e.g. L3); SKIP is space-separated (e.g. "L4 L5").
PROFILE ?=
THROUGH ?=
SKIP ?=
CONFIRM_EXECUTE ?= false

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

validate: ## Validate the bundle config (TARGET=dev|prod)
	databricks bundle validate -t $(TARGET)

deploy: ## Deploy all jobs to dev
	databricks bundle deploy -t dev

deploy-prod: ## Deploy all jobs to prod (requires prod_host + prod_run_as_sp)
	databricks bundle deploy -t prod

test: ## Run the pure-logic unit tests (no cluster)
	python -m pytest backend/src/tests/ -q

typegen-check: ## Fail if committed app query types drift from the .sql (run before pushing query changes; needs a warehouse + USE CATALOG)
	cd app && npm run typegen -- --wait
	git diff --exit-code app/shared/appkit-types/


setup: ## Run stage 99 setup (install-time: Genie space + App SP grants; order-independent with profiler)
	databricks bundle run job_99_setup -t $(TARGET)

profiler: ## Run stage 00 profiler
	databricks bundle run job_00_profiler -t $(TARGET)

wave-plan: ## Run stage 02 wave planner
	databricks bundle run job_02_wave_planner -t $(TARGET)

dry-run: ## Run stage 03 dry run
	databricks bundle run job_03_dry_run -t $(TARGET)

convert: ## Run stage 04 convert (SCHEMA=cat.sch | BUCKET=streaming | WAVES=wave_03)
	@if [ -z "$(SCHEMA)$(BUCKET)$(WAVES)" ]; then \
		echo "ERROR: a selector is required (no implicit convert-all)."; \
		echo "  make convert SCHEMA=<catalog.schema>   # safe default (streaming excluded)"; \
		echo "  make convert BUCKET=streaming          # isolated streaming waves"; \
		echo "  make convert WAVES=wave_03             # one explicit wave (see NOTE above)"; \
		exit 1; \
	fi
	databricks bundle run job_04_convert -t $(TARGET) --params \
		target_schema=$(SCHEMA),bucket=$(BUCKET),wave_ids=$(WAVES),include_streaming=$(INCLUDE_STREAMING),confirm_compaction_paused=true

validate-stage: ## Run stage 05 validate
	databricks bundle run job_05_validate -t $(TARGET)

rollback: ## Run stage 06 rollback (PLAN_RUN_ID=<uuid> WAVE=wave_03 REASON="why") — within 14 days
	@if [ -z "$(PLAN_RUN_ID)" ] || [ -z "$(WAVE)" ] || [ -z "$(REASON)" ]; then \
		echo "ERROR: rollback needs PLAN_RUN_ID, WAVE, and REASON."; exit 1; \
	fi
	databricks bundle run job_06_rollback -t $(TARGET) --params \
		plan_run_id=$(PLAN_RUN_ID),wave_id=$(WAVE),reason="$(REASON)"

cleanup: ## Run stage 07 post-convert cleanup
	databricks bundle run job_07_post_convert_cleanup -t $(TARGET)

e2e-all: ## Run the full e2e conductor L0->L5 (PROFILE=<cli-profile> [THROUGH=L3] [SKIP="L4 L5"] [CONFIRM_EXECUTE=true])
	@if [ -z "$(PROFILE)" ]; then \
		echo "ERROR: PROFILE is required (never auto-selected)."; \
		echo "  make e2e-all PROFILE=<cli-profile>              # read-mostly (L2 dry-run)"; \
		echo "  make e2e-all PROFILE=<p> THROUGH=L3             # L0..L3 only"; \
		echo "  make e2e-all PROFILE=<p> SKIP=\"L4\"              # headless (no UI auth)"; \
		echo "  make e2e-all PROFILE=<p> CONFIRM_EXECUTE=true   # full mutating backbone"; \
		exit 1; \
	fi
	cd backend && python .e2e/e2e_all.py --profile $(PROFILE) \
		$(if $(THROUGH),--through $(THROUGH),) \
		$(foreach s,$(SKIP),--skip $(s)) \
		$(if $(filter true,$(CONFIRM_EXECUTE)),--confirm-execute,)
