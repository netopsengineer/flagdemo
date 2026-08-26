FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.lock /app/app/requirements.lock
RUN pip install --no-cache-dir -r /app/app/requirements.lock

COPY app /app/app
COPY bench /app/bench
COPY flags /app/flags
COPY flagd /app/flagd

# One worker is intentional. The experiment measures one event loop and one
# provider instance. Scale-out and host-isolation runs should use multiple
# independently measured app instances rather than hiding blocking with forks.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
