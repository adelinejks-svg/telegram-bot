FROM python:3.12-slim
WORKDIR /app
COPY secretary-requirements.txt .
RUN pip install --no-cache-dir -r secretary-requirements.txt
COPY secretary.py .
CMD ["python", "-u", "secretary.py"]
