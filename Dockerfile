FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static/ ./static/
COPY data/ ./data/
ENV PORT=5000
EXPOSE 5000
# Production server: gunicorn (2 workers x 4 threads). Tables are created
# on boot via init_db() in app.py. DATABASE_URL selects Postgres;
# without it the app uses SQLite.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 app:app"]
