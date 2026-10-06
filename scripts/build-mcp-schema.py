"""Build the shipped MCP tool catalog from the reviewed connector schema.

Run from any directory. This only reads the checked-in OpenAPI snapshot.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = json.loads((ROOT / "openapi/connectors-v1.json").read_text())


def expand(value):
    if isinstance(value, list):
        return [expand(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        target = source
        for part in value["$ref"].split("/")[1:]:
            target = target[part]
        return expand({**target, **{k: v for k, v in value.items() if k != "$ref"}})
    result = {key: expand(item) for key, item in value.items()}
    if result.get("type") == "object":
        result["additionalProperties"] = False
    return result


tools = []
for path, operations in source["paths"].items():
    for operation in operations.values():
        name = operation["operationId"]
        body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
        schema = expand(body) or {"type": "object", "properties": {}, "required": []}
        schema.setdefault("required", [])
        for parameter in operation.get("parameters", []):
            if parameter["in"] not in ("path", "query"):
                continue
            schema["properties"][parameter["name"]] = expand(parameter["schema"])
            if parameter.get("required"):
                schema["required"].append(parameter["name"])
        if name == "start_travel_claim":
            schema["properties"]["idempotency_key"] = {
                "type": "string", "pattern": "^[A-Za-z0-9_-]{8,100}$",
                "description": "Keep the same key and fields when retrying a failed creation. Never use personal data.",
            }
        if name == "search_credit_cards":
            schema["properties"]["q"] = {"type": "string", "minLength": 1, "maxLength": 100,
                "description": "Credit card product name, never a card number."}
            schema["required"] = ["q"]
        if name == "request_data_deletion":
            schema["properties"]["traveler_requested"] = {"type": "boolean", "const": True}
            schema["required"] = ["traveler_requested"]
        if name == "import_selected_email":
            schema["required"] = [key for key in schema["required"] if key not in ("provider", "message_id")]
        if name == "import_claim_attachment":
            schema["required"] = [key for key in schema["required"] if key != "content_type"]
        if name in ("delete_travel_claim_draft", "disconnect", "submit_authorized_travel_claim"):
            schema["properties"]["traveler_confirmed"] = {
                "type": "boolean", "const": True,
                "description": "True only after the traveler explicitly authorized this action.",
            }
            schema["required"].append("traveler_confirmed")
        if name in ("import_selected_email", "import_claim_attachment"):
            schema["properties"]["user_authorized_sharing"] = {
                "type": "boolean", "const": True,
                "description": "True only after the traveler agreed to share this specific email or file.",
            }
        schema["additionalProperties"] = False
        annotations = dict(operation["x-tool-annotations"])
        # All tools contact a remote API. Creation is idempotent only when the
        # caller supplies a stable key, so do not promise it unconditionally.
        annotations["openWorldHint"] = True
        if name == "start_travel_claim":
            annotations["idempotentHint"] = False
        description = operation.get("description", operation.get("summary", name))
        if name == "import_claim_attachment":
            description += " Supply the selected file's base64 bytes, never a URL or a local path. If the client cannot transfer bytes, give the traveler the draft's evidence_upload_url instead."
        if name == "get_travel_claim_intake":
            description += " Return review_url to the traveler when signing is needed. Never open, fill or sign that page for them."
        tools.append({"name": name, "description": description, "inputSchema": schema, "annotations": annotations})


def extra(name, description, properties=None, required=None, read=False):
    tools.append({
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False},
        "annotations": {"readOnlyHint": read, "destructiveHint": not read, "idempotentHint": read, "openWorldHint": True},
    })


extra("get_service_info", "Read TourClaim availability and mode without signing in. Call first. In review mode use fictional data only: nothing is filed, charged or sent to a clinician.", read=True)
extra("begin_login", "Start sign-in only at the traveler's request. Show verification_uri and user_code exactly as returned; explain that their assistant started this sign-in. The traveler opens the page and types the matching code. Never put the code in a URL, open the page yourself or ask for a key in chat. Then use complete_login after the traveler approves.",
      {"traveler_requested": {"type": "boolean", "const": True}}, ["traveler_requested"])
extra("complete_login", "Check the pending browser sign-in once. Respect retry_after_seconds if approval is still pending. The server stores the credential locally and never returns it to the model. No arguments or code needed.")

destination = ROOT / "python/src/tourclaim/mcp_tools.json"
destination.write_text(json.dumps(tools, indent=2, ensure_ascii=False) + "\n")
print(f"Wrote {len(tools)} tools to {destination}")
