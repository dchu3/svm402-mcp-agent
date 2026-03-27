"""Convert MCP tool schemas to Gemini function declarations."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.genai import types


def mcp_type_to_gemini_type(mcp_type: str) -> str:
    """Convert MCP/JSON Schema type to Gemini type string."""
    type_map = {
        "string": "STRING",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }
    return type_map.get(mcp_type, "STRING")


def convert_json_schema_to_gemini_schema(
    schema: Dict[str, Any], depth: int = 0
) -> Dict[str, Any]:
    """Recursively convert JSON Schema to Gemini Schema dict."""
    if depth > 5:
        return {"type": "STRING"}

    schema_type = schema.get("type", "string")

    # Handle arrays
    if schema_type == "array":
        items_schema = schema.get("items", {"type": "string"})
        return {
            "type": "ARRAY",
            "items": convert_json_schema_to_gemini_schema(items_schema, depth + 1),
        }

    # Handle objects
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        gemini_properties = {}
        for prop_name, prop_schema in properties.items():
            gemini_properties[prop_name] = convert_json_schema_to_gemini_schema(
                prop_schema, depth + 1
            )

        result: Dict[str, Any] = {
            "type": "OBJECT",
            "properties": gemini_properties,
        }
        if required:
            result["required"] = required
        return result

    # Handle primitives
    gemini_type = mcp_type_to_gemini_type(schema_type)
    kwargs: Dict[str, Any] = {"type": gemini_type}

    if "description" in schema:
        kwargs["description"] = schema["description"]

    if "enum" in schema:
        kwargs["enum"] = schema["enum"]

    return kwargs


def mcp_tool_to_gemini_function(
    client_name: str, tool: Dict[str, Any]
) -> Optional[types.FunctionDeclaration]:
    """Convert a single MCP tool to a Gemini FunctionDeclaration."""
    try:
        tool_name = tool.get("name")
        if not tool_name:
            return None

        # Namespace the function name: client_method
        full_name = f"{client_name}_{tool_name}"
        description = tool.get("description", f"Call {client_name}.{tool_name}")

        # Get input schema
        input_schema = tool.get("inputSchema", {})

        # Convert to Gemini parameters schema
        # Always provide a schema, even for parameterless tools
        if input_schema:
            parameters = convert_json_schema_to_gemini_schema(input_schema)
        else:
            # Provide empty object schema for parameterless tools
            parameters = {"type": "OBJECT"}

        return types.FunctionDeclaration(
            name=full_name,
            description=description,
            parameters=parameters,
        )
    except Exception:
        return None


def convert_mcp_tools_to_gemini(
    client_name: str, tools: List[Dict[str, Any]]
) -> List[types.FunctionDeclaration]:
    """Convert a list of MCP tools to Gemini function declarations."""
    declarations = []
    for tool in tools:
        declaration = mcp_tool_to_gemini_function(client_name, tool)
        if declaration:
            declarations.append(declaration)
    return declarations


def parse_function_call_name(name: str) -> tuple[str, str]:
    """Parse a namespaced function name into (client, method)."""
    name = name.strip()
    parts = name.split("_", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", name
