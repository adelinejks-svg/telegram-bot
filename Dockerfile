FROM python:3.12-slim
WORKDIR /app
COPY secretary-requirements.txt .
RUN pip install --no-cache-dir -r secretary-requirements.txt
COPY secretary.py .
COPY secretary-smoke-test.py .
RUN python -m py_compile secretary.py && python secretary-smoke-test.py
CMD ["python", "-u", "secretary.py"]
