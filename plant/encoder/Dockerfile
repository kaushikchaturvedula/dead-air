FROM debian:bookworm-slim

# ffmpeg for the ladder, dejavu for the timecode burn-in (drawtext needs a real
# font file), python3 for the supervisor/exporter. No pip installs -- the
# exporter is stdlib only.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY run_encoder.py .

EXPOSE 9103
CMD ["python3", "-u", "run_encoder.py"]
