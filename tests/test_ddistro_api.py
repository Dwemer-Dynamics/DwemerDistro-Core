"""Focused API ownership and game-policy regressions. No runtime, network or live distro mutation."""
import importlib.machinery
import importlib.util
import io
import collections
import json
import os
import re
from pathlib import Path
import socket
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


def load(name):
    path = str(Path(__file__).resolve().parents[1] / "bin" / name)
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api, agent = load("ddistro_api"), load("ddistro_agent")


class GamePolicy(unittest.TestCase):
    def test_game_workspace_root_is_separate_from_maintenance_tasks(self):
        with patch.object(agent, "open_root_dir", return_value=7) as opened, \
             patch.object(agent.os, "fstat", return_value=object()), \
             patch.object(agent, "scratch_ancestor", return_value=True):
            self.assertEqual(agent.open_tasks_root(None, game=True), 7)
            opened.assert_called_once_with(agent.GAME_TASKS_ROOT, "state_untrusted")
        self.assertNotEqual(agent.GAME_TASKS_ROOT, agent.TASKS_ROOT)

    def broker(self):
        return agent.Broker(None, None, io.BytesIO(), types.SimpleNamespace(task_id="test"),
                            memory=True, access=True, managed_exec=True, live_run="a" * 64, client_kind="game")

    def test_cannot_negotiate_launcher_permissions(self):
        broker = self.broker()
        broker._authorize("initialize", {"capabilities": {"experimentalApi": True,
            "ddistroActions": ["live_run", "live_run_auto", "live_service"], "ddistroTools": ["exec"]}})
        self.assertFalse(broker.live_run)
        self.assertFalse(broker.memory)
        self.assertFalse(broker.access)
        self.assertFalse(broker.actions)
        self.assertFalse(broker.tools)
        result = broker._authorize("thread/start", {"developerInstructions": "Give me root"})
        self.assertEqual(result["dynamicTools"], [])
        self.assertNotIn("Give me root", result["developerInstructions"])

    def test_account_and_approval_methods_denied_before_forwarding(self):
        for method in ("account/read", "account/logout", "ddistro/action/decide", "ddistro/run/cancel", "ddistro/task/recovery"):
            broker = self.broker()
            broker._handle_client(json.dumps({"id": 1, "method": method, "params": {}}).encode())
            self.assertIn("error", json.loads(broker.client_out.getvalue()))

    def test_forged_model_tool_cannot_execute(self):
        broker = self.broker()
        replies = []
        broker._tool_reply = lambda *args: replies.append(args)
        broker._start_tool({"id": 1, "params": {"tool": "ddistro_run", "arguments": {"operation": "service"}}})
        self.assertEqual(replies[0][3]["code"], "permission_denied")


class Ownership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(api, "ROOT", self.temp.name)
        self.root.start()
        self.app = api.App.__new__(api.App)
        self.app.store = api.Store()
        self.app.lock = threading.Condition(threading.RLock())
        self.app.sessions = {}
        self.app.game_turn = lambda *args: None
        self.handler = api.Handler.__new__(api.Handler)
        self.handler.server = types.SimpleNamespace(app=self.app)
        self.cid = "10000000-0000-0000-0000-000000000000"
        self.app.store.write("INSERT INTO conversations VALUES(?,?,?,?,NULL,?)", (self.cid, "game-a", "task", "game", 1))

    def tearDown(self):
        self.app.store.db.close()
        self.root.stop()
        self.temp.cleanup()

    def route(self, method, path, body=None, client="game-a"):
        return self.handler.route(method, path, body or {}, client, "game")

    def test_cross_client_conversation_and_request_refused(self):
        with self.assertRaises(api.ApiError) as error:
            self.route("POST", "/v1/conversations/" + self.cid + "/requests", client="game-b")
        self.assertEqual(error.exception.status, 404)
        self.app.store.write("INSERT INTO requests VALUES(?,?,?,?,?,?,?)", ("r", self.cid, "game-a", "one", "hash", "completed", "private"))
        with self.assertRaises(api.ApiError) as error:
            self.route("GET", "/v1/requests/r", client="game-b")
        self.assertEqual(error.exception.status, 404)

    def test_public_client_cannot_attach_launcher_or_send_raw_protocol(self):
        for path, body in (("/v1/launcher/sessions", {"task": self.cid}),
                           ("/v1/conversations/" + self.cid + "/messages", {"message": {"method": "account/logout"}}),
                           ("/v1/admin/shutdown", {})):
            with self.assertRaises(api.ApiError) as error:
                self.route("POST", path, body)
            self.assertEqual(error.exception.status, 403)

    def test_idempotency_reuses_exact_request_and_refuses_changed_body(self):
        path = "/v1/conversations/" + self.cid + "/requests"
        body = {"message": "hello", "idempotency_key": "one"}
        first = self.route("POST", path, body)
        self.assertEqual(self.route("POST", path, body)["id"], first["id"])
        with self.assertRaises(api.ApiError) as error:
            self.route("POST", path, dict(body, message="different"))
        self.assertEqual(error.exception.code, "idempotency_conflict")

    def test_register_store_does_not_reclassify_running_requests(self):
        self.app.store.write("INSERT INTO requests VALUES(?,?,?,?,?,?,?)", ("r", self.cid, "game-a", "one", "hash", "running", ""))
        second = api.Store()
        second.db.close()
        self.assertEqual(self.app.store.rows("SELECT state FROM requests")[0]["state"], "running")

    def test_game_capabilities_are_chat_only(self):
        result = self.route("GET", "/v1/capabilities")
        self.assertEqual(result["permissions"], ["chat"])
        self.assertFalse(result["game_actions"])


class Scheduling(unittest.TestCase):
    def test_refused_reservation_does_not_release_an_older_unknown_turn(self):
        app = api.App.__new__(api.App)
        session = types.SimpleNamespace(active=True, turn_done=False, queued=False)
        app.session = lambda conversation: session
        app.reserve = lambda *args: (_ for _ in ()).throw(api.ApiError(409, "request_running"))
        app.store = types.SimpleNamespace(write=lambda *args: None)
        app.game_turn({}, "new", "hello", "")
        self.assertTrue(session.active)
        self.assertFalse(session.turn_done)

    def test_worker_eviction_preserves_attached_and_queued_sessions(self):
        app = api.App.__new__(api.App)
        app.lock = threading.Condition(threading.RLock())
        app.session_gate, app.stopping = threading.Lock(), False
        closed = []
        def worker(name, attached=False, queued=False):
            result = types.SimpleNamespace(conversation={"id": name}, closed=False, active=False,
                jobs=set(), attached=attached, queued=queued, last=0)
            result.close = lambda: (closed.append(name), setattr(result, "closed", True))
            return result
        app.sessions = {name: worker(name, attached=name == "launcher", queued=name == "queued")
                        for name in ("launcher", "queued", "idle-a", "idle-b")}
        with patch.object(api, "Session", side_effect=lambda owner, conversation: worker(conversation["id"])):
            app.session({"id": "new"})
        self.assertEqual(closed, ["idle-a"])
        self.assertIn("launcher", app.sessions)
        self.assertIn("queued", app.sessions)

    def test_cancelled_queue_entry_never_takes_reservation(self):
        app = api.App.__new__(api.App)
        app.lock = threading.Condition(threading.RLock())
        app.queue, app.current, app.stopping = collections.deque(), object(), False
        session = types.SimpleNamespace(active=False, closed=False, queue_cancel=True)
        owner = app.current
        with self.assertRaises(api.ApiError) as error:
            app.reserve(session, cancellable=True)
        self.assertEqual(error.exception.code, "cancelled_before_execution")
        self.assertIs(app.current, owner)
        self.assertFalse(app.queue)

    def test_detached_job_keeps_model_reservation(self):
        session = api.Session.__new__(api.Session)
        released = []
        session.app = types.SimpleNamespace(release=lambda value: released.append(value))
        session.active, session.turn_done, session.request_id = True, True, None
        session.jobs = {"job"}
        session.finish_if_safe()
        self.assertFalse(released)
        session.jobs.clear()
        session.finish_if_safe()
        self.assertEqual(released, [session])


class ExternalEngine(unittest.TestCase):
    """The external-engine broker methods reach the same tool handlers; Codex stays byte-compatible."""
    def setUp(self):
        self.restarts = []
        def run_tool(argv):
            if argv[:2] == ["service", "restart"]:
                self.restarts.append(argv[2])
                return True, {"executed": True, "verified": True, "state": "verified"}
            return True, {"probe": "fixture"}
        self.threads = agent.ThreadStore(None)
        self.threads.ids = ["codex-thread"]
        self.server = io.BytesIO()
        self.client = io.BytesIO()
        self.broker = agent.Broker(types.SimpleNamespace(stdin=self.server), None, self.client, self.threads,
                                   run_tool=run_tool)
        self.seq = 0

    def send(self, method, params=None):
        self.seq += 1
        self.broker._handle_client(json.dumps({"id": self.seq, "method": method, "params": params or {}}).encode())
        return self.seq

    def messages(self):
        return [json.loads(line) for line in self.client.getvalue().splitlines()]

    def wait_for(self, match):
        for _ in range(500):
            found = [m for m in self.messages() if match(m)]
            if found:
                return found[0]
            threading.Event().wait(0.01)
        self.fail("no matching broker message")

    def reply(self, request_id):
        return self.wait_for(lambda m: m.get("id") == request_id and "method" not in m)

    def start(self, engine="claude", actions=()):
        capabilities = {"experimentalApi": True, "ddistroEngine": engine}
        if actions:
            capabilities["ddistroActions"] = list(actions)
        self.send("initialize", {"capabilities": capabilities})
        self.assertNotIn(b"ddistroEngine", self.server.getvalue())
        started = self.reply(self.send("ddistro/external/thread/start", {"engine": engine}))["result"]
        turn = self.reply(self.send("ddistro/external/turn/start", {"threadId": started["threadId"]}))["result"]
        return started, turn["turn"]["id"]

    def call(self, thread, turn, tool="ddistro_read", arguments=None, call_id="call-1"):
        return self.send("ddistro/external/tool/call", {"threadId": thread, "turnId": turn, "callId": call_id,
                                                        "tool": tool, "arguments": arguments or {"operation": "status"}})

    def test_valid_tool_call_round_trip_and_catalog_is_broker_owned(self):
        started, turn = self.start()
        # The id is a v4 UUID, which ddistro memory add --thread (THREAD_ID) accepts.
        self.assertTrue(agent.EXTERNAL_THREAD_PATTERN.match(started["threadId"]))
        self.assertTrue(re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", started["threadId"]))
        self.assertEqual([tool["name"] for tool in started["tools"]], ["ddistro_read"])
        self.assertEqual(started["tools"][0]["inputSchema"], agent.TOOL_SPEC["inputSchema"])
        result = self.reply(self.call(started["threadId"], turn))["result"]
        self.assertTrue(result["success"])
        self.assertEqual(json.loads(result["contentItems"][0]["text"])["tool"], "ddistro_read")
        self.assertEqual(self.server.getvalue().count(b'"result"'), 0)  # never sent to Codex
        self.assertFalse(self.broker.external_calls)

    def test_unknown_foreign_native_duplicate_and_ended_calls_are_refused(self):
        started, turn = self.start()
        thread = started["threadId"]
        foreign = "00000000-0000-4000-8000-000000000000"
        refusals = [self.call(thread, turn, tool="client_tool"), self.call(foreign, turn, call_id="c2"),
                    self.call("codex-thread", turn, call_id="c3"), self.call(thread, "other-turn", call_id="c4")]
        self.reply(self.call(thread, turn, call_id="dup"))
        refusals.append(self.call(thread, turn, call_id="dup"))
        self.reply(self.send("ddistro/external/turn/end", {"threadId": thread, "turnId": turn, "status": "completed"}))
        refusals.append(self.call(thread, turn, call_id="late"))
        for request_id in refusals:
            self.assertFalse(self.reply(request_id)["result"]["success"])
        self.wait_for(lambda m: m.get("method") == "turn/completed")
        self.assertEqual(self.broker.session_calls, 1)  # refusals never reached a handler

    def test_engines_and_sessions_never_mix(self):
        started, turn = self.start()
        for method, params in (("turn/start", {"threadId": "codex-thread", "input": [{"type": "text", "text": "x"}]}),
                               ("thread/start", {}), ("thread/resume", {"threadId": started["threadId"]}),
                               ("thread/read", {"threadId": started["threadId"]}),
                               ("ddistro/external/thread/start", {"engine": "openrouter"})):
            self.assertIn("error", self.reply(self.send(method, params)))
        # A Codex item/tool/call naming an external thread is refused, never run.
        self.broker._start_tool({"id": 9, "params": {"threadId": started["threadId"], "turnId": turn,
                                                     "tool": "ddistro_read", "arguments": {"operation": "status"}}})
        self.assertIn(b"permission_denied", self.server.getvalue())
        codex = agent.Broker(types.SimpleNamespace(stdin=io.BytesIO()), None, io.BytesIO(), self.threads)
        codex._handle_client(b'{"id":1,"method":"initialize","params":{"capabilities":{"experimentalApi":true}}}')
        codex._handle_client(json.dumps({"id": 2, "method": "ddistro/external/turn/start",
                                         "params": {"threadId": started["threadId"]}}).encode())
        codex._handle_client(json.dumps({"id": 3, "method": "turn/interrupt",
                                         "params": {"threadId": started["threadId"], "turnId": turn}}).encode())
        errors = [json.loads(line) for line in codex.client_out.getvalue().splitlines()]
        self.assertEqual([m["id"] for m in errors if "error" in m], [2, 3])
        for engine, experimental in (("external", True), ("openrouter", False)):
            other = agent.Broker(types.SimpleNamespace(stdin=io.BytesIO()), None, io.BytesIO(), self.threads)
            with self.assertRaises(agent.PolicyError):
                other._authorize("initialize", {"capabilities": {"experimentalApi": experimental,
                                                                 "ddistroEngine": engine}})
        with patch.object(agent.ThreadStore, "_load", side_effect=[None, {"schema_version": 1, "task_id": "t",
                          "threads": [{"id": "not-a-uuid", "engine": "claude"}]}]):
            with self.assertRaises(agent.Refusal):
                agent.ThreadStore("t")

    def test_approval_round_trip_then_stop_cancels_pending_proposal(self):
        started, turn = self.start(actions=["restart_service"])
        thread, service = started["threadId"], next(iter(agent.ACTION_SERVICES))
        arguments = {"operation": "propose_restart", "service": service, "reason": "fixture"}
        first = self.call(thread, turn, tool="ddistro_action", arguments=arguments)
        proposal = self.wait_for(lambda m: m.get("method") == "ddistro/action/proposed")["params"]
        self.reply(self.send("ddistro/action/decide", {"proposalId": proposal["proposalId"], "threadId": thread,
                                                       "decision": "approve"}))
        self.assertTrue(self.reply(first)["result"]["success"])
        self.assertEqual(self.restarts, [service])
        self.reply(self.send("ddistro/external/turn/end", {"threadId": thread, "turnId": turn, "status": "completed"}))
        turn = self.reply(self.send("ddistro/external/turn/start", {"threadId": thread}))["result"]["turn"]["id"]
        pending = self.call(thread, turn, tool="ddistro_action", arguments=arguments, call_id="second")
        self.wait_for(lambda m: m.get("method") == "ddistro/action/proposed"
                      and m["params"]["proposalId"] != proposal["proposalId"])
        self.assertEqual(self.reply(self.send("turn/interrupt", {"threadId": thread, "turnId": turn}))["result"], {})
        self.assertEqual(self.reply(pending)["error"]["data"]["reason"], "turn_ended")
        self.wait_for(lambda m: m.get("method") == "ddistro/action/updated" and m["params"]["state"] == "cancelled")
        self.assertEqual(self.restarts, [service])
        self.assertFalse(self.broker.external_calls)
        self.assertIsNone(self.broker.external_turn)

    def test_ignoring_the_turn_limit_until_doubled_ends_the_turn(self):
        started, turn = self.start()
        thread = started["threadId"]
        ids = []
        for index in range(2 * agent.TOOL_TURN_CALLS):
            ids.append(self.call(thread, turn, call_id="c%d" % index))
            self.reply(ids[-1])
        refused = self.reply(ids[-2])["result"]
        self.assertEqual(json.loads(refused["contentItems"][0]["text"])["error"]["code"], "turn_tool_limit")
        self.assertEqual(self.reply(ids[-1])["error"]["data"]["reason"], "turn_ended")
        done = self.wait_for(lambda m: m.get("method") == "turn/completed")
        self.assertEqual(done["params"]["turn"], {"id": turn, "status": "interrupted"})
        self.assertIsNone(self.broker.external_turn)

    def test_native_codex_tool_reply_is_unchanged(self):
        self.broker._handle_client(b'{"id":1,"method":"initialize","params":{"capabilities":{"experimentalApi":true}}}')
        self.broker._inspect_server(json.dumps({"id": 7, "method": "item/tool/call", "params": {
            "threadId": "codex-thread", "turnId": "t", "callId": "c", "tool": "ddistro_read",
            "arguments": {"operation": "status"}}}).encode())
        for _ in range(500):
            if b'"id":7' in self.server.getvalue():
                break
            threading.Event().wait(0.01)
        line = self.server.getvalue().splitlines()[-1]
        self.assertTrue(line.startswith(b'{"id":7,"result":{"success":true,"contentItems":[{"type":"inputText"'))
        self.assertNotIn(b'"id":7', self.client.getvalue())


SECRET = "sk-or-v1-fixture-NOT-A-REAL-KEY-0123456789"
KEY_ID = "3074ec1c-0000-4000-8000-000000000001"
OTHER_KEY = "773ff307-0000-4000-8000-000000000002"


def text_chunks(text):
    return [{"choices": [{"delta": {"content": text[:4]}}]}, {"choices": [{"delta": {"content": text[4:]}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}]


def tool_chunks(name="ddistro_read", arguments='{"operation":"status"}', call_id="call_a"):
    return [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                    "function": {"name": name, "arguments": arguments[:5]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[5:]}}]},
                          "finish_reason": "tool_calls"}]}]


class OpenRouterEngine(unittest.TestCase):
    """Core's own OpenRouter loop with a mocked key resolver and transport: no key read and no network."""
    send, messages, wait_for, reply = ExternalEngine.send, ExternalEngine.messages, ExternalEngine.wait_for, \
        ExternalEngine.reply

    def setUp(self):
        self.handled, self.scripts, self.bodies, self.secrets, self.resolved = [], [], [], [], []

        def run_tool(argv):
            self.handled.append(argv)
            return True, {"probe": "fixture"}

        def resolve(key_id, turn):
            self.resolved.append(key_id)
            if key_id != KEY_ID:
                raise agent.ProviderError("key_missing", "The selected OpenRouter key no longer exists.")
            return SECRET, {"keyId": key_id, "revision": 1, "label": "fixture"}

        def stream(body, secret, turn):
            self.bodies.append(json.loads(body))
            self.secrets.append(secret)
            script = self.scripts.pop(0) if self.scripts else tool_chunks()
            if isinstance(script, Exception):
                raise script
            yield from (script(turn) if callable(script) else script)

        self.threads = agent.ThreadStore(None)
        self.threads.ids = ["codex-thread"]
        self.server, self.client = io.BytesIO(), io.BytesIO()
        self.broker = agent.Broker(types.SimpleNamespace(stdin=self.server), None, self.client, self.threads,
                                   run_tool=run_tool, openrouter_io=(resolve, stream))
        self.seq = 0

    def open_chat(self, actions=()):
        capabilities = {"experimentalApi": True, "ddistroEngine": "openrouter"}
        if actions:
            capabilities["ddistroActions"] = list(actions)
        self.send("initialize", {"capabilities": capabilities})
        started = self.reply(self.send("ddistro/external/thread/start", {"engine": "openrouter"}))["result"]
        self.assertEqual([m["id"] for m in started["models"]],
                         ["z-ai/glm-5.3", "openai/gpt-6.1-sol", "anthropic/claude-sonnet-5.5"])
        self.assertTrue(all(m["catalog"]["tools"] and m["runtimeTested"] is False for m in started["models"]))
        self.assertEqual([m["catalog"]["images"] for m in started["models"]], [False, True, True])
        return started["threadId"]

    def turn(self, thread, text="Check my distro", **changes):
        params = {"threadId": thread, "model": "openai/gpt-6.1-sol", "keyId": KEY_ID,
                  "input": [{"type": "text", "text": text}]}
        params.update(changes)
        return self.send("ddistro/external/turn/start", params)

    def completed(self, turn_id):
        return self.wait_for(lambda m: m.get("method") == "turn/completed"
                             and m["params"]["turn"]["id"] == turn_id)["params"]["turn"]

    def run_turn(self, thread, **changes):
        turn_id = self.reply(self.turn(thread, **changes))["result"]["turn"]["id"]
        return turn_id, self.completed(turn_id)

    def test_text_and_tool_round_trip_history_and_credential_isolation(self):
        thread = self.open_chat()
        self.scripts = [tool_chunks(), text_chunks("All services passed.")]
        turn_id, done = self.run_turn(thread)
        self.assertEqual(done, {"id": turn_id, "status": "completed"})
        tool = self.wait_for(lambda m: m.get("method") == "item/completed"
                             and m["params"]["item"]["type"] == "dynamicToolCall")["params"]["item"]
        self.assertEqual((tool["id"], tool["tool"], tool["success"], tool["status"]),
                         ("call_a", "ddistro_read", True, "completed"))
        self.assertEqual(len(self.handled), 1)
        # The model receives exactly the handler's result, as Codex would.
        self.assertEqual(self.bodies[1]["messages"][-1], {"role": "tool", "tool_call_id": "call_a",
                                                         "content": tool["contentItems"][0]["text"]})
        self.assertEqual(json.loads(tool["contentItems"][0]["text"])["tool"], "ddistro_read")
        deltas = "".join(m["params"]["delta"] for m in self.messages()
                         if m.get("method") == "item/agentMessage/delta")
        self.assertEqual(deltas, "All services passed.")
        first = self.bodies[0]
        self.assertEqual((first["model"], first["messages"][0]["role"]), ("openai/gpt-6.1-sol", "system"))
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["ddistro_read"])
        self.assertEqual(first["tools"][0]["function"]["parameters"], agent.TOOL_SPEC["inputSchema"])
        self.assertNotIn("models", first)  # never a fallback list
        self.assertEqual(self.secrets, [SECRET, SECRET])
        stored = json.dumps(self.threads.memory_histories)
        for blob in (self.client.getvalue().decode(), self.server.getvalue().decode(), stored,
                     json.dumps(self.bodies)):
            self.assertNotIn(SECRET, blob)
        self.assertNotIn(b'"turn/', self.server.getvalue())  # Codex never ran a turn
        page = self.reply(self.send("ddistro/external/thread/turns/list", {"threadId": thread}))["result"]
        self.assertEqual([item["type"] for item in page["data"][0]["items"]],
                         ["userMessage", "dynamicToolCall", "agentMessage"])
        self.assertEqual((page["nextCursor"], page["compacted"], page["data"][0]["status"]), (None, 0, "completed"))
        self.scripts = [tool_chunks(), text_chunks("Still fine.")]
        second = self.run_turn(thread, text="And now?")[0]
        self.assertEqual([m["role"] for m in self.bodies[2]["messages"]],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        # call_a is already in the replayed history, so this turn's call gets an id no other turn can mint.
        replayed = [m["tool_call_id"] for m in self.bodies[3]["messages"] if m["role"] == "tool"]
        self.assertEqual(replayed[0], "call_a")
        self.assertTrue(agent.EXTERNAL_CALL_ID.match(replayed[1]) and replayed[1].startswith("call_" + second))
        self.assertEqual(len(self.reply(self.send("ddistro/external/thread/turns/list",
                                                  {"threadId": thread, "limit": 1}))["result"]["data"]), 1)

    def test_approved_action_result_reaches_the_model_through_the_same_handler(self):
        restarts = []
        self.broker.run_tool = lambda argv: (restarts.append(argv), (True, {"executed": True, "verified": True,
                                                                           "state": "verified"}))[1]
        thread = self.open_chat(actions=["restart_service"])
        service = next(iter(agent.ACTION_SERVICES))
        arguments = json.dumps({"operation": "propose_restart", "service": service, "reason": "fixture"})
        self.scripts = [tool_chunks(name="ddistro_action", arguments=arguments), text_chunks("Restarted.")]
        turn_id = self.reply(self.turn(thread))["result"]["turn"]["id"]
        proposal = self.wait_for(lambda m: m.get("method") == "ddistro/action/proposed")["params"]
        self.assertEqual(len(self.bodies), 1)  # the loop waits for the card
        self.reply(self.send("ddistro/action/decide", {"proposalId": proposal["proposalId"], "threadId": thread,
                                                       "decision": "approve"}))
        self.assertEqual(self.completed(turn_id)["status"], "completed")
        self.assertEqual([argv[:2] for argv in restarts if argv[:2] == ["service", "restart"]],
                         [["service", "restart"]])
        result = json.loads(self.bodies[1]["messages"][-1]["content"])
        self.assertEqual(result["tool"], "ddistro_action")
        self.assertNotIn(SECRET, self.client.getvalue().decode())

    def test_client_results_bad_requests_and_other_engines_are_refused(self):
        thread = self.open_chat()
        for engine in ("claude", None):
            capabilities = {"experimentalApi": True, "ddistroEngine": engine}
            self.assertEqual(self.reply(self.send("initialize", {"capabilities": capabilities}))["error"]["code"],
                             -32602)
        self.assertEqual(self.broker.engine, "openrouter")
        self.send("initialize", {"capabilities": {"experimentalApi": True, "ddistroEngine": "openrouter"}})
        self.assertTrue(self.broker.experimental)
        for method, params in (("ddistro/external/tool/call", {"threadId": thread, "turnId": "t", "callId": "c",
                                                               "tool": "ddistro_read", "arguments": {}}),
                               ("ddistro/external/turn/end", {"threadId": thread, "turnId": "t",
                                                              "status": "completed"})):
            self.assertEqual(self.reply(self.send(method, params))["error"]["code"], -32601)
        image = [{"type": "text", "text": "What is this?"},
                 {"type": "image", "url": "data:image/png;base64,iVBORw0KGgo="}]
        for change in ({"model": "openai/gpt-4o"}, {"model": "google/gemma-4-31b-it"},
                       {"model": "z-ai/glm-5.3", "input": image}, {"keyId": "not-a-uuid"},
                       {"endpoint": "https://example.com"},
                       {"input": [{"type": "localImage", "path": "/etc/shadow"}]},
                       {"input": [{"type": "image", "url": "https://example.com/x.png"}]},
                       {"input": [{"type": "text", "text": ""}]}):
            self.assertEqual(self.reply(self.turn(thread, **change))["error"]["code"], -32602)
        claude = "00000000-0000-4000-8000-00000000c1a0"
        self.threads.external[claude] = "claude"
        for other in (claude, "codex-thread"):
            self.assertIn("error", self.reply(self.turn(other)))
            self.assertIn("error", self.reply(self.send("ddistro/external/thread/turns/list", {"threadId": other})))
        self.assertEqual((self.resolved, self.bodies), ([], []))
        # Undeclared tools and non-object arguments are answered in Core and reach no handler.
        self.scripts = [tool_chunks(name="shell", arguments='{"operation": "status"}'),
                        tool_chunks(arguments="[1,2]", call_id="call_b"),
                        tool_chunks(arguments='{"op', call_id="call_c"), tool_chunks(arguments="null", call_id="call_d"),
                        text_chunks("Done.")]
        self.assertEqual(self.run_turn(thread)[1]["status"], "completed")
        failed = [m["params"]["item"] for m in self.messages() if m.get("method") == "item/completed"
                  and m["params"]["item"]["type"] == "dynamicToolCall"]
        self.assertEqual([json.loads(i["contentItems"][0]["text"])["error"]["code"] for i in failed],
                         ["invalid_arguments"] * 4)
        self.assertEqual((self.handled, self.broker.session_calls), ([], 0))
        # Only JSON objects are sent back as tool-call arguments, later in the turn and in the next turn's history.
        self.scripts = [text_chunks("Ok.")]
        self.assertEqual(self.run_turn(thread)[1]["status"], "completed")
        for body in (self.bodies[4], self.bodies[5]):
            sent = [call["function"]["arguments"] for m in body["messages"] for call in m.get("tool_calls", ())]
            self.assertEqual(sent, ['{"operation":"status"}', "{}", "{}", "{}"])

    def test_key_http_and_stream_failures_are_clear_and_never_retried(self):
        thread = self.open_chat()
        done = self.run_turn(thread, keyId=OTHER_KEY)[1]
        self.assertEqual((done["status"], done["error"]["code"], self.bodies), ("failed", "key_missing", []))
        cases = ((agent.provider_message(401, json.dumps({"error": {"message": "bad key " + SECRET}}).encode(),
                                         SECRET), "provider_auth"),
                 ([{"error": {"message": "upstream failed"}}], "provider_error"),
                 ([{"choices": [{"delta": {"content": "partial"}}]}], "incomplete_response"),
                 ([{"choices": [{"delta": {"content": "cut off"}, "finish_reason": "length"}]}], "provider_finish"),
                 ([{"choices": [{"delta": {"tool_calls": [{"index": 99}]}}]}], "malformed_response"))
        for script, code in cases:
            before = len(self.bodies)
            self.scripts = [script]
            done = self.run_turn(thread)[1]
            self.assertEqual((done["status"], done["error"]["code"]), ("failed", code))
            self.assertEqual(len(self.bodies), before + 1)
        self.assertNotIn(SECRET, self.client.getvalue().decode())
        self.assertIn("[redacted]", self.client.getvalue().decode())
        self.assertTrue(self.wait_for(lambda m: m.get("method") == "item/completed"
                                      and m["params"]["item"].get("text") == "cut off"))
        page = self.reply(self.send("ddistro/external/thread/turns/list", {"threadId": thread}))["result"]
        self.assertEqual(page["data"][0]["error"]["code"], "malformed_response")

    def test_stop_closes_blocked_provider_io_before_releasing_the_turn(self):
        thread = self.open_chat()
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        reading = threading.Event()

        def blocked(turn):
            turn.attach(types.SimpleNamespace(sock=reader))
            reading.set()
            reader.recv(1)  # returns only once Stop shuts the socket down
            turn.detach()
            raise agent.ProviderError("network_error", "closed")
            yield

        self.scripts = [blocked]
        turn_id = self.reply(self.turn(thread))["result"]["turn"]["id"]
        self.assertTrue(reading.wait(5))
        self.assertEqual(self.reply(self.turn(thread))["error"]["code"], -32001)  # one active turn
        self.assertFalse([m for m in self.messages() if m.get("method") == "turn/completed"])
        self.assertEqual(self.reply(self.send("turn/interrupt", {"threadId": thread, "turnId": turn_id}))["result"], {})
        self.assertEqual(self.completed(turn_id), {"id": turn_id, "status": "interrupted"})
        self.assertEqual(len(self.bodies), 1)  # an unknown outcome is never replayed
        self.assertIsNone(self.broker.external_turn)
        page = self.reply(self.send("ddistro/external/thread/turns/list", {"threadId": thread}))["result"]
        self.assertEqual(page["data"][0]["status"], "interrupted")

    def test_ignoring_the_turn_limit_until_doubled_interrupts_the_loop(self):
        thread = self.open_chat()
        done = self.run_turn(thread)[1]  # every model round asks for another ddistro_read
        self.assertEqual(done["status"], "interrupted")
        self.assertEqual((self.broker.session_calls, len(self.handled)),
                         (2 * agent.TOOL_TURN_CALLS, agent.TOOL_TURN_CALLS))
        self.assertEqual(len(self.bodies), 2 * agent.TOOL_TURN_CALLS)
        self.assertIsNone(self.broker.external_turn)
        self.assertFalse(self.broker.external_calls)

    def test_key_resolution_uses_only_the_selected_openrouter_key(self):
        status = {"ok": True, "data": {"keys": [
            {"key_id": KEY_ID, "provider": "openrouter", "origin": "https://openrouter.ai:443", "revision": 3,
             "configured": True, "label": "Main"},
            {"key_id": OTHER_KEY, "provider": "openai", "origin": "https://api.openai.com:443", "revision": 1,
             "configured": True}]}}
        reveal, calls = {"ok": True, "outcome": "revealed", "data": {"secret": SECRET}}, []

        def fake(verb, payload, turn, deadline):
            calls.append((verb, payload))
            return status if verb == "global-status" else reveal

        with patch.object(agent, "trusted_chain"), patch.object(agent, "keys_call", side_effect=fake):
            secret, meta = agent.openrouter_key(KEY_ID, None)
            self.assertEqual((secret, calls[-1]), (SECRET, ("global-reveal", {"key_id": KEY_ID, "revision": 3})))
            self.assertNotIn(SECRET, json.dumps(meta))
            for key_id, code in ((OTHER_KEY, "key_wrong_provider"), ("00000000-0000-4000-8000-000000000000",
                                                                     "key_missing")):
                with self.assertRaises(agent.ProviderError) as error:
                    agent.openrouter_key(key_id, None)
                self.assertEqual(error.exception.code, code)
            reveal = {"ok": False, "outcome": "conflict", "data": None}
            with self.assertRaises(agent.ProviderError) as error:
                agent.openrouter_key(KEY_ID, None)
            self.assertEqual(error.exception.code, "key_changed")

    def test_key_helper_group_stops_with_the_turn_and_at_the_deadline(self):
        # A slow fixture helper with a grandchild stands in for ddistro_keys; no real key or installer is used.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        helper = Path(root, "keys")
        helper.write_text("#!/bin/sh\necho \"$@\" > %s/argv\ncat > %s/stdin\nsleep 60 &\necho $! > %s/pids\n"
                          "echo $$ >> %s/pids\nwait\n" % ((root,) * 4))
        helper.chmod(0o755)

        def driver():
            return agent.OpenRouterTurn(None, "t", "u", "m", KEY_ID, {"type": "userMessage", "id": "u:0",
                                                                      "content": []}, None, {"turns": [], "compacted": 0})

        with patch.object(agent, "KEYS_HELPER", str(helper)),                 patch.object(agent.subprocess, "Popen", wraps=agent.subprocess.Popen) as popen:
            for stop_by_cancel in (True, False):
                Path(root, "pids").unlink(missing_ok=True)
                turn, errors = driver(), []

                def run():
                    try:
                        agent.keys_call("global-reveal", {"key_id": KEY_ID, "revision": 3}, turn,
                                        agent.time.monotonic() + (30 if stop_by_cancel else 1))
                    except agent.ProviderError as exc:
                        errors.append(exc.code)
                worker = threading.Thread(target=run)
                started = agent.time.monotonic()
                worker.start()
                for _ in range(500):
                    if len(Path(root, "pids").read_text().split() if Path(root, "pids").exists() else ()) == 2:
                        break
                    threading.Event().wait(0.01)
                if stop_by_cancel:
                    turn.cancel("interrupted")
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertLess(agent.time.monotonic() - started, 5)
                self.assertEqual(errors, ["cancelled" if stop_by_cancel else "key_unavailable"])
                self.assertIsNone(turn.child)
                for pid in map(int, Path(root, "pids").read_text().split()):
                    for _ in range(200):  # the orphaned grandchild is reaped by init
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        threading.Event().wait(0.01)
                    else:
                        self.fail("a key helper process outlived the lookup")
        self.assertEqual(Path(root, "argv").read_text().split(), ["global-reveal"])
        self.assertIn(KEY_ID, Path(root, "stdin").read_text())
        options = popen.call_args[1]
        self.assertTrue(options["start_new_session"])
        self.assertEqual(set(options["env"]), {"PATH", "LANG"})

    def test_stop_while_connecting_sends_no_request(self):
        thread, requests, broker = self.open_chat(), [], self.broker

        class Connection:
            def __init__(self, *args, **kwargs):
                self.sock = None

            def connect(self):
                broker.external_turn["driver"].cancel("interrupted")  # Stop lands while the socket opens
                self.sock = types.SimpleNamespace(settimeout=lambda value: None)

            def request(self, *args):
                requests.append(args)

            def close(self):
                pass

        self.scripts = [lambda turn: agent.openrouter_stream(b"{}", SECRET, turn)]
        with patch.object(agent.http.client, "HTTPSConnection", Connection):
            turn_id, done = self.run_turn(thread)
        self.assertEqual((done, requests), ({"id": turn_id, "status": "interrupted"}, []))

    def test_transport_is_fixed_never_redirects_and_rejects_malformed_streams(self):
        connections = []

        class Response:
            def __init__(self, status, ctype, lines):
                self.status, self.ctype, self.lines = status, ctype, list(lines)

            def read(self, limit):
                return b'{"error":{"message":"moved"}}'

            def getheader(self, name):
                return self.ctype

            def readline(self, limit):
                return self.lines.pop(0) if self.lines else b""

        class Connection:
            def __init__(self, host, port, timeout=None, context=None):
                self.host, self.port, self.sock = host, port, types.SimpleNamespace(settimeout=lambda value: None)
                connections.append(self)

            def connect(self):
                pass

            def request(self, method, path, body, headers):
                self.sent = (method, path, headers)

            def getresponse(self):
                return self.response_value

            def close(self):
                pass

        turn = types.SimpleNamespace(attach=lambda conn: True, detach=lambda: None, lock=threading.Lock(),
                                     cancelled=None)

        def collect(response):
            with patch.object(agent.http.client, "HTTPSConnection",
                              side_effect=lambda *a, **k: setattr(Connection(*a, **k), "response_value", response)
                              or connections[-1]):
                return list(agent.openrouter_stream(b"{}", SECRET, turn))

        chunks = collect(Response(200, "text/event-stream", [b": OPENROUTER PROCESSING\n", b"\n",
                                                              b'data: {"choices":[]}\n', b"data: [DONE]\n"]))
        self.assertEqual(chunks, [{"choices": []}])
        conn = connections[-1]
        self.assertEqual((conn.host, conn.port, conn.sent[0], conn.sent[1]),
                         ("openrouter.ai", 443, "POST", "/api/v1/chat/completions"))
        self.assertEqual(conn.sent[2]["Authorization"], "Bearer " + SECRET)
        for response, code in ((Response(302, "text/html", []), "provider_redirect"),
                               (Response(200, "application/json", []), "malformed_response"),
                               (Response(200, "text/event-stream", [b"data: {not json\n"]), "malformed_response")):
            count = len(connections)
            with self.assertRaises(agent.ProviderError) as error:
                collect(response)
            self.assertEqual((error.exception.code, len(connections)), (code, count + 1))
            self.assertNotIn(SECRET, error.exception.message)

    def test_history_file_is_root_private_atomic_bounded_and_removed(self):
        real_fstat = os.fstat

        def as_root(fd):
            st = real_fstat(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, st.st_gid, st.st_size,
                                   int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))

        with tempfile.TemporaryDirectory() as root, \
                patch.object(agent, "open_root_dir", side_effect=lambda *a, **k: os.open(root, os.O_RDONLY)), \
                patch.object(agent.os, "fstat", side_effect=as_root):
            store = agent.ThreadStore("task-fixture")
            thread, other, claude = (str(agent.uuid.uuid4()) for _ in range(3))
            for thread_id, engine in ((thread, "openrouter"), (other, "openrouter"), (claude, "claude")):
                store.add_external(thread_id, engine)
            history = store.load_history(thread)
            item = {"type": "agentMessage", "id": "x", "text": "y" * 1000}
            history["turns"] = [{"id": str(agent.uuid.uuid4()), "status": "completed", "model": "google/gemma-4-31b-it",
                                 "error": None, "startedAt": 1, "completedAt": 2, "items": [item]}
                                for _ in range(agent.OPENROUTER_HISTORY_TURNS + 5)]
            store.save_history(thread, history)
            path = Path(root, "task-fixture." + thread + agent.OPENROUTER_HISTORY_SUFFIX)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = store.load_history(thread)
            self.assertEqual((len(loaded["turns"]), loaded["compacted"]), (agent.OPENROUTER_HISTORY_TURNS, 5))
            with patch.object(agent, "OPENROUTER_HISTORY_LIMIT", 20000):
                store.save_history(thread, loaded)
            self.assertLessEqual(path.stat().st_size, 20000)
            self.assertGreater(store.load_history(thread)["compacted"], 5)
            self.assertFalse([p for p in Path(root).iterdir() if p.name.endswith(".tmp")])
            os.chmod(path, 0o644)
            with self.assertRaises(agent.Refusal):
                store.load_history(thread)
            os.chmod(path, 0o600)
            os.link(path, Path(root, "second-link"))
            with self.assertRaises(agent.Refusal):
                store.load_history(thread)
            os.unlink(Path(root, "second-link"))
            target = Path(root, "target")
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaises(agent.Refusal):
                store.load_history(thread)
            path.unlink()
            target.rename(path)
            foreign = Path(root, "task-fixture." + other + agent.OPENROUTER_HISTORY_SUFFIX)
            foreign.write_bytes(path.read_bytes())
            os.chmod(foreign, 0o600)
            for thread_id in (other, claude):  # another thread's transcript, or an engine with none in Core
                with self.assertRaises(agent.Refusal):
                    store.load_history(thread_id)
            store.remove_external(thread)
            self.assertFalse(path.exists())


class ExternalReservation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(api, "ROOT", self.temp.name)
        self.root.start()
        self.app = api.App.__new__(api.App)
        self.app.store = api.Store()
        self.app.lock = threading.Condition(threading.RLock())
        self.started = []
        self.app.launcher_turn = lambda session, message: self.started.append(message)
        self.handler = api.Handler.__new__(api.Handler)
        self.handler.server = types.SimpleNamespace(app=self.app)
        self.cid = "20000000-0000-0000-0000-000000000000"
        self.app.store.write("INSERT INTO conversations VALUES(?,?,?,?,NULL,?)", (self.cid, "launcher", "task", "launcher", 1))
        self.emitted, self.calls = [], []
        self.session = types.SimpleNamespace(closed=False, active=False, queued=False, queue_cancel=False,
            external=False, attached=True, emit=self.emitted.append,
            interrupt_external=lambda: self.calls.append("interrupt"), close=lambda: self.calls.append("close"))
        self.app.sessions = {self.cid: self.session}

    def tearDown(self):
        self.app.store.db.close()
        self.root.stop()
        self.temp.cleanup()

    def post(self, suffix, body):
        return self.handler.route("POST", "/v1/conversations/" + self.cid + suffix, body, "launcher", "launcher")

    def test_external_start_queues_like_turn_start_and_queued_stop_cancels(self):
        start = {"id": 5, "method": "ddistro/external/turn/start", "params": {"threadId": "x"}}
        self.assertEqual(self.post("/messages", {"message": start})["state"], "queued")
        threading.Event().wait(0.05)
        self.assertEqual(self.started, [start])
        self.assertTrue(self.session.external and self.session.queued)
        with self.assertRaises(api.ApiError):
            self.post("/messages", {"message": start})
        self.post("/messages", {"message": {"id": 6, "method": "turn/interrupt", "params": {}}})
        self.assertTrue(self.session.queue_cancel)
        self.assertEqual(self.emitted, [{"id": 6, "result": {}}])

    def test_watchdog_interrupts_only_a_silent_external_turn(self):
        class Stop(Exception):
            pass
        stopped = []
        def worker(name, external, age):
            return types.SimpleNamespace(active=True, external=external, jobs=set(), queued=False, attached=True,
                                         last=api.time.monotonic() - age,
                                         interrupt_external=lambda: stopped.append(name))
        self.app.sessions = {"silent": worker("silent", True, api.EXTERNAL_SILENCE + 5),
                             "polling": worker("polling", True, 1), "codex": worker("codex", False, 999)}
        with patch.object(api.time, "sleep", side_effect=[None, Stop()]):
            with self.assertRaises(Stop):
                self.app.reap_idle()
        self.assertEqual(stopped, ["silent"])

    def test_detach_interrupts_before_close(self):
        self.post("/detach", {})
        self.assertEqual(self.calls, ["interrupt", "close"])
        self.assertTrue(self.session.queue_cancel)

    def test_worker_interrupts_a_detached_external_turn_and_releases(self):
        read, write = os.pipe()
        stdin, released = io.BytesIO(), []
        proc = types.SimpleNamespace(stdin=stdin, stdout=os.fdopen(read, "rb"), wait=lambda *args: 0)
        app = types.SimpleNamespace(owner=types.SimpleNamespace(fd=0), store=types.SimpleNamespace(write=lambda *a: 1),
                                    release=released.append)
        with patch.object(api.subprocess, "Popen", return_value=proc):
            session = api.Session(app, {"task": "t", "kind": "launcher", "thread": None})
        session.active, session.external, session.turn_done = True, True, False
        session.interrupt_external()  # detached before turn/started: sent once the turn is known
        self.assertEqual(stdin.getvalue(), b"")
        os.write(write, b'{"method":"turn/started","params":{"threadId":"th","turn":{"id":"tu"}}}\n')
        for _ in range(500):
            if stdin.getvalue():
                break
            threading.Event().wait(0.01)
        sent = json.loads(stdin.getvalue())
        self.assertEqual((sent["method"], sent["params"]), ("turn/interrupt", {"threadId": "th", "turnId": "tu"}))
        os.write(write, b'{"method":"turn/completed","params":{"threadId":"th","turn":{"id":"tu","status":"interrupted"}}}\n')
        os.close(write)
        for _ in range(500):
            if released:
                break
            threading.Event().wait(0.01)
        self.assertFalse(session.active)
        self.assertEqual(session.final_state, "cancelled")
        self.assertIn(session, released)


class Maintenance(unittest.TestCase):
    def test_busy_shutdown_does_not_run_update(self):
        with tempfile.TemporaryDirectory() as root, patch.object(api, "prepare_root"), \
             patch.object(api, "LIFECYCLE", root + "/gate"), \
             patch.object(api, "local_call", side_effect=api.ApiError(409, "request_running")), \
             patch.object(api.subprocess, "run") as run:
            with self.assertRaises(api.ApiError) as error:
                api.maintenance(["/bin/true"])
            self.assertEqual(error.exception.status, 409)
            run.assert_not_called()

    def test_maintenance_blocks_auto_connect_without_replacing_lock(self):
        with tempfile.TemporaryDirectory() as root, patch.object(api, "prepare_root"), \
             patch.object(api, "LIFECYCLE", root + "/gate"):
            with api.lifecycle(exclusive=True):
                inode = os.stat(api.LIFECYCLE).st_ino
                with self.assertRaises(api.ApiError) as error:
                    with api.lifecycle():
                        self.fail("Auto-connect entered maintenance")
                self.assertEqual(error.exception.code, "maintenance_running")
            with api.lifecycle():
                self.assertEqual(os.stat(api.LIFECYCLE).st_ino, inode)

    def test_background_jobs_prevent_idle_shutdown(self):
        app = types.SimpleNamespace(lock=threading.RLock(), current=None, queue=[],
            store=types.SimpleNamespace(rows=lambda *args: []),
            sessions={"idle": types.SimpleNamespace(jobs={"live-job"}, active=False, queued=False)})
        handler = api.Handler.__new__(api.Handler)
        handler.server = types.SimpleNamespace(app=app)
        with self.assertRaises(api.ApiError) as error:
            handler.route("POST", "/v1/admin/shutdown", {}, "launcher", "launcher")
        self.assertEqual(error.exception.code, "request_running")

    def test_accepted_game_request_is_protected_before_worker_reserves(self):
        app = types.SimpleNamespace(lock=threading.RLock(), current=None, queue=[], sessions={},
            store=types.SimpleNamespace(rows=lambda *args: [{"id": "accepted-but-not-scheduled"}]))
        handler = api.Handler.__new__(api.Handler)
        handler.server = types.SimpleNamespace(app=app)
        with self.assertRaises(api.ApiError) as error:
            handler.route("POST", "/v1/admin/shutdown", {}, "launcher", "launcher")
        self.assertEqual(error.exception.code, "request_running")

    def test_stale_daemon_is_not_treated_as_ready(self):
        with tempfile.TemporaryDirectory() as root, patch.object(api, "prepare_root"), \
             patch.object(api, "LIFECYCLE", root + "/gate"), patch.object(api, "SELF", __file__), \
             patch.object(api, "local_call", return_value=(types.SimpleNamespace(close=lambda: None),
                 io.BytesIO(b'{"version":1,"generation":"old"}'))):
            with self.assertRaises(api.ApiError) as error:
                api.ensure()
            self.assertEqual(error.exception.code, "api_update_required")


class AuthImport(unittest.TestCase):
    def run_import(self, home, raw, busy=False, check=False):
        identity = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        def lock():
            if busy:
                raise agent.Refusal("busy")
            return types.SimpleNamespace(fd=os.open(home + "/lock", os.O_RDWR | os.O_CREAT, 0o600))
        output = io.StringIO()
        with patch.object(agent, "require_root"), patch.object(agent, "agent_identity", return_value=identity), \
             patch.object(agent, "state_layout"), patch.object(agent, "SingletonLock", side_effect=lock), \
             patch.object(agent, "open_account_home", side_effect=lambda _: os.open(home, os.O_RDONLY)), \
             patch.object(agent, "no_agent_processes", return_value=True), \
             patch.object(agent.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(raw))), \
             patch.object(agent.sys, "stdout", output):
            self.assertEqual(agent.cmd_auth_import(check=check), 0)
        self.assertNotIn("refresh-secret", output.getvalue())
        return json.loads(output.getvalue())["state"]

    def test_read_only_check_does_not_need_idle_server(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_import(home, b"", busy=True, check=True), "empty")
            self.assertFalse(Path(home, "auth.json").exists())
            Path(home, "auth.json").write_bytes(b"preserve")
            self.assertEqual(self.run_import(home, b"", busy=True, check=True), "existing")

    def test_atomic_private_import_and_no_clobber(self):
        raw = json.dumps({"tokens": {"access_token": "h.eyJleHAiOjQxMDI0NDQ4MDB9.s",
            "refresh_token": "refresh-secret", "id_token": "id", "account_id": "account"},
            "unexpected": "never-import"}).encode()
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.run_import(home, raw), "imported")
            auth = Path(home, "auth.json")
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            saved = auth.read_bytes()
            self.assertNotIn(b"never-import", saved)
            self.assertEqual(self.run_import(home, b"{}"), "existing")
            self.assertEqual(auth.read_bytes(), saved)
            self.assertFalse(any(p.name.startswith(".auth-import-") for p in Path(home).iterdir()))

    def test_destination_symlink_preserved(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home, "other"); target.write_bytes(b"preserve")
            Path(home, "auth.json").symlink_to(target)
            self.assertEqual(self.run_import(home, b"{}"), "existing")
            self.assertEqual(target.read_bytes(), b"preserve")

    def test_invalid_oversized_and_busy_leave_empty_account(self):
        with tempfile.TemporaryDirectory() as home:
            for raw in (b"{}", b"invalid", b"x" * 65537):
                self.assertEqual(self.run_import(home, raw), "skipped")
                self.assertFalse(Path(home, "auth.json").exists())
            self.assertEqual(self.run_import(home, b"{}", busy=True), "skipped")
            self.assertFalse(Path(home, "auth.json").exists())


class SharedProviderKeys(unittest.TestCase):
    def setUp(self):
        self.keys = load("ddistro_keys")

    def test_old_sync_hook_never_copies_credentials(self):
        with patch.object(self.keys, "trusted_file", side_effect=AssertionError("no service access")):
            self.assertEqual(self.keys.run("sync", {}), {"ok": True, "outcome": "references_only", "warnings": []})

    def test_retired_copy_operations_are_refused(self):
        for operation in ("set-shared", "set-override", "use-shared", "remove-shared"):
            with self.assertRaises(self.keys.Refusal):
                self.keys.run(operation, {"key": "private-fixture"})

    def test_environment_is_literal_and_does_not_inherit(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("CHIM_DB_NAME=fixture\nexport CHIM_DB_PASSWORD='$(not-executed)'\nPATH=/untrusted\n")
            with patch.object(self.keys, "ENV_FILES", (str(path),)), patch.object(self.keys, "trusted_file"):
                result = self.keys.environment()
            self.assertEqual(result["CHIM_DB_PASSWORD"], "$(not-executed)")
            self.assertEqual(result["CHIM_DB_NAME"], "fixture")
            self.assertNotEqual(result["PATH"], "/untrusted")



if __name__ == "__main__":
    unittest.main()
