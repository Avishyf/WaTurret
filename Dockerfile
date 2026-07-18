# Use a lightweight python image
FROM python:3.10-slim

# Install minimal system dependencies required for OpenCV, ffmpeg, and networking
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements.txt to install dependencies
COPY requirements.txt .

# Pre-install CPU version of PyTorch and torchvision to keep the image size small
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install the rest of the python requirements
RUN pip install --no-cache-dir -r requirements.txt

# Copy the stream detector code and the YOLO model weights
COPY WaTurrent/ ./

# Expose the Flask dashboard web portal port
EXPOSE 5001

# Command to run the application
ENTRYPOINT ["python", "main.py"]
