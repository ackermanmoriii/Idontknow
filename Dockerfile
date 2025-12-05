# Use a lightweight Python image
FROM python:3.10-slim

# 1. Install System Dependencies (FFmpeg is critical here)
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# 2. Set working directory
WORKDIR /app

# 3. Copy requirements and install Python libs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy the rest of the app code
COPY . .

# 5. Run the app using Gunicorn
CMD gunicorn app:app --bind 0.0.0.0:$PORT
