.PHONY: install run test dashboard bigquery
install:
	pip install -e ".[dev]"
run:
	python -m fph.pipeline run
test:
	pytest -q
dashboard:
	streamlit run app/dashboard.py
bigquery:
	python -m fph.warehouse --to-bigquery
