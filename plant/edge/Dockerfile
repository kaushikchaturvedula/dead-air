FROM python:3.12-slim
WORKDIR /app
COPY edge.py .
# Cloud Run supplies $PORT; 8080 is the local default.
ENV PORT=8080
EXPOSE 8080
CMD ["python", "-u", "edge.py"]
