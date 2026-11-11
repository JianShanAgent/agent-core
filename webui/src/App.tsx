import { useQuery, useQueryClient } from "@tanstack/react-query";
import { WifiOff } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect } from "react";
import { Navigate, Route, Routes } from "react-router";

import { EmptyState, LoadingRows } from "@/components/common";
import { AppShell } from "@/components/layout/AppShell";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { api, setUnauthorizedHandler } from "@/lib/api";
import { Login } from "@/pages/Login";

// 页面按需加载：首页的图表库比较大，别的页面用不着
const Dashboard = lazy(() => import("@/pages/Dashboard"));
const Chat = lazy(() => import("@/pages/Chat"));
const Traces = lazy(() => import("@/pages/Traces"));
const TraceDetail = lazy(() => import("@/pages/TraceDetail"));
const Calls = lazy(() => import("@/pages/Calls"));
const Logs = lazy(() => import("@/pages/Logs"));
const Config = lazy(() => import("@/pages/Config"));
const Models = lazy(() => import("@/pages/Models"));
const Plugins = lazy(() => import("@/pages/Plugins"));
const Knowledge = lazy(() => import("@/pages/Knowledge"));
const Sessions = lazy(() => import("@/pages/Sessions"));
const ChatLog = lazy(() => import("@/pages/ChatLog"));
const System = lazy(() => import("@/pages/System"));

export function App() {
  const client = useQueryClient();
  const session = useQuery({ queryKey: ["session"], queryFn: api.session, retry: false });

  // 登录状态变了（登录、退出）：直接改登录状态，页面跟着切；别的数据全丢掉——退出后不该留着，
  // 重新登录后要重取。不能用 client.clear()：它会把页面正盯着的那份登录状态也丢掉，
  // 之后再写入的是新的一份，页面收不到通知，停在原地不动。
  const setAuthenticated = useCallback(
    (authenticated: boolean) => {
      client.setQueryData(["session"], { authenticated, locked_seconds: 0 });
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== "session" });
    },
    [client],
  );

  useEffect(() => {
    // 哪个接口回了「没登录」，就回到登录页
    setUnauthorizedHandler(() => setAuthenticated(false));
  }, [setAuthenticated]);

  if (session.isPending) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  if (session.isError) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <EmptyState
          icon={WifiOff}
          title="连不上机器人"
          hint="它可能正在重启，或者已经停了。"
          action={
            <Button variant="outline" size="sm" className="mt-1" onClick={() => session.refetch()}>
              再试一次
            </Button>
          }
        />
      </div>
    );
  }
  if (!session.data.authenticated) {
    return <Login locked={session.data.locked_seconds} onDone={() => setAuthenticated(true)} />;
  }

  const logout = () => setAuthenticated(false);

  return (
    <Suspense fallback={<LoadingRows className="mx-auto max-w-3xl pt-24" />}>
      <Routes>
        <Route element={<AppShell onLogout={logout} />}>
          <Route index element={<Dashboard />} />
          <Route path="chat" element={<Chat />} />
          <Route path="traces" element={<Traces />} />
          <Route path="traces/:id" element={<TraceDetail />} />
          <Route path="calls" element={<Calls />} />
          <Route path="logs" element={<Logs />} />
          <Route path="config/*" element={<Config />} />
          <Route path="models" element={<Models />} />
          <Route path="plugins" element={<Plugins />} />
          <Route path="knowledge" element={<Knowledge />} />
          <Route path="sessions" element={<Sessions />} />
          <Route path="sessions/:key" element={<Sessions />} />
          <Route path="chatlog" element={<ChatLog />} />
          <Route path="chatlog/:chat" element={<ChatLog />} />
          <Route path="system" element={<System />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
