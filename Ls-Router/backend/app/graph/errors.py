class ToolError(Exception):
    code = "tool_error"


class ToolInputError(ToolError):
    """The caller passed something invalid (unknown id, bad month, empty description ...)."""
    code = "invalid_input"


class ToolNotFoundError(ToolInputError):
    """The input is well formed but names an entity that is not in the graph (unknown place, segment, bridge, event, corridor).

    It deliberately keeps ``code == "invalid_input"`` so every existing consumer (CLI, MCP server, tests) behaves as before;
    the HTTP API checks the type to answer 404 instead of 422.
    """


class ToolStateError(ToolError):
    """The request is valid but the graph state does not allow it (e.g. clearing an observed event)."""
    code = "invalid_state"


class StoreError(ToolError):
    """A Cypher statement failed."""
    code = "store_error"
