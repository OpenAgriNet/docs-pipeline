FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
# apt-get is skipped — curl is not needed at runtime.
# Uncomment below only if you need libreoffice (office→PDF) or curl:
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     curl \
#     && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
# The pip shipped in python:3.10-slim (23.0.1) rejects wheels whose metadata
# name uses underscores ("expected 'typing-extensions', got 'typing_extensions'")
# and falls back to building the sdist. That needs flit_core, which lives on
# PyPI — unreachable below because --index-url REPLACES PyPI rather than adding
# to it, so the build fails. A newer pip accepts the wheel and never shells out
# to a source build. Keep this ahead of the torch install.
RUN pip install --no-cache-dir --upgrade "pip>=24.0"
# CPU-only torch first: the default PyPI wheel pulls in full NVIDIA CUDA
# runtime libs (multi-GB) even on this GPU-less image. sentence-transformers
# then sees torch already satisfied and skips re-resolving it.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY pipeline/ ./pipeline/

# Catch imports that work locally but fail on the shipped Python 3.10 image.
RUN python -c "import pipeline.api"

# Copy test data for e2e tests
COPY test_data/ ./test_data/

# Create books directory
RUN mkdir -p /app/books

EXPOSE 8001

# Default command (overridden in docker-compose)
CMD ["uvicorn", "pipeline.api:app", "--host", "0.0.0.0", "--port", "8001"]
