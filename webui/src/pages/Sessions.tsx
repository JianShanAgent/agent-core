// 用户与会话：每个和机器人说过话的人（群里是每个成员各一份）的对话历史、记忆、处理记录。

import { MessagesSquare } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { EmptyState, PageHeader } from "@/components/common";
import { FullHeight } from "@/components/layout/AppShell";

import { SessionDetail } from "./sessions/SessionDetail";
import { SessionList } from "./sessions/SessionList";
import { TwoPane } from "./sessions/shared";

export default function Sessions() {
  // 地址里是 /sessions/<会话键>（会话键里有冒号，写进地址时转过码，这里拿到的已经解开了）
  const { key: sessionKey } = useParams();
  const navigate = useNavigate();
  // 看哪个标签记在这一层：换一个会话时还停在同一个标签上，方便挨个比
  const [tab, setTab] = useState("history");

  return (
    <FullHeight>
      <PageHeader
        title="用户与会话"
        description="每个和机器人说过话的人各有一份：带给模型的对话历史、机器人记住的事、处理记录。群里是每个成员各一份。"
      />
      <TwoPane
        selected={Boolean(sessionKey)}
        list={
          <SessionList
            selected={sessionKey}
            onSelect={(next) => navigate(`/sessions/${encodeURIComponent(next)}`)}
          />
        }
        detail={
          sessionKey ? (
            <SessionDetail key={sessionKey} sessionKey={sessionKey} tab={tab} onTab={setTab} />
          ) : (
            <EmptyState
              className="flex-1"
              icon={MessagesSquare}
              title="选一个会话看它的对话历史和记忆"
              hint="左边按最近活动的时间排。平台给的标识看不懂的话，点进去可以起个备注名。"
            />
          )
        }
      />
    </FullHeight>
  );
}
