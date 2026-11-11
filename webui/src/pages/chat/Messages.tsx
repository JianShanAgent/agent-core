// 网页对话的消息区：用户的话在右，机器人的回复在左。

import { CircleAlert } from "lucide-react";
import { memo, useEffect, useState } from "react";
import { Link } from "react-router";

import { CopyButton, Markdown } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { formatSeconds } from "@/lib/format";
import { cn } from "@/lib/utils";

import { type BotEntry, type Entry, type Flight, picturesOf, type UserEntry } from "./session";

/** 网页对话的处理记录都在这里（还在处理的排在最上面）。 */
const TRACES = "/traces?channel=web";
const BUBBLE = "max-w-[85%] rounded-2xl px-3.5 py-2.5";

const UserBubble = memo(function UserBubble({ entry }: { entry: UserEntry }) {
  const pictures = picturesOf(entry.id);
  return (
    <div className="flex justify-end">
      <div
        className={cn(
          BUBBLE,
          "bg-primary text-primary-foreground flex flex-col gap-1.5 rounded-br-md text-sm leading-relaxed",
        )}
      >
        {pictures.length ? (
          <div className="flex flex-wrap gap-1.5">
            {pictures.map((source, index) => (
              <img
                key={index}
                src={source}
                alt={`发的第 ${index + 1} 张图片`}
                className="size-16 rounded-md object-cover"
              />
            ))}
          </div>
        ) : entry.images > 0 ? (
          <div className="text-xs opacity-70">
            带了 {entry.images} 张图片（图片没有存下来，刷新后看不到了）
          </div>
        ) : null}
        {entry.text ? <div className="break-words whitespace-pre-wrap">{entry.text}</div> : null}
      </div>
    </div>
  );
});

/** 处理中途收到过的进度：处理完以后留在回复上面，颜色淡一些。 */
function Trail({ lines }: { lines: string[] }) {
  if (!lines.length) return null;
  return (
    <ol className="text-muted-foreground/70 flex max-w-[85%] flex-col gap-0.5 pl-1 text-xs">
      {lines.map((line, index) => (
        <li key={index} className="break-words whitespace-pre-wrap">
          {line}
        </li>
      ))}
    </ol>
  );
}

/** 用了多久，去哪看这条是怎么处理的。 */
function Facts({ entry }: { entry: BotEntry }) {
  return (
    <>
      {entry.seconds !== null ? <span>用时 {formatSeconds(entry.seconds)}</span> : null}
      {entry.traceId ? (
        <Link to={`/traces/${entry.traceId}`} className="text-brand hover:underline">
          看处理过程
        </Link>
      ) : null}
    </>
  );
}

/** 回复下面的一行小字。 */
function Footer({ entry, copy }: { entry: BotEntry; copy?: string }) {
  if (entry.seconds === null && !entry.traceId && !copy) return null;
  return (
    <div className="text-muted-foreground flex items-center gap-2 pl-1 text-[11px]">
      <Facts entry={entry} />
      {copy ? <CopyButton text={copy} label="复制回复" /> : null}
    </div>
  );
}

const BotBubble = memo(function BotBubble({
  entry,
  busy,
  onCommand,
}: {
  entry: BotEntry;
  busy: boolean;
  onCommand: (command: string) => void;
}) {
  // 早先存下来的记录里可能没有这一项
  const progress = entry.progress ?? [];
  if (entry.state === "silent") {
    return (
      <div className="text-muted-foreground flex flex-wrap items-center justify-center gap-x-2 gap-y-0.5 text-center text-xs">
        <span>机器人没有回复这条（比如被限流了）</span>
        <Facts entry={entry} />
      </div>
    );
  }
  if (entry.state === "error") {
    return (
      <div className="flex flex-col items-start gap-1">
        <Trail lines={progress} />
        <div
          className={cn(
            BUBBLE,
            "border-destructive/30 bg-destructive/8 text-destructive flex items-start gap-2 rounded-bl-md border text-sm",
          )}
        >
          <CircleAlert className="mt-0.5 size-4 shrink-0" />
          <span className="min-w-0 leading-relaxed break-words whitespace-pre-wrap">
            {entry.content || "处理出错了"}
          </span>
        </div>
        <Footer entry={entry} />
      </div>
    );
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <Trail lines={progress} />
      {entry.content ? (
        // 表格太宽时在气泡里横着滚，不把气泡撑出去
        <div className={cn(BUBBLE, "bg-card overflow-x-auto rounded-bl-md border shadow-xs")}>
          {entry.markdown ? (
            <Markdown>{entry.content}</Markdown>
          ) : (
            <div className="text-sm leading-relaxed break-words whitespace-pre-wrap">
              {entry.content}
            </div>
          )}
        </div>
      ) : null}
      {entry.buttons.length ? (
        <div className="flex max-w-[85%] flex-wrap gap-1.5">
          {entry.buttons.map((button, index) => (
            <Button
              key={`${index}-${button.command}`}
              variant="outline"
              size="xs"
              disabled={busy}
              onClick={() => onCommand(button.command)}
            >
              {button.label}
            </Button>
          ))}
        </div>
      ) : null}
      <Footer entry={entry} copy={entry.content} />
    </div>
  );
});

/** 过了多少秒，每秒跳一下。 */
function useWaited(startedAt: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

/** 正在处理：一个转圈，旁边逐句列出收到的进度（最新一句醒目，前面的变淡）和已经过了多久。 */
function Pending({ flight }: { flight: Flight }) {
  const waited = useWaited(flight.startedAt);
  const lines = flight.progress;
  return (
    <div className="flex flex-col items-start gap-1">
      <div className={cn(BUBBLE, "bg-card flex items-start gap-2.5 rounded-bl-md border text-sm shadow-xs")}>
        <Spinner className="text-brand mt-[3px] shrink-0" />
        <div className="flex min-w-0 flex-col gap-0.5">
          {lines.length === 0 ? (
            <span>正在处理…</span>
          ) : (
            lines.map((line, index) => (
              <span
                key={index}
                className={cn(
                  "break-words whitespace-pre-wrap",
                  index === lines.length - 1 ? "font-medium" : "text-muted-foreground/70",
                )}
              >
                {line}
              </span>
            ))
          )}
          <span className="text-muted-foreground text-xs tabular-nums">
            已经过了{" "}
            {waited < 60 ? `${waited} 秒` : `${Math.floor(waited / 60)} 分 ${waited % 60} 秒`}
          </span>
        </div>
      </div>
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-2 pl-1 text-[11px]">
        <span>查证类的问题要十几到几十秒</span>
        <Link to={TRACES} className="text-brand hover:underline">
          看它走到哪一步了
        </Link>
      </div>
    </div>
  );
}

/** 存着的「还在等」但不是正在进行的那一次：页面刷新前没等到结果。 */
function Lost() {
  return (
    <div className="text-muted-foreground pl-1 text-xs">
      这条没等到结果（页面刷新了），可以去
      <Link to={TRACES} className="text-brand hover:underline">
        查证过程
      </Link>
      里找
    </div>
  );
}

export function Messages({
  entries,
  flight,
  onCommand,
}: {
  entries: Entry[];
  flight: Flight | null;
  onCommand: (command: string) => void;
}) {
  const busy = flight !== null;
  return (
    <div className="mx-auto flex w-full max-w-[52rem] flex-col gap-4 px-1 py-4">
      {entries.map((entry) =>
        entry.kind === "user" ? (
          <UserBubble key={entry.id} entry={entry} />
        ) : entry.kind === "divider" ? (
          <div key={entry.id} className="text-muted-foreground py-1 text-center text-xs">
            {entry.text}
          </div>
        ) : entry.state !== "pending" ? (
          <BotBubble
            key={entry.id}
            entry={entry}
            // 只有带按钮的回复才关心现在能不能发：别的回复不用跟着重画
            busy={entry.buttons.length ? busy : false}
            onCommand={onCommand}
          />
        ) : flight?.id === entry.id ? (
          <Pending key={entry.id} flight={flight} />
        ) : (
          <Lost key={entry.id} />
        ),
      )}
    </div>
  );
}
