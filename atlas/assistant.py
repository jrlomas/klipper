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


ASSISTANT_SCHEMA_VERSION = 1
DEFAULT_MAX_QUESTION = 4096
DEFAULT_MAX_CONFIG_BYTES = 2 * 1024 * 1024
DEFAULT_PROPOSAL_TTL = 15 * 60
DEFAULT_MAX_HISTORY_MESSAGES = 8
DEFAULT_MAX_HISTORY_CHARS = 16 * 1024
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
                 proposal_ttl=DEFAULT_PROPOSAL_TTL):
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
        self._proposals = {}
        self._request_count = 0
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
        return {
            "enabled": True,
            "backend": "%s:%s" % (
                self.backend.name, self.backend.accelerator),
            "model": (os.path.basename(self.backend.model_path)
                      if getattr(self.backend, "model_path", None) else ""),
            "grounding_documents": len(self.rag),
            "request_count": self._request_count,
            "busy": self._lock.locked(),
            "last_completed_at": self._last_completed_at,
            "last_error": self._last_error,
            "config_preview": self.config_path is not None,
            "live_apply": False,
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
        now = self.clock()
        self._proposals = {
            key: value for key, value in self._proposals.items()
            if now - value["created_at"] <= self.proposal_ttl
        }

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
        self._proposals[token] = record
        return {
            "proposal_id": token,
            "created_at": created_at,
            "expires_at": created_at + self.proposal_ttl,
            "rationale": proposal.rationale,
            "tier": result.tier.name.lower(),
            "action": result.action,
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
        record = self._proposals.get(token)
        if record is None:
            raise ValueError("proposal is unknown or expired")
        return record

    def handle(self, operation, params, timeline):
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        with self._lock:
            try:
                if operation == "status":
                    result = self.status()
                elif operation == "ask":
                    question = self._validate_question(params.get("question"))
                    history = self._validate_history(params.get("history"))
                    result = {"answer": answer_question(
                        self.backend, question, timeline, self.rag,
                        history=history),
                              "read_only": True}
                elif operation == "interpret":
                    result = {"interpretation": interpret_incident(
                        self.backend, timeline, self.rag,
                        structured=bool(params.get("structured", False))),
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
                        gated = ApplyPipeline().process(proposal)
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
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
                raise
            else:
                self._request_count += 1
                self._last_completed_at = self.clock()
                self._last_error = ""
                return {
                    "schema_version": ASSISTANT_SCHEMA_VERSION,
                    "operation": operation,
                    "result": result,
                }
