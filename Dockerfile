# mcp-ooxml-ledger is a STDIO MCP server -- it speaks JSON-RPC on stdin/stdout, not
# HTTP. Do NOT add EXPOSE or a HEALTHCHECK that assumes a network listener; there is
# nothing to probe, and `docker run -i <image>` is the only supported invocation.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/Anselmoo/mcp-ooxml-ledger" \
      org.opencontainers.image.description="Edit Office documents and prove no edit went unrecorded" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.Anselmoo/mcp-ooxml-ledger"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OOXML_LEDGER_ROOTS is the filesystem security boundary (see mcp/guards.py
# Boundary._resolve). It MUST be an explicit, non-root-owned path -- never let it
# default to "/", which would let any tool argument resolve anywhere on the image.
# Callers mount their real documents here:
#     docker run -i -v "$PWD":/documents ghcr.io/anselmoo/mcp-ooxml-ledger:<version>
#
# Set as its own instruction rather than inside the continuation above: Docker does
# strip comment lines out of a `\`-continued instruction, but that is a parser detail
# this image's build should not depend on when a separate ENV costs nothing.
ENV OOXML_LEDGER_ROOTS=/documents

WORKDIR /app

# Only what the sdist build needs: pyproject.toml (readme/license fields point at
# these two files) and the package source. No tests/, docs/, mockup/, or dev tooling
# reaches the image -- keeps it small and keeps dev cruft out of what gets shipped.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# `pip install .` resolves build-system.requires (uv_build, pinned in pyproject.toml)
# in an isolated build env and installs the resulting wheel -- no need to invoke uv
# or a separate wheel-build stage.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && rm -rf /root/.cache /app/src

# Non-root: an MCP server that opens and edits arbitrary Office documents has no
# business running as root inside its own container.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin ooxml \
    && mkdir -p /documents \
    && chown -R ooxml:ooxml /documents

USER ooxml

# ENTRYPOINT is the MCP server itself, so `docker run -i <image>` speaks MCP
# immediately -- no shell, no wrapper command to select.
ENTRYPOINT ["ooxml-ledger-mcp"]
