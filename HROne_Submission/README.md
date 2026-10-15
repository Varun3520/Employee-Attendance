# Employee Attendance & Analytics API

FastAPI application backed by MongoDB. Requires Python 3.11+ and MongoDB.

## Run

```bash
python -m pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your environment or a local `.env` file, then start the API:

```bash
python -m uvicorn app.main:app --port 8000
```

API documentation: `http://127.0.0.1:8000/docs`  
Health check: `GET http://127.0.0.1:8000/health`

Do not commit database credentials or `.env` files. The application creates its indexes at startup; `/health` checks MongoDB connectivity.
