"""A minimal tool-calling agent loop over the WMS MCP server, and its offline evaluation.

Nothing in this package calls a model API unless you run ``wms-eval --model <id>``
with LiteLLM installed and your own keys. Tests and ``make eval`` use
:class:`~wms_mcp.agent.models.ScriptedModel`, which replays hand-written turns.
"""
