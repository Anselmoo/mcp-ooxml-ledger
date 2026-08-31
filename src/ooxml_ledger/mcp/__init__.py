"""The MCP front-end. Nothing in `ooxml_ledger` outside this package may import it.

Name note: this package is `ooxml_ledger.mcp`, and it imports the UNRELATED top-level package
`mcp` (the official SDK, which supplies `mcp.types.ToolAnnotations` and is pulled in by
fastmcp). There is no collision: Python 3 has no implicit relative imports, so inside this
package `from mcp.types import ToolAnnotations` resolves to the top-level package. A test
pins that down rather than leaving it to be rediscovered from a confusing traceback.
"""
