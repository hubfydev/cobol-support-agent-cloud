
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
HEALTHCHECK --interval=1m --timeout=3s CMD python -c "print('ok')"
CMD ["python", "app.py"]
