# Local Atlas assistant runtime (FD-0002 section 7).

import hashlib
import json
import os
import secrets
import threading
import time

from .apply import ApplyPipeline, Proposal
from .memory import MachineMemory, RagIndex, kb_documents
from .model import answer_question, interpret_incident, propose_config_edit
from .model.assistant import config_excerpt


ASSISTANT_SCHEMA_VERSION = 1
DEFAULT_MAX_QUESTION = 4096
DEFAULT_MAX_CONFIG_BYTES = 2 * 1024 * 1024
DEFAULT_PROPOSAL_TTL = 15 * 60
DEFAULT_MAX_HISTORY_MESSAGES = 8
DEFAULT_MAX_HISTORY_CHARS = 8 * 1024
DEFAULT_MAX_QUEUE = 2
_KEEP_MEMORY = object()


def _change_dict(change):
    return {
        "section": change.section,
        "key": change.key,
        "op": change.op,
        "old": change.old,
        "new": change.new,
    }


class AssistantRuntime:
    """Serialize local inference and enforce deterministic proposal gates.

    The runtime deliberately exposes draft/preview, not live mutation.  A
    proposal token binds the exact before/after texts and expires quickly;
    the eventual machine-side apply seam can consume that token without ever
    asking the model to decide risk.
    """

    def __init__(self, backend, patterns=None, memory=None,
                 config_path=None, allow_stub=False, wall_clock=time.time,
                 proposal_ttl=DEFAULT_PROPOSAL_TTL,
                 max_queue=DEFAULT_MAX_QUEUE):
        if not backend.available():
            raise RuntimeError("configured model backend is unavailable")
        if getattr(backend, "name", "") == "stub" and not allow_stub:
            raise RuntimeError("stub backend cannot serve the assistant")
        self.backend = backend
        self.config_path = (os.path.abspath(os.path.expanduser(config_path))
                            if config_path else None)
        self.clock = wall_clock or time.time
        self.proposal_ttl = proposal_ttl
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.max_queue = max_queue
        self._active = False
        self._queued = 0
        self._current_operation = ""
        self._current_started_at = None
        self._proposals = {}
        self._request_count = 0
        self._failure_count = 0
        self._rejected_count = 0
        self._error_counts = {}
        self._latency_total = 0.0
        self._latency_max = 0.0
        self._last_latency = None
        self._proposals_issued = 0
        self._proposals_expired = 0
        self._last_error = ""
        self._last_completed_at = None
        self.memory = memory
        self.update_grounding(patterns or [], memory)

    def update_grounding(self, patterns, memory=_KEEP_MEMORY):
        if memory is _KEEP_MEMORY:
            memory = self.memory
        if memory is not None and not isinstance(memory, MachineMemory):
            raise TypeError("memory must be MachineMemory")
        self.memory = memory
        self.rag = RagIndex().build(kb_documents(patterns, memory))

    def status(self):
        self._purge_proposals()
        with self._state_lock:
            request_count = self._request_count
            completed = max(0, request_count - self._failure_count)
            backend_status = self.backend.status()
            return {
                "enabled": True,
                "backend": "%s:%s" % (
                    self.backend.name, self.backend.accelerator),
                "model": (os.path.basename(self.backend.model_path)
                          if getattr(self.backend, "model_path", None) else ""),
                "grounding": self.rag.status(),
                "grounding_documents": len(self.rag),
                "machine_memory": self.memory is not None,
                "request_count": request_count,
                "failure_count": self._failure_count,
                "rejected_count": self._rejected_count,
                "error_counts": dict(self._error_counts),
                "busy": self._active,
                "queue_depth": self._queued,
                "queue_limit": self.max_queue,
                "current_operation": self._current_operation,
                "current_started_at": self._current_started_at,
                "latency_seconds": {
                    "last": self._last_latency,
                    "average": (self._latency_total / completed
                                if completed else None),
                    "maximum": self._latency_max if completed else None,
                },
                "model_runtime": backend_status,
                "last_completed_at": self._last_completed_at,
                "last_error": self._last_error,
                "config_preview": self.config_path is not None,
                "proposals": {
                    "active": len(self._proposals),
                    "issued": self._proposals_issued,
                    "expired": self._proposals_expired,
                },
                "live_apply": False,
                "live_apply_status": (
                    "not wired; board-rig qualification required"),
            }

    def _validate_question(self, value):
        if not isinstance(value, str):
            raise ValueError("question must be a string")
        value = value.strip()
        if not value:
            raise ValueError("question must not be empty")
        if len(value) > DEFAULT_MAX_QUESTION:
            raise ValueError("question exceeds %d characters"
                             % DEFAULT_MAX_QUESTION)
        return value

    def _read_config(self):
        if self.config_path is None:
            raise RuntimeError("config preview is not configured")
        size = os.path.getsize(self.config_path)
        if size > DEFAULT_MAX_CONFIG_BYTES:
            raise RuntimeError("config exceeds %d bytes"
                               % DEFAULT_MAX_CONFIG_BYTES)
        with open(self.config_path, encoding="utf-8") as handle:
            return handle.read()

    def _config_context(self, request=""):
        if self.config_path is None:
            return None
        return config_excerpt(self._read_config(), request=request)

    def _validate_history(self, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("history must be an array")
        if len(value) > DEFAULT_MAX_HISTORY_MESSAGES:
            raise ValueError("history exceeds %d messages"
                             % DEFAULT_MAX_HISTORY_MESSAGES)
        clean = []
        total = 0
        for message in value:
            if not isinstance(message, dict):
                raise ValueError("history messages must be objects")
            role = message.get("role")
            content = message.get("content")
            if role not in ("operator", "atlas"):
                raise ValueError("history role must be operator or atlas")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("history content must be a non-empty string")
            content = content.strip()
            total += len(content)
            if total > DEFAULT_MAX_HISTORY_CHARS:
                raise ValueError("history exceeds %d characters"
                                 % DEFAULT_MAX_HISTORY_CHARS)
            clean.append({"role": role, "content": content})
        return clean

    def _purge_proposals(self):
        with self._state_lock:
            now = self.clock()
            retained = {
                key: value for key, value in self._proposals.items()
                if now - value["created_at"] <= self.proposal_ttl
            }
            self._proposals_expired += len(self._proposals) - len(retained)
            self._proposals = retained

    def _proposal_response(self, proposal, result):
        created_at = self.clock()
        digest = hashlib.sha256(
            (proposal.before + "\0" + proposal.after).encode("utf-8"))
        token = "%s.%s" % (secrets.token_urlsafe(18), digest.hexdigest()[:16])
        record = {
            "proposal": proposal, "result": result,
            "created_at": created_at,
        }
        self._purge_proposals()
        with self._state_lock:
            self._proposals[token] = record
            self._proposals_issued += 1
        return {
            "proposal_id": token,
            "created_at": created_at,
            "expires_at": created_at + self.proposal_ttl,
            "rationale": proposal.rationale,
            "tier": result.tier.name.lower(),
            "action": result.action,
            "policy_action": result.action,
            "execution": "preview",
            "needs_confirmation": result.needs_confirmation,
            "applied": False,
            "changes": [_change_dict(c) for c in result.changes],
            "before_sha256": hashlib.sha256(
                proposal.before.encode("utf-8")).hexdigest(),
            "after_sha256": hashlib.sha256(
                proposal.after.encode("utf-8")).hexdigest(),
        }

    def get_proposal(self, token):
        self._purge_proposals()
        with self._state_lock:
            record = self._proposals.get(token)
        if record is None:
            raise ValueError("proposal is unknown or expired")
        return record

    def handle(self, operation, params, timeline):
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        # Status must remain available while a long model inference owns the
        # single accelerator lane.
        if operation == "status":
            return {
                "schema_version": ASSISTANT_SCHEMA_VERSION,
                "operation": operation,
                "result": self.status(),
            }
        queued = False
        with self._state_lock:
            if self._active:
                if self._queued >= self.max_queue:
                    self._rejected_count += 1
                    self._error_counts["queue_full"] = (
                        self._error_counts.get("queue_full", 0) + 1)
                    raise RuntimeError("assistant inference queue is full")
                self._queued += 1
                queued = True
            else:
                self._active = True
        with self._lock:
            started = self.clock()
            with self._state_lock:
                if queued:
                    self._queued -= 1
                self._active = True
                self._current_operation = operation
                self._current_started_at = started
                self._request_count += 1
            try:
                if operation == "ask":
                    question = self._validate_question(params.get("question"))
                    history = self._validate_history(params.get("history"))
                    result = {"answer": answer_question(
                        self.backend, question, timeline, self.rag,
                        history=history,
                        config_context=self._config_context(question)),
                              "read_only": True}
                elif operation == "interpret":
                    result = {"interpretation": interpret_incident(
                        self.backend, timeline, self.rag,
                        structured=bool(params.get("structured", False)),
                        config_context=self._config_context()),
                              "read_only": True}
                elif operation == "propose_config":
                    request = self._validate_question(params.get("request"))
                    before = self._read_config()
                    proposal = propose_config_edit(
                        self.backend, request, before, self.rag)
                    if proposal is None:
                        result = {"proposal": None,
                                  "reason": "model proposed no config edit"}
                    else:
                        gated = ApplyPipeline().preview(proposal)
                        if not gated.validation.ok:
                            result = {"proposal": None,
                                      "reason": "; ".join(
                                          gated.validation.errors)}
                        else:
                            result = {"proposal": self._proposal_response(
                                proposal, gated)}
                else:
                    raise ValueError("unsupported operation %r" % operation)
            except Exception as exc:
                with self._state_lock:
                    self._failure_count += 1
                    error_kind = _error_kind(exc)
                    self._error_counts[error_kind] = (
                        self._error_counts.get(error_kind, 0) + 1)
                    self._last_error = "%s: %s" % (type(exc).__name__, exc)
                raise
            else:
                completed_at = self.clock()
                latency = max(0.0, completed_at - started)
                with self._state_lock:
                    self._last_completed_at = completed_at
                    self._last_error = ""
                    self._last_latency = latency
                    self._latency_total += latency
                    self._latency_max = max(self._latency_max, latency)
                return {
                    "schema_version": ASSISTANT_SCHEMA_VERSION,
                    "operation": operation,
                    "result": result,
                }
            finally:
                with self._state_lock:
                    self._active = self._queued > 0
                    self._current_operation = ""
                    self._current_started_at = None


def _error_kind(exc):
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return "timeout"
    if isinstance(exc, MemoryError) or "out of memory" in text or "oom" in text:
        return "out_of_memory"
    if isinstance(exc, ValueError):
        return "invalid_request"
    return type(exc).__name__.lower()
