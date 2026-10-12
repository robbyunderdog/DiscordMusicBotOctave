FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY Octave.py .

ENV FFMPEG_PATH=ffmpeg

RUN useradd --create-home --shell /usr/sbin/nologin octave
USER octave

CMD ["python", "Octave.py"]
