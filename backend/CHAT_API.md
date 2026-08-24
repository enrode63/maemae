# Fund Chat Python API

`fund_chat.ChatOrchestrator` is an API-free, deterministic chat service layer. It does not
provide HTTP/UI or call an LLM. Every accepted operation is appended to a JSONL audit log.

```python
from pathlib import Path
from fund_chat import ChatOrchestrator

# This callback is the only integration boundary to the demo engine. It is invoked only
# after an explicit PM approval. Omit it for audit-only operation.
def apply_to_demo_engine(proposal):
    return {"accepted": True, "reference": proposal["proposal_id"]}

chat = ChatOrchestrator(Path("state/chat-audit.jsonl"), apply_to_demo_engine)
conversation_id = chat.create_conversation()["conversation_id"]

reply = chat.send_message(
    conversation_id=conversation_id,
    request_id="client-request-001",
    content="DEMO 포지션 한도를 낮추는 전략을 검토해줘",
    proposal_kind="strategy",  # "strategy", "code", or omit
)
proposal_id = reply["proposals"][0]["proposal_id"]

# No callback or engine change has happened yet.
decision = chat.decide_proposal(
    conversation_id, proposal_id, "client-request-002", approve=True, reason="PM 승인",
    actor_role="PM",
)
```

`actor_role="PM"` is required for both approval and rejection. Omitting the role (including
for callers using the older method signature) denies the decision, records a
`proposal_decision_denied` audit event, and never invokes the callback.

Responses contain conversation, message, proposal, and request IDs. A `request_id` can be
processed only once, including after restart. Proposals transition only from `pending` to
`approved` or `rejected`; terminal decisions cannot be reversed. Rejection and pending state
never invoke the integration callback. The audit file is append-only and reconstructs this
state when a new orchestrator instance starts.

The callback runs before an approval event is committed. If it raises, approval is not
recorded and the proposal remains pending, allowing an operator to investigate and retry with
a new request ID. Callbacks should therefore be idempotent in a production adapter.

## Chat data contract

`content` is the canonical message body. It is required by `send_message`, must be a
non-empty string after trimming, and is returned as `messages[*].content`. Aliases such as
`message`, `text`, or `prompt` are not accepted.

Conversation context is optional for backward compatibility. `role` must be one of `PM`,
`Bull`, `Bear`, `Risk`, or `Research`; `team` is a non-empty string; and `metadata` is an
object supporting the non-empty string fields `channel` and `team_label`. Context supplied
when creating a conversation is inherited by later sends. A send may supply context to
update it. The effective `role`, `team`, and `metadata` are returned at the top level,
included in `conversation_created` and `chat_completed` audit payloads, and restored after
restart. Older calls that provide only the original arguments remain valid.
