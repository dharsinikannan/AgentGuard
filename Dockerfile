# One image for every AgentGuard Python service: gateway, tool servers, fake GitHub,
# dashboard, agent. docker-compose.yml picks the command per service.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Not root: if a tool server or the agent is ever compromised, it isn't root in the container.
RUN useradd --create-home --uid 1000 agentguard
WORKDIR /app

# Dependencies first, so code changes don't reinstall them (layer cache). Only files
# that affect dependencies: copying README.md here made every docs edit reinstall everything.
COPY pyproject.toml ./
COPY agentguard/__init__.py agentguard/
COPY servers/__init__.py servers/
COPY agent/__init__.py agent/
COPY evals/__init__.py evals/
RUN pip install ".[agent,aws,ui,demo]"

COPY . .
RUN pip install --no-deps . && chown -R agentguard /app
USER agentguard

EXPOSE 8765 8501
CMD ["python", "-m", "agentguard", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
