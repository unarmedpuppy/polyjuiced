FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Copy scripts
COPY scripts/ ./scripts/

# Copy tests (for running tests in container)
COPY tests/ ./tests/

# Create non-root user and data directory
RUN useradd -m -u 1000 botuser && \
    mkdir -p /app/data && \
    chown -R botuser:botuser /app
USER botuser

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Run the bot
CMD ["python", "-m", "src.main"]
