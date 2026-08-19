# Build context is ./plant so the shared tracer can be copied in.
FROM python:3.12-slim
WORKDIR /app
COPY shared/deadair_trace.py .
COPY viewers/viewers.py .
EXPOSE 9104
CMD ["python", "-u", "viewers.py"]
