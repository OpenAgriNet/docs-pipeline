FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY pipeline/ ./pipeline/

# Copy test data for e2e tests
COPY test_data/ ./test_data/

# Create books directory
RUN mkdir -p /app/books

EXPOSE 8001

# Default command (overridden in docker-compose)
CMD ["uvicorn", "pipeline.api:app", "--host", "0.0.0.0", "--port", "8001"]
