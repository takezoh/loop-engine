"""A Model Context Protocol server over stdio for one tool group, on the standard library alone.

It speaks what a tool server needs: `initialize`, `ping`, `tools/list` and `tools/call`, as JSON-RPC
2.0 messages, one per line. A tool group has `tools` ({name: (description, input schema)}),
`instructions` and `invoke(name, arguments)`. This module decides nothing a tool decides: a refusal
by the group is reported as a tool error, distinct from a unit's stop.
"""
import json
import sys
import threading

NAME = "loop-engine"
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # protocol revisions; the first is offered to an unknown client
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id, value):
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def call(group, params):
    """The result of `tools/call`: the tool's value as JSON text, or the refusal as a tool error."""
    try:
        value = group.invoke(params["name"], params.get("arguments") or {})
        text, failed = json.dumps(value, ensure_ascii=False, allow_nan=False), False
    except Exception as error:  # a refusal or a failure of the entry; neither ends the server
        text, failed = str(error) or type(error).__name__, True
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def respond(group, message):
    """The response to one message, or None when it asks for none (a notification, a client's response)."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "a message is one JSON object")
    method, request_id, params = message.get("method"), message.get("id"), message.get("params", {})
    if not isinstance(method, str):
        return None if "result" in message or "error" in message else _error(request_id, INVALID_REQUEST, "no method")
    if "id" not in message:
        return None
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "params is not an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(request_id, {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": "unversioned"},
            "instructions": group.instructions})
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": [
            {"name": name, "description": description, "inputSchema": input_schema}
            for name, (description, input_schema) in group.tools.items()]})
    if method == "tools/call":
        if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
            return _error(request_id, INVALID_PARAMS, "a call names a tool and gives its arguments as an object")
        return _result(request_id, call(group, params))
    return _error(request_id, METHOD_NOT_FOUND, "method not found")


def serve(group, reader=None, writer=None):
    """Answer messages from `reader` on `writer` until the reader ends. A tool call runs on its own
    thread, so a wait does not hold up other requests; `writer` carries protocol messages only."""
    reader = sys.stdin.buffer if reader is None else reader
    writer = sys.stdout.buffer if writer is None else writer
    writing, calls = threading.Lock(), []

    def send(response):
        if response is None:
            return
        with writing:
            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            writer.flush()

    for line in reader:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send(_error(None, PARSE_ERROR, "not JSON"))
            continue
        if isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message:
            thread = threading.Thread(target=lambda item=message: send(respond(group, item)), daemon=True)
            calls.append(thread)
            thread.start()
        else:
            send(respond(group, message))
    for thread in calls:  # the reader ended: let the calls already accepted answer
        thread.join()
