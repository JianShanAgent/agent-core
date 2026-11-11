// 网页对话：不经过 QQ，在网页里直接和机器人说话，调试用。走的是和 QQ 消息完全一样的处理流程。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleHelp, Eraser, ImagePlus, MessageSquare, MessageSquarePlus } from "lucide-react";
import { type DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { FullHeight } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Input, Switch } from "@/components/ui/form";
import { Hint } from "@/components/ui/misc";
import { api, type ChatRequest } from "@/lib/api";
import { useStored } from "@/lib/hooks";

import { Composer, type ComposerHandle } from "./chat/Composer";
import { Messages } from "./chat/Messages";
import {
  addDivider,
  ask,
  attach,
  cleanUser,
  DEFAULT_USER,
  type Entry,
  loadLog,
  LOG_KEY,
  tidy,
  useFlight,
} from "./chat/session";

// 输入框下面的几个例子。现在写的是阴阳师 wiki 的；换了 wiki 要跟着换（页面上的文字不从配置来）
const EXAMPLES = ["/查 山风", "千姬的CV是谁", "SSR里有哪些拉条式神", "你好"];
const NEW_TOPIC = "—— 新话题 ——";

/** 拖过来的是不是文件（拖一段选中的文字不算）。 */
const carriesFiles = (event: DragEvent) => event.dataTransfer.types.includes("Files");

function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-[52rem] flex-col items-center justify-center gap-4 px-6 py-10 text-center">
      <MessageSquare className="text-muted-foreground size-8 opacity-40" />
      {/* 写成两段字符串：直接折行的话，两行之间会多出一个空格 */}
      <p className="text-muted-foreground max-w-lg text-sm leading-relaxed">
        {"不经过 QQ，在这里直接和机器人说话，走的是和 QQ 消息完全一样的处理流程。" +
          "网页上的身份是独立的，它的对话历史、记忆和 QQ 上的用户互不相干；这里发的消息不进群聊记录。"}
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        {EXAMPLES.map((example) => (
          <Button key={example} variant="outline" size="sm" onClick={() => onPick(example)}>
            {example}
          </Button>
        ))}
      </div>
    </div>
  );
}

export default function Chat() {
  const client = useQueryClient();
  const [savedUser, setSavedUser] = useStored("jianshanagent.chat.user", DEFAULT_USER);
  const user = cleanUser(String(savedUser)) || DEFAULT_USER;
  // 输入框里正在打的字。用输入法打汉字时中间会出现拼音和分隔符，先原样留着，存的时候再去掉不合规的
  const [draft, setDraft] = useState(user);
  const [group, setGroup] = useStored("jianshanagent.chat.group", false);
  const [stored, setStored] = useStored<Entry[]>(LOG_KEY, []);
  const flight = useFlight();
  const scope: ChatRequest["scope"] = group ? "group" : "c2c";
  const entries = useMemo(() => (Array.isArray(stored) ? stored : []), [stored]);

  // 改记录都走这里。回复是一段一段到的，每次要在「最新的那份」上改，所以另外记一份随时能读的
  const latest = useRef(entries);
  const change = useCallback(
    (update: (log: Entry[]) => Entry[]) => {
      const next = tidy(update(latest.current));
      latest.current = next;
      setStored(next);
    },
    [setStored],
  );
  // 页面开着的时候，正在进行的请求把回复交到这里；离开页面后它自己直接改浏览器里存的那份
  useEffect(() => {
    const detach = attach(change);
    // 从读出记录到挂上来之间有一小会儿，这期间到的回复是直接写进浏览器的：对一下，别漏了
    const saved = loadLog();
    if (JSON.stringify(saved) !== JSON.stringify(latest.current)) change(() => saved);
    return detach;
  }, [change]);

  // send 不随身份变：它一变，每条回复（里面的按钮要用它）都得重画一遍
  const identity = useRef<Pick<ChatRequest, "user" | "scope">>({ user, scope });
  useEffect(() => {
    identity.current = { user, scope };
  }, [user, scope]);
  const send = useCallback((text: string, images: string[] = []) => {
    ask({ text, images, ...identity.current });
  }, []);

  // 清空屏幕。正在等回复的那一问一答留着，不然回复到了没处放
  const clear = () => {
    change((log) => log.filter((entry) => entry.id === flight?.id || entry.id === flight?.askId));
  };

  const reset = useMutation({
    mutationFn: () => api.resetChat(user, scope),
    onSuccess: (data) => {
      // 屏幕上的记录留着，只画一条线：线上面的那些话，机器人那边已经不记得了
      addDivider(NEW_TOPIC);
      void client.invalidateQueries({ queryKey: ["sessions"] });
      toast.success(
        data.deleted
          ? `已开始新话题，清掉了机器人那边的 ${data.deleted} 条对话历史`
          : "已开始新话题（机器人那边本来就没有对话历史）",
      );
    },
    onError: (error: Error) => toast.error("没能清掉对话历史", { description: error.message }),
  });

  // 新消息、新进度来了滚到底
  const scroller = useRef<HTMLDivElement>(null);
  const last = entries[entries.length - 1];
  const lastState = last?.kind === "bot" ? last.state : "";
  const mark = `${entries.length}:${last?.id}:${lastState}:${flight?.progress.length ?? 0}`;
  useEffect(() => {
    const element = scroller.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [mark]);

  // 把图片拖到页面上：交给输入区。进出子元素时 enter / leave 会成对地多触发，数着层数才知道是不是真的拖出去了
  const composer = useRef<ComposerHandle>(null);
  const [dragging, setDragging] = useState(false);
  const depth = useRef(0);
  const onDragEnter = (event: DragEvent) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    depth.current += 1;
    setDragging(true);
  };
  const onDragOver = (event: DragEvent) => {
    // 不拦下来的话，松手时浏览器会直接打开那个文件
    if (carriesFiles(event)) event.preventDefault();
  };
  const onDragLeave = (event: DragEvent) => {
    if (!carriesFiles(event)) return;
    depth.current = Math.max(0, depth.current - 1);
    if (depth.current === 0) setDragging(false);
  };
  const onDrop = (event: DragEvent) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    depth.current = 0;
    setDragging(false);
    composer.current?.addFiles([...event.dataTransfer.files]);
  };

  return (
    <FullHeight>
      <div
        className="relative flex min-h-0 flex-1 flex-col"
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b pb-2.5">
          <h1 className="text-base font-semibold tracking-tight">网页对话</h1>
          <span className="text-muted-foreground hidden text-xs xl:inline">
            调试用：不经过 QQ，直接和机器人说话
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-2">
            <Hint text="网页上用谁的身份说话。换一个名字就是另一个人，对话历史和记忆各是各的，和 QQ 上的用户也互不相干。只能用字母、数字、汉字和连字符，最多 32 个字；不填就是 admin。">
              <label className="text-muted-foreground flex items-center gap-2 text-xs">
                身份
                <Input
                  className="text-foreground h-8 w-28 px-2.5 text-[13px]"
                  value={draft}
                  maxLength={32}
                  placeholder={DEFAULT_USER}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    setSavedUser(cleanUser(event.target.value) || DEFAULT_USER);
                  }}
                  onBlur={() => setDraft(user)}
                />
              </label>
            </Hint>
            <Hint text="开着时按群聊里的消息处理，关着时按单聊处理。群聊里记忆的规则不一样：只记明确要求记住的。">
              <label className="flex cursor-pointer items-center gap-1.5 text-sm">
                <Switch checked={group} onCheckedChange={setGroup} className="mr-0.5" />
                按群聊处理
                <CircleHelp className="text-muted-foreground size-3.5" />
              </label>
            </Hint>
            <Hint text="清掉机器人那边这个身份的对话历史（等于发了一句 /新话题）。屏幕上的记录留着，中间画一条线。">
              <Button
                variant="outline"
                size="sm"
                loading={reset.isPending}
                disabled={flight !== null}
                onClick={() => reset.mutate()}
              >
                {reset.isPending ? null : <MessageSquarePlus />} 新话题
              </Button>
            </Hint>
            <Hint text="只清页面上显示的，机器人那边的对话历史不动。">
              <Button variant="ghost" size="sm" disabled={entries.length === 0} onClick={clear}>
                <Eraser /> 清空屏幕
              </Button>
            </Hint>
          </div>
        </div>
        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
          {entries.length === 0 ? (
            <Welcome onPick={send} />
          ) : (
            <Messages entries={entries} flight={flight} onCommand={send} />
          )}
        </div>
        <div className="mx-auto w-full max-w-[52rem] px-1 pt-2">
          <div className="bg-card rounded-xl border p-2.5 shadow-xs">
            <Composer ref={composer} busy={flight !== null} onSend={send} />
          </div>
        </div>
        {dragging ? (
          // 只是个提示，不接鼠标事件：不然拖到它上面又算「拖出去了」
          <div className="border-brand/50 bg-background/85 text-muted-foreground pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed text-sm">
            <ImagePlus className="size-7 opacity-60" />
            松手，把图片加到这条消息里
          </div>
        ) : null}
      </div>
    </FullHeight>
  );
}
