# Employee Attendance & Analytics API

## Project flow

The OpenAPI document defines endpoint behavior and rules R1-R10; `DATA_MODEL.md` defines MongoDB storage and sample shapes. `sample_seed.py` imports the sample JSON into the configured database. The FastAPI implementation is in `app/main.py`, and the code review/design rationale are in `REVIEW.md` and `DECISIONS.md`.

Implemented endpoints: readiness, employee create/list, attendance punch-in/punch-out/list/regularization, monthly employee and department analytics, late leaderboard, department daily trend, and explain plans. MongoDB owns uniqueness, pagination, and aggregation; API serializers convert BSON instants to epoch milliseconds.

## Local setup

Requires Python 3.11+, Git, and MongoDB 6.0+. Configure `MONGO_URI` and optional `MONGO_DB` in the environment or a local `.env` file.

```bash
python -m venv .venv
# Activate the virtual environment for your shell
pip install -r requirements.txt
cp .env.example .env            # if available; set MONGO_URI / MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

The seed data is illustrative only; tests should include empty months, employees without logs, overnight shifts, weekend records, and tie cases. MongoDB Atlas free tier or a local MongoDB instance works.

## Submit

Submit the source, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README. Do not commit `.env` files or credentials.
