# ---- Stage 1: Build MCP servers ----
FROM node:22-slim AS mcp-builder

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ARG DEXSCREENER_MCP_REF=bee751d9bde24ef9680c70db5e09a9ef56985169
ARG RUGCHECK_MCP_REF=723c89636157c4095f2eb4074d33ffbf4de3e3cc
ARG SOLANA_RPC_MCP_REF=c22d7fb5878d99d1432ed4e624f3ad3cee15e965
ARG DEXPAPRIKA_MCP_VERSION=1.0.5

# Clone and build each MCP server from public GitHub repos
RUN git clone https://github.com/dchu3/dex-screener-mcp.git \
    && cd dex-screener-mcp && git checkout "$DEXSCREENER_MCP_REF" && npm ci && npm run build

RUN git clone https://github.com/dchu3/dex-rugcheck-mcp.git \
    && cd dex-rugcheck-mcp && git checkout "$RUGCHECK_MCP_REF" && npm ci && npm run build

RUN git clone https://github.com/dchu3/solana-rpc-mcp.git \
    && cd solana-rpc-mcp && git checkout "$SOLANA_RPC_MCP_REF" && npm ci && npm run build

# Install dexpaprika-mcp globally
RUN npm install -g "dexpaprika-mcp@${DEXPAPRIKA_MCP_VERSION}"


# ---- Stage 2: Python runtime ----
FROM python:3.11-slim

# Install Node.js runtime (required to spawn MCP server subprocesses)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Copy built MCP servers
COPY --from=mcp-builder /build/dex-screener-mcp /opt/mcp/dex-screener-mcp
COPY --from=mcp-builder /build/dex-rugcheck-mcp /opt/mcp/dex-rugcheck-mcp
COPY --from=mcp-builder /build/solana-rpc-mcp /opt/mcp/solana-rpc-mcp

# Copy globally installed dexpaprika-mcp
COPY --from=mcp-builder /usr/local/lib/node_modules/dexpaprika-mcp /usr/local/lib/node_modules/dexpaprika-mcp
RUN ln -s /usr/local/lib/node_modules/dexpaprika-mcp/dist/bin.js /usr/local/bin/dexpaprika-mcp \
    && chmod +x /usr/local/bin/dexpaprika-mcp

# Pre-configure MCP server commands (users don't need to set these)
ENV MCP_DEXSCREENER_CMD="node /opt/mcp/dex-screener-mcp/dist/index.js"
ENV MCP_DEXPAPRIKA_CMD="dexpaprika-mcp"
ENV MCP_RUGCHECK_CMD="node /opt/mcp/dex-rugcheck-mcp/dist/index.js"
ENV MCP_SOLANA_RPC_CMD="node /opt/mcp/solana-rpc-mcp/dist/index.js"

# Create a non-root runtime user and writable data directory
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /home/botuser/.x402-bot \
    && chown -R botuser:botuser /home/botuser

# Set up Python application
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
RUN chown -R botuser:botuser /app

USER botuser

ENTRYPOINT ["python", "-m", "app"]
CMD ["--interactive"]
