import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
} from "@assistant-ui/react";
import { unstable_createLangGraphStream, useLangGraphRuntime } from "@assistant-ui/react-langgraph";
import { Client } from "@langchain/langgraph-sdk";
import { useMemo, type ReactNode } from "react";

const AGENT_URL = "http://localhost:2024";
const ASSISTANT_ID = "task_agent";
const THREAD_KEY = "pao-thread";

function savedThread(): string | undefined {
  try {
    return localStorage.getItem(THREAD_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

function saveThread(id: string | undefined) {
  try {
    if (id) localStorage.setItem(THREAD_KEY, id);
    else localStorage.removeItem(THREAD_KEY);
  } catch {
    // 저장하지 못해도 채팅은 동작한다. 새로고침 때 새 대화로 시작할 뿐이다.
  }
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const client = useMemo(() => new Client({ apiUrl: AGENT_URL }), []);
  const stream = useMemo(
    // 노드가 완성된 메시지를 state로 돌려주므로 토큰 스트림 대신 values를 받는다.
    () => unstable_createLangGraphStream({ client, assistantId: ASSISTANT_ID, streamMode: ["values"] }),
    [client],
  );
  const runtime = useLangGraphRuntime({
    stream,
    initialThreadId: savedThread(),
    onThreadIdChange: saveThread,
    create: async () => {
      const thread = await client.threads.create();
      return { externalId: thread.thread_id };
    },
    load: async (threadId) => {
      const state = await client.threads.getState<{ messages?: never[] }>(threadId);
      return { messages: state.values.messages ?? [], interrupts: [] };
    },
  });
  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>;
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="mb-4 flex justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-lg bg-raised px-3 py-2">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="mb-4">
      <div className="max-w-[90%] whitespace-pre-wrap break-words leading-relaxed">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

export function Chat() {
  return (
    <ThreadPrimitive.Root className="mx-auto flex h-full max-w-3xl flex-col">
      <ThreadPrimitive.Viewport className="min-h-0 flex-1 overflow-y-auto p-5">
        <ThreadPrimitive.Empty>
          <p className="mt-20 text-center text-muted">
            "내일까지 2325 Eval 돌려야 해", "브리핑"처럼 말해 보세요.
          </p>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
        <ThreadPrimitive.If running>
          <p className="text-sm text-muted">생각하는 중…</p>
        </ThreadPrimitive.If>
      </ThreadPrimitive.Viewport>
      <ComposerPrimitive.Root className="flex gap-2 border-t border-line p-3">
        <ComposerPrimitive.Input
          aria-label="에이전트에게 말하기"
          placeholder="할 일을 말해 주세요"
          rows={1}
          className="min-h-10 flex-1 resize-none rounded-lg border border-line bg-panel px-3 py-2 outline-none focus:border-accent"
        />
        <ComposerPrimitive.Send className="rounded-lg border border-line bg-raised px-3 text-sm hover:border-accent hover:text-accent disabled:opacity-40">
          보내기
        </ComposerPrimitive.Send>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
