# VPN Bot Dockerfile
# Multi-stage build for production deployment

FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN useradd -m -u 1000 vpn-bot && \
    mkdir -p /var/lib/vpn-bot /var/log/vpn-bot /app && \
    chown -R vpn-bot:vpn-bot /var/lib/vpn-bot /var/log/vpn-bot /app

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=vpn-bot:vpn-bot bot/ ./bot/
COPY --chown=vpn-bot:vpn-bot scripts/ ./scripts/

# Switch to non-root user
USER vpn-bot

# Expose port for web server (health checks)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Run the bot
CMD ["python", "-m", "bot.main"]
