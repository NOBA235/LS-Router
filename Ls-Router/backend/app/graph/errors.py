class ToolError(Exception):
    code = "tool_error"


class ToolInputError(ToolError):
    """The caller passed something invalid (unknown id, bad month, empty description ...)."""
    code = "invalid_input"


class ToolStateError(ToolError):
    """The request is valid but the graph state does not allow it (e.g. clearing an observed event)."""
    code = "invalid_state"


class StoreError(ToolError):
    """A Cypher statement failed."""
    code = "store_error"
