FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md main.py ./
COPY pyneolink ./pyneolink

RUN pip install --no-cache-dir -e ".[aes]"

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
