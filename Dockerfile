FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/app-root/src

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .

CMD ["python", "collector.py"]