FROM python:3.12

WORKDIR /app

COPY requirements.txt requirements.txt
RUN apt-get update && \
    apt-get install -y ocrmypdf tesseract-ocr-all && \
    rm -rf /var/lib/apt/lists/* && \
    pip install -r requirements.txt

COPY lib/ .

ENTRYPOINT ["python3", "-u", "main.py"]