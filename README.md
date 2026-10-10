# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB (real environment variables take priority)
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$densify`, `$setWindowFields`). Local: `docker run -p 27017:27017 mongo:7`.
Indexes are created automatically at startup. Interactive docs: `http://localhost:8000/docs`.

## Notes
- All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
- "Exceeds 92 days" for the trend is read as more than 92 inclusive rows (a 93-day range returns 422).
- The trend moving average is the mean of the already-rounded (4 dp) daily rates.
- Analytics read the stored derived fields and never recompute them from punch times.
- See `REVIEW.md` (defects found in the starter) and `DECISIONS.md`.