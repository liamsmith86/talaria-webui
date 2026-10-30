"""A loopback model that does what each prompt's plan says, and makes no choices of its own.

A plan is a list of steps, which a test sends as its prompt. Every request for that turn
takes the step after those already answered, so the model keeps no state.
"""

import json
import secrets
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Reply with every tool result of the turn, which shows that real output came back.
ECHO = {"echo": True}


def tool(name, **arguments):
    return {"tool": name, "arguments": arguments}


def terminal(command):
    return tool("terminal", command=command)


def held_terminal(gate, command):
    """A command that stays running until the test creates the file `gate`."""
    return terminal(f"until [ -e {gate} ]; do sleep 0.1; done; {command}")


def plan(*steps):
    return json.dumps({"plan": steps})


def cut_off(text):
    """Stream `text`, then lose the connection before the response ends."""
    return {"cut_off": text}


def planned(content):
    try:
        steps = json.loads(content)
        return steps["plan"] if isinstance(steps, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def next_step(messages):
    """The step this request asks for, and the tool results so far in its turn."""
    answered, results = 0, []
    for message in reversed(messages):
        if message["role"] == "assistant":
            answered += 1
        elif message["role"] == "tool":
            results.insert(0, message["content"])
        elif message["role"] == "user" and (steps := planned(message["content"])) is not None:
            return (steps[answered] if answered < len(steps) else "OK"), results
    return "OK", []


def answer(step, results):
    """The assistant message for a step, and why it finishes there."""
    if isinstance(step, dict) and "tool" in step:
        arguments = json.dumps(step["arguments"])
        call = {"name": step["tool"], "arguments": arguments}
        calls = [{"index": 0, "id": f"call_{secrets.token_hex(4)}", "type": "function"}]
        calls[0]["function"] = call
        return {"role": "assistant", "tool_calls": calls}, "tool_calls"
    if isinstance(step, dict) and "cut_off" in step:
        return {"role": "assistant", "content": step["cut_off"]}, None
    text = "\n".join(results) if step == ECHO else step
    return {"role": "assistant", "content": text}, "stop"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        message, finish = answer(*next_step(request["messages"]))
        body = {"id": "scripted", "created": 1, "model": request["model"]}
        body["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if request.get("stream"):
            choice = {"index": 0, "delta": message, "finish_reason": finish}
            chunk = body | {"object": "chat.completion.chunk", "choices": [choice]}
            end = "data: [DONE]\n\n" if finish else ""
            payload, kind = f"data: {json.dumps(chunk)}\n\n{end}", "text/event-stream"
        else:
            choice = {"index": 0, "message": message, "finish_reason": finish}
            completion = body | {"object": "chat.completion", "choices": [choice]}
            payload, kind = json.dumps(completion), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        if not finish:
            # Promise more than is sent, so closing reads as a lost connection.
            self.send_header("Content-Length", str(len(payload.encode()) + 1024))
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *_):
        pass


@contextmanager
def scripted_model():
    """Serve the model, yielding its API's base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
